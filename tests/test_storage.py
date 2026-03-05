"""Tests for terravault.storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pystac

from terravault.storage import StorageManager, _tile_id_from_item, _item_date


def _make_item(
    item_id: str = "S2A_MSIL2A_20240601T100559",
    dt: datetime | None = None,
    mgrs_tile: str | None = "32TNT",
) -> pystac.Item:
    """Create a minimal STAC item stub."""
    if dt is None:
        dt = datetime(2024, 6, 1, 10, 5, 59, tzinfo=timezone.utc)
    props: dict = {"datetime": dt.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if mgrs_tile:
        props["s2:mgrs_tile"] = mgrs_tile

    item = MagicMock(spec=pystac.Item)
    item.id = item_id
    item.properties = props
    item.datetime = dt
    item.to_dict.return_value = {
        "type": "Feature",
        "id": item_id,
        "properties": props,
        "geometry": None,
        "links": [],
        "assets": {},
        "stac_version": "1.0.0",
    }
    return item


class TestTileIdExtraction(unittest.TestCase):
    def test_s2_mgrs_tile_property(self):
        item = _make_item(mgrs_tile="32TNT")
        self.assertEqual(_tile_id_from_item(item), "32TNT")

    def test_fallback_to_item_id(self):
        item = _make_item(mgrs_tile=None)
        item.properties = {}
        self.assertEqual(_tile_id_from_item(item), item.id)

    def test_mgrs_components(self):
        item = _make_item(mgrs_tile=None)
        item.properties = {
            "mgrs:utm_zone": "32",
            "mgrs:latitude_band": "T",
            "mgrs:grid_square": "NT",
        }
        self.assertEqual(_tile_id_from_item(item), "32TNT")


class TestItemDate(unittest.TestCase):
    def test_returns_item_datetime(self):
        dt = datetime(2024, 6, 1, tzinfo=timezone.utc)
        item = _make_item(dt=dt)
        self.assertEqual(_item_date(item), dt)

    def test_raises_when_no_datetime(self):
        item = _make_item()
        item.datetime = None
        item.properties = {}
        with self.assertRaises(ValueError):
            _item_date(item)


class TestStorageManager(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = StorageManager(root=self.tmpdir, mission="sentinel2")

    def test_scene_dir_structure(self):
        item = _make_item(dt=datetime(2024, 6, 15, tzinfo=timezone.utc), mgrs_tile="32TNT")
        scene_dir = self.mgr.scene_dir(item)
        expected = Path(self.tmpdir) / "sentinel2" / "2024" / "06" / "15" / "32TNT"
        self.assertEqual(scene_dir, expected)
        self.assertTrue(scene_dir.is_dir())

    def test_metadata_path(self):
        item = _make_item()
        path = self.mgr.metadata_path(item)
        self.assertEqual(path.name, "scene_metadata.json")

    def test_save_and_load_metadata(self):
        item = _make_item()
        saved_path = self.mgr.save_metadata(item)
        self.assertTrue(saved_path.exists())

        loaded = self.mgr.load_metadata(item)
        self.assertEqual(loaded["id"], item.id)

    def test_metadata_exists_after_save(self):
        item = _make_item()
        self.assertFalse(self.mgr.metadata_exists(item))
        self.mgr.save_metadata(item)
        self.assertTrue(self.mgr.metadata_exists(item))

    def test_asset_path_default_suffix(self):
        item = _make_item()
        path = self.mgr.asset_path(item, "B04")
        self.assertEqual(path.suffix, ".tif")
        self.assertEqual(path.name, "B04.tif")

    def test_asset_path_custom_suffix(self):
        item = _make_item()
        path = self.mgr.asset_path(item, "thumbnail", suffix=".png")
        self.assertEqual(path.name, "thumbnail.png")

    def test_save_metadata_overwrites_existing(self):
        item = _make_item()
        self.mgr.save_metadata(item)
        # Mutate the mock's to_dict return value and save again
        item.to_dict.return_value["properties"]["updated"] = True
        self.mgr.save_metadata(item)
        loaded = self.mgr.load_metadata(item)
        self.assertTrue(loaded["properties"].get("updated"))


if __name__ == "__main__":
    unittest.main()
