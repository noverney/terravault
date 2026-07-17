"""Tests for gradual historical backfill and quota retirement."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pystac

from terravault.cli import build_parser
from terravault.historical import (
    HistoricalConfig,
    HistoricalIngestor,
    parse_utc_date,
)
from terravault.rolling import RollingConfig, load_roi
from terravault.s3_downloader import QuotaExceededError, S3DownloadResult


def _history_item(
    item_id: str,
    acquired: datetime,
    *,
    with_asset: bool = False,
) -> pystac.Item:
    item = pystac.Item(
        id=item_id,
        geometry={
            "type": "Polygon",
            "coordinates": [[[8, 47], [9, 47], [9, 46], [8, 47]]],
        },
        bbox=[8, 46, 9, 47],
        datetime=acquired,
        properties={},
        collection="sentinel-2-l2a",
    )
    if with_asset:
        item.add_asset("B04_10m", pystac.Asset(f"s3://eodata/{item_id}/B04.jp2"))
    return item


class WindowCatalog:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def search(self, *, start_datetime, end_datetime, **_kwargs):
        self.calls.append((start_datetime, end_datetime))
        return iter(
            item
            for item in self.items
            if start_datetime <= item.datetime <= end_datetime
        )


class QuotaThenSuccessDownloader:
    def __init__(self, quota_failures: int) -> None:
        self.quota_failures = quota_failures
        self.calls = 0

    def download(self, _href, destination, **_kwargs):
        self.calls += 1
        if self.calls <= self.quota_failures:
            raise QuotaExceededError(
                "test quota exhausted",
                retry_after_seconds=0,
                error_code="SlowDown",
                status_code=429,
            )
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"historical")
        return S3DownloadResult(
            path=destination,
            byte_count=10,
            sha256="c" * 64,
            resumed_from=0,
            skipped=False,
        )


def _historical_config(
    tmp_path,
    *,
    asset_profile="metadata-only",
    asset_keys=None,
    max_attempts=3,
) -> HistoricalConfig:
    rolling = RollingConfig(
        roi=load_roi(bbox=[7.9, 45.9, 9.1, 47.1]),
        asset_profile=asset_profile,
        asset_keys=asset_keys or [],
        state_db=tmp_path / "history.db",
        storage_root=tmp_path / "data",
        max_attempts=max_attempts,
        quota_retry_seconds=0,
    )
    return HistoricalConfig(
        rolling=rolling,
        start_datetime=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_datetime=datetime(2024, 1, 3, tzinfo=timezone.utc),
        window_days=1,
        progress=False,
    )


def test_parse_dates_and_minimal_cli():
    assert parse_utc_date("2024-01-01") == datetime(
        2024, 1, 1, tzinfo=timezone.utc
    )
    assert parse_utc_date("2024-01-02", inclusive_end=True) == datetime(
        2024, 1, 3, tzinfo=timezone.utc
    )
    args = build_parser().parse_args(
        [
            "historic",
            "--bbox",
            "5.96",
            "45.82",
            "10.49",
            "47.81",
            "--start-date",
            "2024-01-01",
        ]
    )
    assert args.asset_profile == "native"
    assert args.window_days == 1
    assert args.max_attempts == 8

    ndvi_args = build_parser().parse_args(
        [
            "historic",
            "--bbox",
            "5.96",
            "45.82",
            "10.49",
            "47.81",
            "--start-date",
            "2024-01-01",
            "--asset-profile",
            "ndvi",
        ]
    )
    assert ndvi_args.asset_profile == "ndvi"


def test_historical_windows_resume_from_durable_cursor(tmp_path):
    items = [
        _history_item(
            "S2A_T32TNT_day1",
            datetime(2024, 1, 1, 12, tzinfo=timezone.utc),
        ),
        _history_item(
            "S2A_T32TNT_day2",
            datetime(2024, 1, 2, 12, tzinfo=timezone.utc),
        ),
    ]
    catalog = WindowCatalog(items)
    config = _historical_config(tmp_path)
    runner = HistoricalIngestor(config, catalog=catalog)
    try:
        first = runner.run_history()
    finally:
        runner.close()

    resumed = HistoricalIngestor(config, catalog=catalog)
    try:
        second = resumed.run_history()
    finally:
        resumed.close()

    assert first.windows_completed == 2
    assert first.discovered == 2
    assert first.completed == 2
    assert not first.errors
    assert second.windows_completed == 0
    assert second.discovered == 0


def test_quota_wait_then_success(tmp_path):
    item = _history_item(
        "S2A_T32TNT_quota_then_ok",
        datetime(2024, 1, 1, 12, tzinfo=timezone.utc),
        with_asset=True,
    )
    downloader = QuotaThenSuccessDownloader(quota_failures=1)
    config = _historical_config(
        tmp_path,
        asset_profile="custom",
        asset_keys=["B04_10m"],
        max_attempts=3,
    )
    runner = HistoricalIngestor(
        config,
        catalog=WindowCatalog([item]),
        downloader=downloader,
    )
    try:
        result = runner.run_history()
        snapshot = runner.state.job_snapshot(item.id)
    finally:
        runner.close()

    assert result.completed == 1
    assert result.retired == 0
    assert downloader.calls == 2
    assert snapshot["status"] == "completed"
    assert any("quota" in event["message"] for event in snapshot["recent_events"])


def test_quota_job_is_retired_after_attempt_ceiling(tmp_path):
    item = _history_item(
        "S2A_T32TNT_retired",
        datetime(2024, 1, 1, 12, tzinfo=timezone.utc),
        with_asset=True,
    )
    downloader = QuotaThenSuccessDownloader(quota_failures=99)
    config = _historical_config(
        tmp_path,
        asset_profile="custom",
        asset_keys=["B04_10m"],
        max_attempts=2,
    )
    runner = HistoricalIngestor(
        config,
        catalog=WindowCatalog([item]),
        downloader=downloader,
    )
    try:
        result = runner.run_history()
        snapshot = runner.state.job_snapshot(item.id)
    finally:
        runner.close()

    assert result.retired == 1
    assert downloader.calls == 2
    assert snapshot["status"] == "retired"
    assert snapshot["assets"][0]["status"] == "retired"

    retry_config = _historical_config(
        tmp_path,
        asset_profile="custom",
        asset_keys=["B04_10m"],
        max_attempts=2,
    )
    retry_config.rolling.retry_failed = True
    retry_runner = HistoricalIngestor(
        retry_config,
        catalog=WindowCatalog([item]),
        downloader=QuotaThenSuccessDownloader(quota_failures=0),
    )
    try:
        retried = retry_runner.run_history()
        retried_snapshot = retry_runner.state.job_snapshot(item.id)
    finally:
        retry_runner.close()

    assert retried.completed == 1
    assert retried_snapshot["status"] == "completed"
