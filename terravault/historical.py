"""Windowed, resumable historical Sentinel-2 backfill."""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import FrameType
from typing import Any

from tqdm import tqdm

from .rolling import RollingConfig, RollingIngestor, RunLock
from .rolling_state import as_utc_text

logger = logging.getLogger(__name__)


def parse_utc_date(value: str, *, inclusive_end: bool = False) -> datetime:
    """Parse an ISO date/datetime, treating date-only end values as inclusive."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Invalid date {value!r}; expected YYYY-MM-DD or an ISO-8601 datetime"
        ) from exc
    date_only = "T" not in value and " " not in value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    if date_only and inclusive_end:
        parsed += timedelta(days=1)
    return parsed


@dataclass
class HistoricalConfig:
    """Settings for a gradual historical backfill."""

    rolling: RollingConfig
    start_datetime: datetime
    end_datetime: datetime
    window_days: float = 1.0
    progress: bool = True
    max_jobs_per_batch: int = 100

    def __post_init__(self) -> None:
        for name in ("start_datetime", "end_datetime"):
            value = getattr(self, name)
            if value.tzinfo is None:
                setattr(self, name, value.replace(tzinfo=timezone.utc))
            else:
                setattr(self, name, value.astimezone(timezone.utc))
        if self.start_datetime >= self.end_datetime:
            raise ValueError("Historical start date must be before the end date")
        if self.window_days <= 0:
            raise ValueError("window_days must be positive")
        if self.max_jobs_per_batch <= 0:
            raise ValueError("max_jobs_per_batch must be positive")


@dataclass(frozen=True)
class HistoricalRunResult:
    """Summary of one historical worker invocation."""

    windows_completed: int
    discovered: int
    queued: int
    completed: int
    failed: int
    retired: int
    stopped: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _WindowResult:
    advanced: bool
    discovered: int
    queued: int
    completed: int
    failed: int
    retired: int
    errors: tuple[str, ...]


class HistoricalIngestor(RollingIngestor):
    """Backfill an ROI in small durable windows, then stop at the target date."""

    def __init__(self, config: HistoricalConfig, **kwargs: Any) -> None:
        super().__init__(config.rolling, **kwargs)
        self.history_config = config

    def _configure_history(self) -> tuple[datetime, datetime, datetime]:
        requested_start = as_utc_text(self.history_config.start_datetime)
        requested_end = as_utc_text(self.history_config.end_datetime)
        stored_start = self.state.get_setting("history_start")
        if stored_start is not None and stored_start != requested_start:
            raise ValueError(
                "This historical database was created with a different start date. "
                "Use the original date or a different --state-db."
            )
        if stored_start is None:
            self.state.set_setting("history_start", requested_start)
            self.state.set_setting("history_end", requested_end)
            self.state.set_setting("history_cursor", requested_start)

        stored_end = self.state.get_setting("history_end") or requested_end
        stored_cursor = self.state.get_setting("history_cursor") or requested_start
        start = datetime.fromisoformat(requested_start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(stored_end.replace("Z", "+00:00"))
        cursor = datetime.fromisoformat(stored_cursor.replace("Z", "+00:00"))
        if self.config.retry_failed:
            cursor = start
            self.state.set_setting("history_cursor", requested_start)
        return start, end, max(start, min(cursor, end))

    def _notify_wait(self, message: str) -> None:
        logger.warning(message)
        if self.history_config.progress:
            tqdm.write(f"NOTICE: {message}")

    def _wait_until(self, retry_at: datetime) -> None:
        while not self.stop_event.is_set():
            now = self.now()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            else:
                now = now.astimezone(timezone.utc)
            remaining = (retry_at - now).total_seconds()
            if remaining <= 0:
                return
            self.stop_event.wait(min(remaining, 60.0))

    def _drain_window(
        self,
        *,
        run_id: int,
        start: datetime,
        end: datetime,
    ) -> tuple[int, int, int]:
        completed = failed = retired = 0
        initial_counts = self.state.window_status_counts(start, end)
        total_items = sum(initial_counts.values())
        initial_terminal = sum(
            initial_counts.get(status, 0) for status in ("completed", "failed", "retired")
        )
        item_bar = tqdm(
            total=total_items,
            initial=initial_terminal,
            desc=f"Items {start.date()}",
            unit="item",
            leave=False,
            dynamic_ncols=True,
            disable=not self.history_config.progress,
        )
        try:
            while not self.stop_event.is_set():
                counts = self.state.window_status_counts(start, end)
                active = sum(
                    counts.get(status, 0)
                    for status in ("queued", "processing", "retry_wait")
                )
                if active == 0:
                    break

                due = self.state.due_jobs(
                    limit=self.history_config.max_jobs_per_batch,
                    now=self.now(),
                    item_datetime_from=start,
                    item_datetime_to=end,
                )
                if due:
                    for job in due:
                        if self.stop_event.is_set():
                            break
                        outcome = self._process_job(run_id, job)
                        if outcome == "completed":
                            completed += 1
                            item_bar.update(1)
                        elif outcome == "failed":
                            failed += 1
                            item_bar.update(1)
                        elif outcome == "retired":
                            retired += 1
                            item_bar.update(1)
                            self._notify_wait(
                                f"Retired {job['item_id']} after the configured retry limit"
                            )
                        elif outcome == "stopped":
                            break
                    continue

                retry_at = self.state.next_retry_at(start, end)
                if retry_at is None:
                    raise RuntimeError(
                        "Historical window has active jobs but no due or scheduled retry"
                    )
                now = self.now()
                if now.tzinfo is None:
                    now = now.replace(tzinfo=timezone.utc)
                seconds = max(0.0, (retry_at - now).total_seconds())
                self._notify_wait(
                    f"Usage limit or transient error; {counts.get('retry_wait', 0)} "
                    f"job(s) waiting until {as_utc_text(retry_at)} "
                    f"(about {seconds:.0f} seconds)"
                )
                item_bar.set_postfix(waiting=counts.get("retry_wait", 0))
                self._wait_until(retry_at)
        finally:
            item_bar.close()
        return completed, failed, retired

    def _run_window(self, start: datetime, end: datetime) -> _WindowResult:
        run_id = self.state.start_run()
        discovered = queued = completed = failed = retired = 0
        errors: list[str] = []
        logger.info(
            "Historical window started – run=%d start=%s end=%s storage=%s",
            run_id,
            as_utc_text(start),
            as_utc_text(end),
            self.config.storage_root.resolve(),
        )
        try:
            items = []
            for item in self.catalog.search(
                start_datetime=start,
                end_datetime=end - timedelta(microseconds=1),
                max_items=self.config.max_items_per_cycle,
            ):
                if self.stop_event.is_set():
                    break
                items.append(item)
            discovered = len(items)
            queued = self._queue_items(run_id, items)
            if not self.stop_event.is_set():
                completed, failed, retired = self._drain_window(
                    run_id=run_id,
                    start=start,
                    end=end,
                )
        except Exception as exc:  # noqa: BLE001
            error = f"Historical window failed: {type(exc).__name__}: {exc}"
            logger.exception(error)
            errors.append(error)

        advanced = not self.stop_event.is_set() and not errors
        status = (
            "stopped"
            if self.stop_event.is_set()
            else "error"
            if errors
            else "complete_with_retired"
            if retired
            else "complete"
        )
        self.state.finish_run(
            run_id,
            status=status,
            discovered=discovered,
            queued=queued,
            completed=completed,
            failed=failed + retired,
            error="; ".join(errors) or None,
        )
        self.dataset.upsert_run(
            self.state.run_snapshot(run_id),
            mode="historic",
            state_db=self.config.state_db,
        )
        logger.info(
            "Historical window complete – run=%d start=%s end=%s discovered=%d "
            "queued=%d completed=%d failed=%d retired=%d status=%s",
            run_id,
            as_utc_text(start),
            as_utc_text(end),
            discovered,
            queued,
            completed,
            failed,
            retired,
            status,
        )
        return _WindowResult(
            advanced=advanced,
            discovered=discovered,
            queued=queued,
            completed=completed,
            failed=failed,
            retired=retired,
            errors=tuple(errors),
        )

    def run_history(self) -> HistoricalRunResult:
        """Run or resume the historical backfill until its durable end date."""

        previous_handlers: dict[signal.Signals, Any] = {}

        def handle_signal(signum: int, _frame: FrameType | None) -> None:
            self.request_stop(f"received signal {signal.Signals(signum).name}")

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, handle_signal)

        windows_completed = discovered = queued = completed = failed = retired = 0
        errors: list[str] = []
        try:
            with RunLock(self.config.effective_lock_file):
                self._prepare_state()
                history_start, history_end, cursor = self._configure_history()
                total_days = (history_end - history_start).total_seconds() / 86400
                initial_days = (cursor - history_start).total_seconds() / 86400
                progress = tqdm(
                    total=total_days,
                    initial=initial_days,
                    desc="Historical backfill",
                    unit="day",
                    dynamic_ncols=True,
                    disable=not self.history_config.progress,
                )
                try:
                    while cursor < history_end and not self.stop_event.is_set():
                        window_end = min(
                            history_end,
                            cursor + timedelta(days=self.history_config.window_days),
                        )
                        progress.set_postfix(
                            window=f"{cursor.date()}..{window_end.date()}"
                        )
                        result = self._run_window(cursor, window_end)
                        discovered += result.discovered
                        queued += result.queued
                        completed += result.completed
                        failed += result.failed
                        retired += result.retired
                        errors.extend(result.errors)
                        if not result.advanced:
                            break
                        self.state.set_setting("history_cursor", as_utc_text(window_end))
                        progress.update((window_end - cursor).total_seconds() / 86400)
                        cursor = window_end
                        windows_completed += 1
                finally:
                    progress.close()
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)

        return HistoricalRunResult(
            windows_completed=windows_completed,
            discovered=discovered,
            queued=queued,
            completed=completed,
            failed=failed,
            retired=retired,
            stopped=self.stop_event.is_set(),
            errors=tuple(errors),
        )
