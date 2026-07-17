"""Tests for durable rolling ingestion state."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from terravault.rolling_state import RollingState


def _insert_job(state: RollingState, item_id: str = "scene-1") -> None:
    state.upsert_job(
        item_id=item_id,
        item_datetime=datetime(2026, 7, 4, tzinfo=timezone.utc),
        collection_id="sentinel-2-l2a",
        metadata_path=f"/data/{item_id}/scene_metadata.json",
        status_path=f"/data/{item_id}/job_status.json",
        requested_assets=["B04_10m"],
        missing_assets=[],
    )
    state.upsert_asset(
        item_id=item_id,
        asset_key="B04_10m",
        href=f"s3://eodata/{item_id}/B04.jp2",
        local_path=f"/data/{item_id}/B04.jp2",
    )


def test_database_is_bound_to_one_roi(tmp_path):
    state = RollingState(tmp_path / "rolling.db")
    state.configure_roi("fingerprint-a", '{"type":"Polygon"}')
    state.configure_roi("fingerprint-a", '{"type":"Polygon"}')
    with pytest.raises(ValueError, match="different ROI"):
        state.configure_roi("fingerprint-b", '{"type":"Polygon"}')
    state.close()


def test_interrupted_processing_is_requeued(tmp_path):
    state = RollingState(tmp_path / "rolling.db")
    _insert_job(state)
    assert state.claim_job("scene-1")
    state.mark_asset_processing("scene-1", "B04_10m")

    jobs, assets = state.recover_interrupted()

    assert (jobs, assets) == (1, 1)
    assert state.due_jobs(limit=10)[0]["status"] == "queued"
    assert state.assets_for_job("scene-1")[0]["status"] == "queued"
    state.close()


def test_complete_job_snapshot_has_asset_provenance(tmp_path):
    state = RollingState(tmp_path / "rolling.db")
    _insert_job(state)
    state.mark_asset_complete(
        "scene-1",
        "B04_10m",
        byte_count=42,
        sha256="a" * 64,
    )
    state.mark_job_complete("scene-1")
    state.record_event(
        level="INFO",
        item_id="scene-1",
        asset_key="B04_10m",
        message="complete",
    )

    snapshot = state.job_snapshot("scene-1")

    assert snapshot["status"] == "completed"
    assert snapshot["assets"][0]["bytes"] == 42
    assert snapshot["assets"][0]["sha256"] == "a" * 64
    assert snapshot["recent_events"][0]["message"] == "complete"
    state.close()


def test_operator_can_requeue_terminal_failure(tmp_path):
    state = RollingState(tmp_path / "rolling.db")
    _insert_job(state)
    state.mark_asset_retry("scene-1", "B04_10m", "network failure")
    state.mark_job_retry(
        "scene-1",
        error="network failure",
        next_attempt_at=None,
        terminal=True,
    )

    jobs, assets = state.requeue_failed_jobs()

    assert (jobs, assets) == (1, 1)
    assert state.due_jobs(limit=10)[0]["attempts"] == 0
    assert state.assets_for_job("scene-1")[0]["status"] == "queued"
    state.close()


def test_bootstrap_ignored_items_are_durable(tmp_path):
    state = RollingState(tmp_path / "rolling.db")
    assert not state.is_ignored("older-scene")
    state.ignore_item("older-scene", "superseded during bootstrap")
    assert state.is_ignored("older-scene")
    state.close()

    reopened = RollingState(tmp_path / "rolling.db")
    assert reopened.is_ignored("older-scene")
    reopened.close()
