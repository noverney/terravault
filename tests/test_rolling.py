"""End-to-end unit tests for the restartable rolling worker."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pystac
import pytest

from terravault.rolling import RunLock, RollingConfig, RollingIngestor, load_roi
from terravault.s3_downloader import S3DownloadResult
from terravault.spatial import geometry_area


class FakeCatalog:
    def __init__(self, items):
        self.items = items
        self.calls = 0
        self.search_calls = []

    def search(self, **kwargs):
        self.calls += 1
        self.search_calls.append(kwargs)
        return iter(self.items)


class FlakyDownloader:
    def __init__(self) -> None:
        self.calls = 0

    def download(self, _href, destination, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary test failure")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"native-data")
        return S3DownloadResult(
            path=destination,
            byte_count=11,
            sha256="b" * 64,
            resumed_from=0,
            skipped=False,
        )


def _item(
    with_asset: bool = False,
    *,
    tile: str = "32TNT",
    acquired: datetime | None = None,
    suffix: str = "20260704T130000",
) -> pystac.Item:
    acquired = acquired or datetime(2026, 7, 4, 10, 20, tzinfo=timezone.utc)
    item = pystac.Item(
        id=f"S2A_MSIL2A_20260704T102031_N0511_R065_T{tile}_{suffix}",
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
        item.add_asset("B04_10m", pystac.Asset("s3://eodata/scene/B04.jp2"))
    return item


def test_load_bbox_and_geojson_roi(tmp_path):
    bbox_roi = load_roi(bbox=[5.96, 45.82, 10.49, 47.81])
    assert bbox_roi.bbox == (5.96, 45.82, 10.49, 47.81)

    geojson = tmp_path / "roi.geojson"
    geojson.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {},
                "geometry": bbox_roi.geometry,
            }
        )
    )
    file_roi = load_roi(geojson_path=geojson)
    assert file_roi.fingerprint == bbox_roi.fingerprint

    with pytest.raises(ValueError, match="exactly one"):
        load_roi()


def test_metadata_only_run_is_idempotent_and_writes_status(tmp_path):
    item = _item()
    config = RollingConfig(
        roi=load_roi(bbox=[7.9, 45.9, 9.1, 47.1]),
        asset_profile="metadata-only",
        state_db=tmp_path / "rolling.db",
        storage_root=tmp_path / "data",
    )
    runner = RollingIngestor(config, catalog=FakeCatalog([item]))
    try:
        first = runner.run(once=True)
        second = runner.run(once=True)
        snapshot = runner.state.job_snapshot(item.id)
    finally:
        runner.close()

    assert first.discovered == 1
    assert first.queued == 1
    assert first.completed == 1
    assert not first.errors
    assert second.discovered == 1
    assert second.queued == 0
    assert second.completed == 0
    assert snapshot["status"] == "completed"
    status_path = next((tmp_path / "data").rglob("job_status.json"))
    on_disk = json.loads(status_path.read_text())
    assert on_disk["status"] == "completed"


def test_failed_asset_is_retried_and_completed_on_next_cycle(tmp_path):
    item = _item(with_asset=True)
    downloader = FlakyDownloader()
    config = RollingConfig(
        roi=load_roi(bbox=[7.9, 45.9, 9.1, 47.1]),
        asset_profile="custom",
        asset_keys=["B04_10m"],
        state_db=tmp_path / "rolling.db",
        storage_root=tmp_path / "data",
        retry_base_seconds=0,
    )
    runner = RollingIngestor(
        config,
        catalog=FakeCatalog([item]),
        downloader=downloader,
    )
    try:
        first = runner.run(once=True)
        first_snapshot = runner.state.job_snapshot(item.id)
        second = runner.run(once=True)
        second_snapshot = runner.state.job_snapshot(item.id)
    finally:
        runner.close()

    assert first.errors
    assert first_snapshot["status"] == "retry_wait"
    assert first_snapshot["assets"][0]["last_error"]
    assert second.completed == 1
    assert second_snapshot["status"] == "completed"
    assert second_snapshot["assets"][0]["sha256"] == "b" * 64


def test_overlapping_worker_does_not_recover_state_before_lock(tmp_path):
    config = RollingConfig(
        roi=load_roi(bbox=[7.9, 45.9, 9.1, 47.1]),
        asset_profile="metadata-only",
        state_db=tmp_path / "rolling.db",
        storage_root=tmp_path / "data",
    )
    runner = RollingIngestor(config, catalog=FakeCatalog([]))
    try:
        with RunLock(config.effective_lock_file):
            with pytest.raises(RuntimeError, match="Another rolling worker"):
                runner.run(once=True)
        assert runner.state.get_setting("roi_fingerprint") is None
    finally:
        runner.close()


def test_first_run_selects_latest_per_tile_then_follows_only_new_items(tmp_path):
    initial_time = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    old_tnt = _item(
        tile="32TNT",
        acquired=datetime(2026, 7, 13, tzinfo=timezone.utc),
        suffix="older",
    )
    latest_tnt = _item(
        tile="32TNT",
        acquired=datetime(2026, 7, 16, tzinfo=timezone.utc),
        suffix="latest",
    )
    latest_tmt = _item(
        tile="32TMT",
        acquired=datetime(2026, 7, 15, tzinfo=timezone.utc),
        suffix="other-tile",
    )
    catalog = FakeCatalog([old_tnt, latest_tnt, latest_tmt])
    clock = {"now": initial_time}
    config = RollingConfig(
        roi=load_roi(bbox=[5.96, 45.82, 10.49, 47.81]),
        asset_profile="metadata-only",
        state_db=tmp_path / "rolling.db",
        storage_root=tmp_path / "data",
    )
    runner = RollingIngestor(
        config,
        catalog=catalog,
        now=lambda: clock["now"],
    )
    try:
        first = runner.run(once=True)
        assert runner.state.is_ignored(old_tnt.id)
        with pytest.raises(KeyError):
            runner.state.job_snapshot(old_tnt.id)

        new_tnt = _item(
            tile="32TNT",
            acquired=datetime(2026, 7, 19, tzinfo=timezone.utc),
            suffix="new-after-bootstrap",
        )
        catalog.items = [old_tnt, latest_tnt, latest_tmt, new_tnt]
        clock["now"] = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        second = runner.run(once=True)
        new_snapshot = runner.state.job_snapshot(new_tnt.id)
    finally:
        runner.close()

    assert first.discovered == 3
    assert first.queued == 2
    assert first.completed == 2
    assert catalog.search_calls[0]["start_datetime"] == initial_time.replace(
        day=3
    )
    assert second.discovered == 4
    assert second.queued == 1
    assert second.completed == 1
    assert new_snapshot["status"] == "completed"
    assert catalog.search_calls[1]["start_datetime"] == initial_time.replace(
        day=14
    )


def test_bootstrap_uses_newest_near_full_footprint_not_newer_sliver(tmp_path):
    full = _item(
        tile="32TNT",
        acquired=datetime(2026, 7, 15, tzinfo=timezone.utc),
        suffix="full",
    )
    full.geometry = {
        "type": "Polygon",
        "coordinates": [
            [[8.0, 46.0], [9.0, 46.0], [9.0, 47.0], [8.0, 47.0], [8.0, 46.0]]
        ],
    }
    partial = _item(
        tile="32TNT",
        acquired=datetime(2026, 7, 16, tzinfo=timezone.utc),
        suffix="partial",
    )
    partial.geometry = {
        "type": "Polygon",
        "coordinates": [
            [[8.0, 46.0], [8.1, 46.0], [8.1, 47.0], [8.0, 47.0], [8.0, 46.0]]
        ],
    }
    config = RollingConfig(
        roi=load_roi(bbox=[7.9, 45.9, 9.1, 47.1]),
        asset_profile="metadata-only",
        state_db=tmp_path / "rolling.db",
        storage_root=tmp_path / "data",
    )
    runner = RollingIngestor(config, catalog=FakeCatalog([full, partial]))
    try:
        result = runner.run(once=True)
        full_snapshot = runner.state.job_snapshot(full.id)
        partial_ignored = runner.state.is_ignored(partial.id)
    finally:
        runner.close()

    assert geometry_area(full.geometry) == 1
    assert geometry_area(partial.geometry) == pytest.approx(0.1)
    assert result.queued == 1
    assert full_snapshot["status"] == "completed"
    assert partial_ignored
