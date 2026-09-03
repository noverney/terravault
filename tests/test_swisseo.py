"""Tests for direct processing of one swissEO orbit mosaic."""

from __future__ import annotations

import json
from pathlib import Path

from terravault.swisseo import (
    SWISSEO_COLLECTION,
    SWISSEO_REFLECTANCE_SCALE,
    SWISSEO_STAC_URL,
    SwissEOImageConfig,
    SwissEOImageProcessor,
    discover_swisseo_month,
    swisseo_band_map,
)


def _item() -> dict:
    return {
        "stac_version": "1.0.0",
        "type": "Feature",
        "id": "2024-06-04t101559",
        "collection": SWISSEO_COLLECTION,
        "bbox": [6.35, 45.73, 10.67, 47.91],
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[6.35, 45.73], [10.67, 45.73], [10.67, 47.91], [6.35, 47.91], [6.35, 45.73]]],
        },
        "properties": {"datetime": "2024-06-04T10:15:59Z"},
        # Keys are intentionally opaque; the implementation must use title.
        "assets": {
            "dynamic-a": {"title": "BANDS-10M", "href": "https://example/bands10.tif"},
            "dynamic-b": {"title": "BANDS-20M", "href": "https://example/bands20.tif"},
            "dynamic-c": {"title": "MASKS-10M", "href": "https://example/masks.tif"},
            "dynamic-d": {"title": "METADATA", "href": "https://example/metadata.json"},
        },
    }


def _metadata() -> dict:
    return {
        "BANDS-10M": {
            "BANDS": [
                {"id": "B4", "dimensions": [200, 100]},
                {"id": "B3", "dimensions": [200, 100]},
                {"id": "B2", "dimensions": [200, 100]},
                {"id": "B8", "dimensions": [200, 100]},
            ],
            "SOURCE_COLLECTION_PROPERTIES": {
                "scene_count": 11,
                "percent_data": 31.6,
                "percent_masked": 68.4,
                "SENSING_ORBIT_NUMBER": 65,
            },
        },
        "BANDS-20M": {
            # This is the actual per-item order from swissEO metadata and is
            # deliberately different from descriptive prose on the product page.
            "BANDS": [
                {"id": "B8A", "dimensions": [100, 50]},
                {"id": "B11", "dimensions": [100, 50]},
                {"id": "B5", "dimensions": [100, 50]},
            ]
        },
        "MASKS-10M": {
            "BANDS": [
                {"id": "terrainShadowMask", "dimensions": [200, 100]},
                {"id": "cloudAndCloudShadowMask", "dimensions": [200, 100]},
                {"id": "reg_confidence", "dimensions": [200, 100]},
            ]
        },
    }


class FakeClient:
    stac_url = SWISSEO_STAC_URL
    collection = SWISSEO_COLLECTION

    def get_item(self, _item_id: str) -> dict:
        return _item()

    def get_metadata(self, _item: dict) -> dict:
        return _metadata()

    def search_month(self, **_kwargs) -> tuple[dict, ...]:
        return (_item(),)


def test_band_mapping_comes_from_item_metadata_not_asset_order():
    assert swisseo_band_map(_metadata()) == {
        "B03": ("BANDS-10M", 2),
        "B04": ("BANDS-10M", 1),
        "B05": ("BANDS-20M", 3),
        "B08": ("BANDS-10M", 4),
        "B8A": ("BANDS-20M", 1),
        "B11": ("BANDS-20M", 2),
    }


def test_month_inventory_states_that_items_are_orbit_mosaics():
    result = discover_swisseo_month(year=2024, month=6, client=FakeClient())
    payload = result.to_dict()

    assert payload["stac_authentication_required"] is False
    assert payload["item_count"] == 1
    assert payload["items"][0]["required_assets_present"] is True
    assert "not guaranteed" in payload["coverage_note"]


def test_dry_run_is_one_swisseo_image_and_not_monthly_or_force(tmp_path: Path):
    result = SwissEOImageProcessor(
        SwissEOImageConfig(
            item_id="2024-06-04t101559",
            output_root=tmp_path,
            dry_run=True,
        ),
        client=FakeClient(),
    ).run()

    assert result.dry_run is True
    assert (result.width, result.height) == (100, 50)
    assert not result.reflectance_path.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["processing_option"] == "swisseo-published-image"
    assert manifest["source"] == "swissEO-S2-SR-published-orbit-mosaic"
    assert manifest["source_is_monthly_composite"] is False
    assert manifest["source_is_force"] is False
    assert manifest["source_is_treeai"] is False
    assert manifest["source_reflectance_scale"] == SWISSEO_REFLECTANCE_SCALE
    assert manifest["source_band_mapping"]["B05"] == {
        "asset_title": "BANDS-20M",
        "band": 3,
    }
    assert manifest["quality_policy"]["cloud_and_cloud_shadow_invalid_value"] == 1
    assert manifest["quality_policy"]["terrain_shadow_invalid_value"] == 100
    assert manifest["attribution"] == (
        "© swisstopo, Contains modified Copernicus Sentinel data 2024"
    )
