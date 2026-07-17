"""Tests for terravault.catalog."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pystac

from terravault.catalog import CatalogClient, SWITZERLAND_BBOX, DEFAULT_COLLECTIONS


def _make_item(
    item_id: str = "test-item",
    cloud_cover: float | None = 10.0,
    dt: datetime | None = None,
) -> pystac.Item:
    """Create a minimal STAC item for testing."""
    if dt is None:
        dt = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    props = {"datetime": dt.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if cloud_cover is not None:
        props["eo:cloud_cover"] = cloud_cover
    item = MagicMock(spec=pystac.Item)
    item.id = item_id
    item.properties = props
    item.datetime = dt
    return item


class TestCatalogClientInit(unittest.TestCase):
    def test_defaults(self):
        client = CatalogClient()
        self.assertEqual(client.bbox, SWITZERLAND_BBOX)
        self.assertEqual(client.collections, DEFAULT_COLLECTIONS)
        self.assertEqual(client.max_cloud_cover, 20.0)

    def test_custom_params(self):
        client = CatalogClient(
            catalog_url="https://example.com",
            collections=["sentinel-1-grd"],
            bbox=[6.0, 46.0, 10.0, 47.0],
            max_cloud_cover=None,
        )
        self.assertEqual(client.catalog_url, "https://example.com")
        self.assertIsNone(client.max_cloud_cover)

    def test_geojson_intersects_takes_precedence_over_bbox(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [[[8.0, 47.0], [8.1, 47.0], [8.1, 47.1], [8.0, 47.0]]],
        }
        client = CatalogClient(bbox=[6, 45, 11, 48], intersects=geometry)
        self.assertIsNone(client.bbox)
        self.assertEqual(client.intersects, geometry)


class TestFormatDatetimeInterval(unittest.TestCase):
    def test_utc_datetimes(self):
        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, 12, 30, 0, tzinfo=timezone.utc)
        result = CatalogClient._format_datetime_interval(start, end)
        self.assertEqual(result, "2024-01-01T00:00:00Z/2024-01-02T12:30:00Z")

    def test_naive_datetimes_treated_as_utc(self):
        start = datetime(2024, 3, 1, 8, 0, 0)
        end = datetime(2024, 3, 2, 8, 0, 0)
        result = CatalogClient._format_datetime_interval(start, end)
        self.assertIn("Z", result)

    def test_aware_datetimes_are_converted_to_utc(self):
        from datetime import timedelta

        plus_two = timezone(timedelta(hours=2))
        result = CatalogClient._format_datetime_interval(
            datetime(2024, 1, 1, 2, tzinfo=plus_two),
            datetime(2024, 1, 2, 2, tzinfo=plus_two),
        )
        self.assertEqual(result, "2024-01-01T00:00:00Z/2024-01-02T00:00:00Z")


class TestCloudCoverFilter(unittest.TestCase):
    def _run_search(self, items, max_cloud_cover):
        """Helper: patch _open_client and collect yielded items."""
        client = CatalogClient(max_cloud_cover=max_cloud_cover)
        mock_search = MagicMock()
        mock_search.items.return_value = iter(items)
        mock_pystac_client = MagicMock()
        mock_pystac_client.search.return_value = mock_search

        with patch.object(client, "_open_client", return_value=mock_pystac_client):
            return list(
                client.search(
                    start_datetime=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    end_datetime=datetime(2024, 1, 2, tzinfo=timezone.utc),
                )
            )

    def test_items_below_threshold_pass(self):
        items = [_make_item("a", cloud_cover=5.0), _make_item("b", cloud_cover=15.0)]
        result = self._run_search(items, max_cloud_cover=20.0)
        self.assertEqual(len(result), 2)

    def test_items_above_threshold_filtered(self):
        items = [_make_item("a", cloud_cover=25.0), _make_item("b", cloud_cover=5.0)]
        result = self._run_search(items, max_cloud_cover=20.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "b")

    def test_no_filter_when_max_cloud_cover_is_none(self):
        items = [_make_item("a", cloud_cover=90.0), _make_item("b", cloud_cover=None)]
        result = self._run_search(items, max_cloud_cover=None)
        self.assertEqual(len(result), 2)

    def test_missing_cloud_cover_passes(self):
        """Items without eo:cloud_cover should not be filtered out."""
        items = [_make_item("a", cloud_cover=None)]
        result = self._run_search(items, max_cloud_cover=20.0)
        self.assertEqual(len(result), 1)

    def test_item_at_exact_threshold_passes(self):
        items = [_make_item("a", cloud_cover=20.0)]
        result = self._run_search(items, max_cloud_cover=20.0)
        self.assertEqual(len(result), 1)

    def test_empty_catalog_returns_empty(self):
        result = self._run_search([], max_cloud_cover=20.0)
        self.assertEqual(result, [])

    def test_search_passes_intersects_instead_of_bbox(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [[[8.0, 47.0], [8.1, 47.0], [8.1, 47.1], [8.0, 47.0]]],
        }
        client = CatalogClient(intersects=geometry)
        mock_search = MagicMock()
        mock_search.items.return_value = iter([])
        mock_stac_client = MagicMock()
        mock_stac_client.search.return_value = mock_search
        with patch.object(client, "_open_client", return_value=mock_stac_client):
            list(
                client.search(
                    datetime(2024, 1, 1, tzinfo=timezone.utc),
                    datetime(2024, 1, 2, tzinfo=timezone.utc),
                )
            )
        kwargs = mock_stac_client.search.call_args.kwargs
        self.assertEqual(kwargs["intersects"], geometry)
        self.assertNotIn("bbox", kwargs)


class TestLatestItem(unittest.TestCase):
    def test_returns_newest_item(self):
        items = [
            _make_item("older", dt=datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)),
            _make_item("newest", dt=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)),
            _make_item("middle", dt=datetime(2024, 6, 1, 11, 0, 0, tzinfo=timezone.utc)),
        ]
        client = CatalogClient()
        with patch.object(client, "search", return_value=iter(items)):
            latest = client.latest_item(
                start_datetime=datetime(2024, 6, 1, tzinfo=timezone.utc),
                end_datetime=datetime(2024, 6, 2, tzinfo=timezone.utc),
            )

        self.assertIsNotNone(latest)
        self.assertEqual(latest.id, "newest")

    def test_item_datetime_falls_back_to_properties(self):
        item = MagicMock(spec=pystac.Item)
        item.id = "fallback"
        item.datetime = None
        item.properties = {"datetime": "2024-06-01T12:34:56Z"}

        parsed = CatalogClient.item_datetime(item)
        self.assertEqual(parsed, datetime(2024, 6, 1, 12, 34, 56, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
