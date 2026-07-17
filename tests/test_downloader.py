"""Tests for terravault.downloader."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pystac
import requests

from terravault.downloader import AssetDownloader, DownloadConfig
from terravault.storage import StorageManager


def _make_item(item_id: str = "test-item", assets: dict | None = None) -> pystac.Item:
    dt = datetime(2024, 6, 1, tzinfo=timezone.utc)
    item = MagicMock(spec=pystac.Item)
    item.id = item_id
    item.datetime = dt
    item.properties = {"s2:mgrs_tile": "32TNT", "datetime": "2024-06-01T00:00:00Z"}

    if assets is None:
        mock_asset = MagicMock(spec=pystac.Asset)
        mock_asset.href = "https://example.com/data/B04.tif"
        assets = {"B04": mock_asset}
    item.assets = assets
    return item


class TestDownloadConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = DownloadConfig()
        self.assertEqual(cfg.max_workers, 4)
        self.assertEqual(cfg.max_retries, 5)
        self.assertEqual(cfg.asset_keys, [])


class TestAssetDownloaderSkipsExisting(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.storage = StorageManager(root=self.tmpdir)

    def test_skips_if_file_exists(self):
        item = _make_item()
        # Pre-create the expected asset file
        scene_dir = self.storage.scene_dir(item)
        existing = scene_dir / "B04.tif"
        existing.write_bytes(b"fake data")

        cfg = DownloadConfig(asset_keys=["B04"])
        dl = AssetDownloader(storage=self.storage, config=cfg)
        results = dl.download_item(item)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].skipped)
        self.assertTrue(results[0].success)


class TestAssetDownloaderMissingHref(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.storage = StorageManager(root=self.tmpdir)

    def test_returns_failure_for_empty_href(self):
        mock_asset = MagicMock(spec=pystac.Asset)
        mock_asset.href = None
        item = _make_item(assets={"B04": mock_asset})

        cfg = DownloadConfig(asset_keys=["B04"])
        dl = AssetDownloader(storage=self.storage, config=cfg)
        results = dl.download_item(item)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertIn("no href", results[0].error.lower())

    def test_returns_failure_for_s3_href(self):
        mock_asset = MagicMock(spec=pystac.Asset)
        mock_asset.href = "s3://eodata/Sentinel-2/test.jp2"
        item = _make_item(assets={"B04": mock_asset})

        cfg = DownloadConfig(asset_keys=["B04"])
        dl = AssetDownloader(storage=self.storage, config=cfg)
        results = dl.download_item(item)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertIn("s3", results[0].error.lower())


class TestAssetDownloaderNetworkSuccess(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.storage = StorageManager(root=self.tmpdir)

    def test_successful_download_writes_file(self):
        item = _make_item()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.iter_content.return_value = [b"binary", b"data"]
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get.return_value = mock_response

        cfg = DownloadConfig(asset_keys=["B04"])
        dl = AssetDownloader(
            storage=self.storage,
            config=cfg,
            session_factory=lambda: mock_session,
        )
        results = dl.download_item(item)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertFalse(results[0].skipped)
        self.assertTrue(results[0].local_path.exists())

    def test_http_error_produces_failure_result(self):
        item = _make_item()

        mock_session = MagicMock()
        mock_session.get.side_effect = requests.ConnectionError("timeout")

        cfg = DownloadConfig(asset_keys=["B04"], max_retries=1, backoff_base=0)
        dl = AssetDownloader(
            storage=self.storage,
            config=cfg,
            session_factory=lambda: mock_session,
        )
        results = dl.download_item(item)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertIsNotNone(results[0].error)


class TestAssetKeyFiltering(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.storage = StorageManager(root=self.tmpdir)

    def test_only_configured_keys_are_downloaded(self):
        a1 = MagicMock(spec=pystac.Asset)
        a1.href = "https://example.com/B04.tif"
        a2 = MagicMock(spec=pystac.Asset)
        a2.href = "https://example.com/B08.tif"
        a3 = MagicMock(spec=pystac.Asset)
        a3.href = "https://example.com/B11.tif"

        item = _make_item(assets={"B04": a1, "B08": a2, "B11": a3})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.iter_content.return_value = [b"data"]
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response

        cfg = DownloadConfig(asset_keys=["B04", "B08"])
        dl = AssetDownloader(
            storage=self.storage,
            config=cfg,
            session_factory=lambda: mock_session,
        )
        results = dl.download_item(item)

        downloaded_keys = {r.asset_key for r in results}
        self.assertEqual(downloaded_keys, {"B04", "B08"})
        self.assertNotIn("B11", downloaded_keys)

    def test_missing_requested_key_is_a_failure(self):
        item = _make_item()
        cfg = DownloadConfig(asset_keys=["does-not-exist"])
        dl = AssetDownloader(storage=self.storage, config=cfg)

        results = dl.download_item(item)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertIn("not present", results[0].error)


if __name__ == "__main__":
    unittest.main()
