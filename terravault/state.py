"""State tracking for the ingestion pipeline.

Persists the last-processed timestamp and the set of already-ingested STAC
item IDs so that repeated pipeline runs ingest only *new* scenes.

Two backends are supported:

* **JSON** – lightweight, no extra dependencies, suitable for a single process.
* **SQLite** – allows concurrent access and efficient querying over large
  item histories.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 UTC string produced by this module."""
    return datetime.strptime(value, _ISO_FMT).replace(tzinfo=timezone.utc)


def _fmt_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime(_ISO_FMT)


# ---------------------------------------------------------------------------
# JSON backend
# ---------------------------------------------------------------------------

class JSONStateManager:
    """File-based state tracking using a JSON file.

    Parameters
    ----------
    state_file:
        Path to the JSON state file.  Created automatically on first use.
    """

    def __init__(self, state_file: str | Path = "terravault_state.json") -> None:
        self.state_file = Path(state_file)
        self._data: dict = self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read state file %s: %s – starting fresh", self.state_file, exc)
        return {"last_processed": None, "ingested_ids": []}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def last_processed(self) -> datetime | None:
        """Return the timestamp of the most recently processed item."""
        raw = self._data.get("last_processed")
        return _parse_dt(raw) if raw else None

    def mark_processed(self, item_id: str, item_datetime: datetime) -> None:
        """Record *item_id* as processed and update the last-processed timestamp."""
        ids: list = self._data.setdefault("ingested_ids", [])
        if item_id not in ids:
            ids.append(item_id)

        current = self.last_processed
        if current is None or item_datetime > current:
            self._data["last_processed"] = _fmt_dt(item_datetime)

        self._save()

    def is_ingested(self, item_id: str) -> bool:
        """Return ``True`` if *item_id* has already been processed."""
        return item_id in self._data.get("ingested_ids", [])

    def ingested_ids(self) -> set[str]:
        """Return all ingested item IDs as a set."""
        return set(self._data.get("ingested_ids", []))


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

class SQLiteStateManager:
    """SQLite-backed state tracking.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Use ``":memory:"`` for an
        in-memory database (useful in tests).
    """

    _CREATE_SQL = """
        CREATE TABLE IF NOT EXISTS ingested_items (
            item_id       TEXT PRIMARY KEY,
            item_datetime TEXT NOT NULL,
            ingested_at   TEXT NOT NULL
        );
    """

    def __init__(self, db_path: str | Path = "terravault_state.db") -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(self._CREATE_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def last_processed(self) -> datetime | None:
        """Return the timestamp of the most recently processed item."""
        row = self._conn.execute(
            "SELECT MAX(item_datetime) FROM ingested_items"
        ).fetchone()
        if row and row[0]:
            return _parse_dt(row[0])
        return None

    def mark_processed(self, item_id: str, item_datetime: datetime) -> None:
        """Record *item_id* as processed."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO ingested_items (item_id, item_datetime, ingested_at)
            VALUES (?, ?, ?)
            """,
            (item_id, _fmt_dt(item_datetime), _fmt_dt(_utcnow())),
        )
        self._conn.commit()

    def is_ingested(self, item_id: str) -> bool:
        """Return ``True`` if *item_id* has already been processed."""
        row = self._conn.execute(
            "SELECT 1 FROM ingested_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row is not None

    def ingested_ids(self) -> set[str]:
        """Return all ingested item IDs as a set."""
        rows = self._conn.execute("SELECT item_id FROM ingested_items").fetchall()
        return {r[0] for r in rows}

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Factory / alias
# ---------------------------------------------------------------------------

StateManager = SQLiteStateManager
"""Default state manager (SQLite).  Use :class:`JSONStateManager` for a
lightweight alternative that requires no database engine."""
