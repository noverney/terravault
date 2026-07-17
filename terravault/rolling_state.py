"""Durable SQLite state for the rolling Sentinel-2 ingestion worker."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""

    return datetime.now(timezone.utc)


def as_utc_text(value: datetime | None = None) -> str:
    """Serialize a datetime into a stable, lexically sortable UTC string."""

    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class RollingState:
    """Persist runs, product jobs, assets, failures and operator events.

    The database is intentionally independent of the original one-shot
    ``StateManager``.  A rolling item is complete only when every requested
    asset is complete and no required asset is missing.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    discovered INTEGER NOT NULL DEFAULT 0,
                    queued INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    item_id TEXT PRIMARY KEY,
                    item_datetime TEXT NOT NULL,
                    collection_id TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    metadata_path TEXT NOT NULL,
                    status_path TEXT NOT NULL,
                    requested_assets_json TEXT NOT NULL,
                    missing_assets_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assets (
                    item_id TEXT NOT NULL,
                    asset_key TEXT NOT NULL,
                    href TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    local_path TEXT NOT NULL,
                    bytes INTEGER,
                    sha256 TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (item_id, asset_key),
                    FOREIGN KEY (item_id) REFERENCES jobs(item_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    run_id INTEGER,
                    item_id TEXT,
                    asset_key TEXT,
                    message TEXT NOT NULL,
                    details_json TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE SET NULL,
                    FOREIGN KEY (item_id) REFERENCES jobs(item_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS ignored_items (
                    item_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS jobs_due_idx
                    ON jobs(status, next_attempt_at, updated_at);
                CREATE INDEX IF NOT EXISTS events_item_idx
                    ON events(item_id, event_id);
                """
            )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> RollingState:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def configure_roi(self, fingerprint: str, canonical_geojson: str) -> None:
        """Bind this database to one ROI and reject accidental reuse."""

        current = self.get_setting("roi_fingerprint")
        if current is not None and current != fingerprint:
            raise ValueError(
                "The rolling state database is already bound to a different ROI. "
                "Use a different --state-db or restore the original ROI."
            )
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                ("roi_fingerprint", fingerprint),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                ("roi_geojson", canonical_geojson),
            )

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        """Insert or atomically replace an operational setting."""

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def delete_setting(self, key: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM settings WHERE key = ?", (key,))

    def ignore_item(self, item_id: str, reason: str) -> None:
        """Record an item intentionally excluded by the initial bootstrap."""

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO ignored_items(item_id, reason, observed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    reason = excluded.reason,
                    observed_at = excluded.observed_at
                """,
                (item_id, reason, as_utc_text()),
            )

    def is_ignored(self, item_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM ignored_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row is not None

    def unignore_item(self, item_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM ignored_items WHERE item_id = ?", (item_id,)
            )

    def start_run(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO runs(started_at, status) VALUES (?, ?)",
                (as_utc_text(), "running"),
            )
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        discovered: int,
        queued: int,
        completed: int,
        failed: int,
        error: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE runs
                   SET finished_at = ?, status = ?, discovered = ?, queued = ?,
                       completed = ?, failed = ?, error = ?
                 WHERE run_id = ?
                """,
                (
                    as_utc_text(),
                    status,
                    discovered,
                    queued,
                    completed,
                    failed,
                    error,
                    run_id,
                ),
            )

    def recover_interrupted(self) -> tuple[int, int]:
        """Move interrupted in-progress records back to their durable queue."""

        now = as_utc_text()
        with self.connection:
            assets = self.connection.execute(
                """
                UPDATE assets
                   SET status = 'queued', updated_at = ?
                 WHERE status = 'processing'
                """,
                (now,),
            ).rowcount
            jobs = self.connection.execute(
                """
                UPDATE jobs
                   SET status = 'queued', next_attempt_at = NULL, updated_at = ?
                 WHERE status = 'processing'
                """,
                (now,),
            ).rowcount
        return int(jobs), int(assets)

    def upsert_job(
        self,
        *,
        item_id: str,
        item_datetime: datetime,
        collection_id: str | None,
        metadata_path: str | Path,
        status_path: str | Path,
        requested_assets: Iterable[str],
        missing_assets: Iterable[str],
    ) -> bool:
        """Insert or refresh one item job.

        Returns ``True`` when the item was inserted or its requested/missing
        asset policy changed.
        """

        requested_json = json.dumps(sorted(set(requested_assets)))
        missing_json = json.dumps(sorted(set(missing_assets)))
        now = as_utc_text()
        existing = self.connection.execute(
            "SELECT requested_assets_json, missing_assets_json FROM jobs WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        changed = existing is not None and (
            existing["requested_assets_json"] != requested_json
            or existing["missing_assets_json"] != missing_json
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO jobs(
                    item_id, item_datetime, collection_id, status, metadata_path,
                    status_path, requested_assets_json, missing_assets_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    item_datetime = excluded.item_datetime,
                    collection_id = excluded.collection_id,
                    metadata_path = excluded.metadata_path,
                    status_path = excluded.status_path,
                    requested_assets_json = excluded.requested_assets_json,
                    missing_assets_json = excluded.missing_assets_json,
                    status = CASE
                        WHEN jobs.requested_assets_json != excluded.requested_assets_json
                          OR jobs.missing_assets_json != excluded.missing_assets_json
                        THEN 'queued'
                        ELSE jobs.status
                    END,
                    attempts = CASE
                        WHEN jobs.requested_assets_json != excluded.requested_assets_json
                          OR jobs.missing_assets_json != excluded.missing_assets_json
                        THEN 0
                        ELSE jobs.attempts
                    END,
                    next_attempt_at = CASE
                        WHEN jobs.requested_assets_json != excluded.requested_assets_json
                          OR jobs.missing_assets_json != excluded.missing_assets_json
                        THEN NULL
                        ELSE jobs.next_attempt_at
                    END,
                    last_error = CASE
                        WHEN jobs.requested_assets_json != excluded.requested_assets_json
                          OR jobs.missing_assets_json != excluded.missing_assets_json
                        THEN NULL
                        ELSE jobs.last_error
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    item_id,
                    as_utc_text(item_datetime),
                    collection_id,
                    str(metadata_path),
                    str(status_path),
                    requested_json,
                    missing_json,
                    now,
                    now,
                ),
            )
        return existing is None or changed

    def upsert_asset(
        self,
        *,
        item_id: str,
        asset_key: str,
        href: str,
        local_path: str | Path,
    ) -> bool:
        """Insert or refresh an asset, returning whether its source changed."""

        existing = self.connection.execute(
            """
            SELECT href, local_path
              FROM assets
             WHERE item_id = ? AND asset_key = ?
            """,
            (item_id, asset_key),
        ).fetchone()
        changed = existing is not None and (
            existing["href"] != href or existing["local_path"] != str(local_path)
        )
        now = as_utc_text()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO assets(
                    item_id, asset_key, href, status, local_path, updated_at
                )
                VALUES (?, ?, ?, 'queued', ?, ?)
                ON CONFLICT(item_id, asset_key) DO UPDATE SET
                    href = excluded.href,
                    local_path = excluded.local_path,
                    status = CASE
                        WHEN assets.href != excluded.href
                          OR assets.local_path != excluded.local_path
                        THEN 'queued'
                        ELSE assets.status
                    END,
                    attempts = CASE
                        WHEN assets.href != excluded.href
                          OR assets.local_path != excluded.local_path
                        THEN 0
                        ELSE assets.attempts
                    END,
                    bytes = CASE
                        WHEN assets.href != excluded.href
                          OR assets.local_path != excluded.local_path
                        THEN NULL
                        ELSE assets.bytes
                    END,
                    sha256 = CASE
                        WHEN assets.href != excluded.href
                          OR assets.local_path != excluded.local_path
                        THEN NULL
                        ELSE assets.sha256
                    END,
                    last_error = CASE
                        WHEN assets.href != excluded.href
                          OR assets.local_path != excluded.local_path
                        THEN NULL
                        ELSE assets.last_error
                    END,
                    updated_at = excluded.updated_at
                """,
                (item_id, asset_key, href, str(local_path), now),
            )
        return existing is None or changed

    def retain_assets(self, item_id: str, asset_keys: Iterable[str]) -> int:
        """Remove queue rows no longer selected/present, leaving files untouched."""

        keys = tuple(dict.fromkeys(asset_keys))
        with self.connection:
            if keys:
                placeholders = ", ".join("?" for _key in keys)
                cursor = self.connection.execute(
                    f"""
                    DELETE FROM assets
                     WHERE item_id = ?
                       AND asset_key NOT IN ({placeholders})
                    """,
                    (item_id, *keys),
                )
            else:
                cursor = self.connection.execute(
                    "DELETE FROM assets WHERE item_id = ?", (item_id,)
                )
        return int(cursor.rowcount)

    def due_jobs(
        self,
        *,
        limit: int,
        now: datetime | None = None,
        item_datetime_from: datetime | None = None,
        item_datetime_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
              FROM jobs
             WHERE (
                    status = 'queued'
                    OR (status = 'retry_wait' AND next_attempt_at <= ?)
                   )
               AND (? IS NULL OR item_datetime >= ?)
               AND (? IS NULL OR item_datetime < ?)
             ORDER BY item_datetime, item_id
             LIMIT ?
            """,
            (
                as_utc_text(now),
                None
                if item_datetime_from is None
                else as_utc_text(item_datetime_from),
                None
                if item_datetime_from is None
                else as_utc_text(item_datetime_from),
                None if item_datetime_to is None else as_utc_text(item_datetime_to),
                None if item_datetime_to is None else as_utc_text(item_datetime_to),
                limit,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def window_status_counts(
        self,
        start: datetime,
        end: datetime,
    ) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
              FROM jobs
             WHERE item_datetime >= ? AND item_datetime < ?
             GROUP BY status
            """,
            (as_utc_text(start), as_utc_text(end)),
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def next_retry_at(
        self,
        start: datetime,
        end: datetime,
    ) -> datetime | None:
        row = self.connection.execute(
            """
            SELECT MIN(next_attempt_at) AS next_attempt_at
              FROM jobs
             WHERE status = 'retry_wait'
               AND item_datetime >= ? AND item_datetime < ?
            """,
            (as_utc_text(start), as_utc_text(end)),
        ).fetchone()
        raw = None if row is None else row["next_attempt_at"]
        if raw is None:
            return None
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))

    def assets_for_job(self, item_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM assets WHERE item_id = ? ORDER BY asset_key", (item_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def claim_job(self, item_id: str) -> bool:
        with self.connection:
            count = self.connection.execute(
                """
                UPDATE jobs
                   SET status = 'processing', updated_at = ?
                 WHERE item_id = ? AND status IN ('queued', 'retry_wait')
                """,
                (as_utc_text(), item_id),
            ).rowcount
        return bool(count)

    def mark_asset_processing(self, item_id: str, asset_key: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE assets
                   SET status = 'processing', updated_at = ?
                 WHERE item_id = ? AND asset_key = ?
                """,
                (as_utc_text(), item_id, asset_key),
            )

    def mark_asset_complete(
        self,
        item_id: str,
        asset_key: str,
        *,
        byte_count: int,
        sha256: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE assets
                   SET status = 'completed', bytes = ?, sha256 = ?,
                       last_error = NULL, updated_at = ?
                 WHERE item_id = ? AND asset_key = ?
                """,
                (byte_count, sha256, as_utc_text(), item_id, asset_key),
            )

    def mark_asset_retry(self, item_id: str, asset_key: str, error: str) -> int:
        with self.connection:
            self.connection.execute(
                """
                UPDATE assets
                   SET status = 'retry_wait', attempts = attempts + 1,
                       last_error = ?, updated_at = ?
                 WHERE item_id = ? AND asset_key = ?
                """,
                (error, as_utc_text(), item_id, asset_key),
            )
        row = self.connection.execute(
            "SELECT attempts FROM assets WHERE item_id = ? AND asset_key = ?",
            (item_id, asset_key),
        ).fetchone()
        return int(row["attempts"])

    def mark_asset_retired(self, item_id: str, asset_key: str, error: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE assets
                   SET status = 'retired', last_error = ?, updated_at = ?
                 WHERE item_id = ? AND asset_key = ?
                """,
                (error, as_utc_text(), item_id, asset_key),
            )

    def requeue_asset(self, item_id: str, asset_key: str, error: str | None = None) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE assets
                   SET status = 'queued', last_error = ?, updated_at = ?
                 WHERE item_id = ? AND asset_key = ?
                """,
                (error, as_utc_text(), item_id, asset_key),
            )

    def mark_job_complete(self, item_id: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE jobs
                   SET status = 'completed', next_attempt_at = NULL,
                       last_error = NULL, updated_at = ?
                 WHERE item_id = ?
                """,
                (as_utc_text(), item_id),
            )

    def mark_job_retry(
        self,
        item_id: str,
        *,
        error: str,
        next_attempt_at: datetime | None,
        terminal: bool,
        terminal_status: str = "failed",
    ) -> int:
        if terminal_status not in {"failed", "retired"}:
            raise ValueError("terminal_status must be failed or retired")
        status = terminal_status if terminal else "retry_wait"
        with self.connection:
            self.connection.execute(
                """
                UPDATE jobs
                   SET status = ?, attempts = attempts + 1, next_attempt_at = ?,
                       last_error = ?, updated_at = ?
                 WHERE item_id = ?
                """,
                (
                    status,
                    None if next_attempt_at is None else as_utc_text(next_attempt_at),
                    error,
                    as_utc_text(),
                    item_id,
                ),
            )
        row = self.connection.execute(
            "SELECT attempts FROM jobs WHERE item_id = ?", (item_id,)
        ).fetchone()
        return int(row["attempts"])

    def requeue_job(self, item_id: str, error: str | None = None) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE jobs
                   SET status = 'queued', next_attempt_at = NULL, last_error = ?,
                       updated_at = ?
                 WHERE item_id = ?
                """,
                (error, as_utc_text(), item_id),
            )

    def requeue_failed_jobs(self) -> tuple[int, int]:
        """Reset failed/retired jobs for an explicit operator-requested retry."""

        now = as_utc_text()
        with self.connection:
            assets = self.connection.execute(
                """
                UPDATE assets
                   SET status = 'queued', attempts = 0, last_error = NULL,
                       updated_at = ?
                 WHERE status NOT IN ('completed')
                   AND item_id IN (
                       SELECT item_id FROM jobs WHERE status IN ('failed', 'retired')
                   )
                """,
                (now,),
            ).rowcount
            jobs = self.connection.execute(
                """
                UPDATE jobs
                   SET status = 'queued', attempts = 0, next_attempt_at = NULL,
                       last_error = NULL, updated_at = ?
                 WHERE status IN ('failed', 'retired')
                """,
                (now,),
            ).rowcount
        return int(jobs), int(assets)

    def record_event(
        self,
        *,
        level: str,
        message: str,
        run_id: int | None = None,
        item_id: str | None = None,
        asset_key: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO events(
                    timestamp, level, run_id, item_id, asset_key, message, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    as_utc_text(),
                    level,
                    run_id,
                    item_id,
                    asset_key,
                    message,
                    None if details is None else json.dumps(details, sort_keys=True),
                ),
            )

    def job_snapshot(self, item_id: str) -> dict[str, Any]:
        job = self.connection.execute(
            "SELECT * FROM jobs WHERE item_id = ?", (item_id,)
        ).fetchone()
        if job is None:
            raise KeyError(item_id)
        payload = dict(job)
        payload["requested_assets"] = json.loads(payload.pop("requested_assets_json"))
        payload["missing_assets"] = json.loads(payload.pop("missing_assets_json"))
        payload["assets"] = self.assets_for_job(item_id)
        events = self.connection.execute(
            """
            SELECT timestamp, level, run_id, asset_key, message, details_json
              FROM events
             WHERE item_id = ?
             ORDER BY event_id DESC
             LIMIT 20
            """,
            (item_id,),
        ).fetchall()
        payload["recent_events"] = []
        for event in reversed(events):
            event_payload = dict(event)
            details_json = event_payload.pop("details_json")
            event_payload["details"] = (
                None if details_json is None else json.loads(details_json)
            )
            payload["recent_events"].append(event_payload)
        return payload

    def run_snapshot(self, run_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)
