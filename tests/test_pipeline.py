"""Tests for terravault.pipeline."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pystac

from terravault.pipeline import Pipeline, PipelineConfig, PipelineRunResult
from terravault.downloader import DownloadResult


def _make_item(item_id: str = "item-1", cloud_cover: float = 5.0) -> pystac.Item:
    dt = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    item = MagicMock(spec=pystac.Item)
    item.id = item_id
    item.datetime = dt
    item.properties = {
        "eo:cloud_cover": cloud_cover,
        "s2:mgrs_tile": "32TNT",
        "datetime": "2024-06-01T10:00:00Z",
    }
    item.assets = {}
    item.to_dict.return_value = {
        "type": "Feature",
        "id": item_id,
        "properties": item.properties,
        "geometry": None,
        "links": [],
        "assets": {},
        "stac_version": "1.0.0",
    }
    return item


class TestPipelineRunMetadataOnly(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_run_no_download_processes_new_items(self):
        item = _make_item("item-1")

        cfg = PipelineConfig(
            storage_root=self.tmpdir,
            state_db=":memory:",
            lookback_hours=24,
        )
        pipeline = Pipeline(cfg)

        # Patch catalog to yield our test item
        pipeline._catalog.search = MagicMock(return_value=iter([item]))

        result = pipeline.run(download=False)

        self.assertEqual(result.items_discovered, 1)
        self.assertEqual(result.items_processed, 1)
        self.assertEqual(result.items_skipped_duplicate, 0)
        self.assertEqual(len(result.download_results), 0)

    def test_run_skips_already_ingested_items(self):
        item = _make_item("item-dup")

        cfg = PipelineConfig(
            storage_root=self.tmpdir,
            state_db=":memory:",
            lookback_hours=24,
        )
        pipeline = Pipeline(cfg)

        # Mark item as already ingested
        pipeline._state.mark_processed(item.id, item.datetime)

        pipeline._catalog.search = MagicMock(return_value=iter([item]))
        result = pipeline.run(download=False)

        self.assertEqual(result.items_discovered, 1)
        self.assertEqual(result.items_skipped_duplicate, 1)
        self.assertEqual(result.items_processed, 0)

    def test_run_with_multiple_items(self):
        items = [_make_item(f"item-{i}") for i in range(5)]

        cfg = PipelineConfig(
            storage_root=self.tmpdir,
            state_db=":memory:",
        )
        pipeline = Pipeline(cfg)
        pipeline._catalog.search = MagicMock(return_value=iter(items))

        result = pipeline.run(download=False)

        self.assertEqual(result.items_processed, 5)
        self.assertEqual(result.items_skipped_duplicate, 0)

    def test_run_updates_state_after_processing(self):
        item = _make_item("item-state-test")

        cfg = PipelineConfig(
            storage_root=self.tmpdir,
            state_db=":memory:",
        )
        pipeline = Pipeline(cfg)
        pipeline._catalog.search = MagicMock(return_value=iter([item]))
        pipeline.run(download=False)

        self.assertTrue(pipeline._state.is_ingested(item.id))


class TestPipelineRunResult(unittest.TestCase):
    def test_computed_properties(self):
        r = PipelineRunResult()
        r.download_results = [
            DownloadResult("i1", "B04", Path("a.tif"), success=True, skipped=False),
            DownloadResult("i1", "B08", Path("b.tif"), success=True, skipped=True),
            DownloadResult("i2", "B04", Path("c.tif"), success=False, error="err"),
        ]
        self.assertEqual(r.downloads_ok, 1)
        self.assertEqual(r.downloads_skipped, 1)
        self.assertEqual(r.downloads_failed, 1)


class TestPipelineConfig(unittest.TestCase):
    def test_default_collections(self):
        cfg = PipelineConfig()
        self.assertIn("sentinel-2-l2a", cfg.collections)
        self.assertIn("sentinel-2-l1c", cfg.collections)

    def test_custom_lookback(self):
        cfg = PipelineConfig(lookback_hours=48)
        self.assertEqual(cfg.lookback_hours, 48)


if __name__ == "__main__":
    unittest.main()
