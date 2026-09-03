"""Tests for direct Copernicus monthly scene selection and dry runs."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from terravault.copernicus_monthly import (
    CopernicusMonthlyConfig,
    CopernicusMonthlyProcessor,
    select_copernicus_scenes,
)
from terravault.rolling import VEGETATION_L2A_ASSET_KEYS


def _item_pieces(
    root: Path,
    *,
    item_id: str,
    tile_id: str = "32TMT",
    acquired: datetime = datetime(2024, 6, 15, 10, 0, tzinfo=timezone.utc),
    cloud_cover: float | None = 12.5,
) -> list[dict[str, Any]]:
    pieces: list[dict[str, Any]] = []
    for key in VEGETATION_L2A_ASSET_KEYS:
        local_path = root / item_id / f"{key}.jp2"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"local-test-asset")
        pieces.append(
            {
                "item_id": item_id,
                "asset_key": key,
                "local_path": str(local_path),
                "acquisition_time": acquired,
                "tile_id": tile_id,
                "cloud_cover": cloud_cover,
                "nodata": 0,
                "raster_scale": 0.0001,
                "raster_offset": 0.0,
            }
        )
    return pieces


def test_scene_selection_keeps_complete_items_grouped_and_sorted(tmp_path: Path):
    later = _item_pieces(
        tmp_path,
        item_id="later",
        acquired=datetime(2024, 6, 20, 10, 0, tzinfo=timezone.utc),
    )
    earlier = _item_pieces(
        tmp_path,
        item_id="earlier",
        acquired=datetime(2024, 6, 3, 10, 0),
        cloud_cover=None,
    )
    cloudy = _item_pieces(tmp_path, item_id="cloudy", tile_id="32TNT", cloud_cover=45.0)
    incomplete = _item_pieces(tmp_path, item_id="incomplete", tile_id="32TNT")
    incomplete = [piece for piece in incomplete if piece["asset_key"] != "CLD_20m"]
    missing_file = _item_pieces(tmp_path, item_id="missing-file", tile_id="32TNT")
    Path(missing_file[0]["local_path"]).unlink()

    selected, skipped = select_copernicus_scenes(
        [*later, *cloudy, *incomplete, *earlier, *missing_file],
        max_cloud_cover=20.0,
    )

    assert list(selected) == ["32TMT"]
    assert [scene.item_id for scene in selected["32TMT"]] == ["earlier", "later"]
    assert selected["32TMT"][0].acquisition_time.tzinfo == timezone.utc
    assert tuple(selected["32TMT"][0].assets) == VEGETATION_L2A_ASSET_KEYS
    assert skipped["cloudy"] == ("cloud-cover:45",)
    assert skipped["incomplete"] == ("missing:CLD_20m",)
    assert skipped["missing-file"] == ("missing-file:B03_10m",)


def test_copernicus_monthly_dry_run_records_selection_and_timings(
    monkeypatch,
    tmp_path: Path,
):
    pieces = _item_pieces(tmp_path / "assets", item_id="scene-a")
    database_path = tmp_path / "dataset.duckdb"
    database_path.write_bytes(b"catalog-placeholder")
    calls: list[dict[str, Any]] = []

    class FakeDatasetCatalog:
        def __init__(self, path: Path) -> None:
            assert path == database_path

        def query_raster_pieces(self, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append(kwargs)
            return pieces

    monkeypatch.setattr(
        "terravault.copernicus_monthly.DatasetCatalog",
        FakeDatasetCatalog,
    )
    result = CopernicusMonthlyProcessor(
        CopernicusMonthlyConfig(
            dataset_db=database_path,
            output_root=tmp_path / "monthly",
            year=2024,
            month=6,
            bbox=(8.0, 46.0, 8.01, 46.01),
            max_cloud_cover=20.0,
            dry_run=True,
        )
    ).run()

    assert result.dry_run is True
    assert result.scene_count == 1
    assert not result.reflectance_path.exists()
    assert not result.indices_path.exists()
    assert len(calls) == 1
    assert calls[0]["start_datetime"] == datetime(2024, 6, 1, tzinfo=timezone.utc)
    assert calls[0]["end_datetime"] == datetime(
        2024,
        6,
        30,
        23,
        59,
        59,
        999999,
        tzinfo=timezone.utc,
    )
    assert calls[0]["asset_keys"] == VEGETATION_L2A_ASSET_KEYS

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["processing_option"] == "direct-copernicus-monthly-composite"
    assert manifest["source"] == "copernicus-sentinel-2-l2a"
    assert manifest["source_is_force"] is False
    assert manifest["source_is_swisseo"] is False
    assert manifest["scene_count"] == 1
    assert manifest["tile_count"] == 1
    assert manifest["average_catalog_cloud_cover_pct"] == 12.5
    assert manifest["native_input_assets"] == list(VEGETATION_L2A_ASSET_KEYS)
    assert manifest["timings_seconds"]["monthly_mosaic_generation"] == 0.0
    assert manifest["timings_seconds"]["index_calculation"] == 0.0
    assert manifest["timings_seconds"]["total"] >= 0.0
    transforms = manifest["sources"][0]["physical_reflectance_transforms"]
    assert transforms["B03_10m"] == {"scale": 0.0001, "offset": 0.0}


def test_copernicus_reflectance_transform_fails_closed_without_metadata():
    with pytest.raises(ValueError, match="requires explicit raster scale and offset"):
        CopernicusMonthlyProcessor._physical_transform(
            {"item_id": "scene", "asset_key": "B04_10m"}
        )


@pytest.mark.skipif(
    any(
        shutil.which(command) is None
        for command in ("gdal_create", "gdal_calc.py", "gdalwarp", "gdalinfo")
    ),
    reason="GDAL command-line tools are not installed",
)
def test_copernicus_monthly_runs_end_to_end_on_small_aligned_scenes(
    monkeypatch,
    tmp_path: Path,
):
    database_path = tmp_path / "dataset.duckdb"
    database_path.write_bytes(b"catalog-placeholder")
    band_values = {
        "B03_10m": 4000,
        "B04_10m": 3000,
        "B05_20m": 3500,
        "B08_10m": 7000,
        "B8A_20m": 6800,
        "B11_20m": 4500,
        "SCL_20m": 4,
        "CLD_20m": 10,
    }
    pieces: list[dict[str, Any]] = []
    for scene_number in (1, 2):
        item_id = f"scene-{scene_number}"
        for key in VEGETATION_L2A_ASSET_KEYS:
            path = tmp_path / "assets" / item_id / f"{key}.tif"
            path.parent.mkdir(parents=True, exist_ok=True)
            value = band_values[key]
            if key not in {"SCL_20m", "CLD_20m"}:
                value += (scene_number - 1) * 200
            if scene_number == 2 and key == "B03_10m":
                # The L2A offset transforms this to -0.05. One nonphysical
                # required band must invalidate the complete acquisition pixel
                # instead of destabilizing the vegetation-index denominator.
                value = 500
            subprocess.run(
                [
                    "gdal_create",
                    "-of",
                    "GTiff",
                    "-ot",
                    "UInt16",
                    "-outsize",
                    "100",
                    "50",
                    "-bands",
                    "1",
                    "-burn",
                    str(value),
                    "-a_srs",
                    "EPSG:4326",
                    "-a_ullr",
                    "7.0",
                    "47.0",
                    "8.0",
                    "46.5",
                    "-a_nodata",
                    "0",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            is_reflectance = key not in {"SCL_20m", "CLD_20m"}
            pieces.append(
                {
                    "item_id": item_id,
                    "asset_key": key,
                    "local_path": str(path),
                    "acquisition_time": datetime(
                        2024,
                        6,
                        5 + scene_number,
                        10,
                        tzinfo=timezone.utc,
                    ),
                    "tile_id": "32TMT",
                    "cloud_cover": 10.0,
                    "nodata": 0,
                    "raster_scale": 0.0001 if is_reflectance else None,
                    "raster_offset": -0.1 if is_reflectance else None,
                }
            )

    class FakeDatasetCatalog:
        def __init__(self, path: Path) -> None:
            assert path == database_path

        def query_raster_pieces(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return pieces

    monkeypatch.setattr(
        "terravault.copernicus_monthly.DatasetCatalog",
        FakeDatasetCatalog,
    )
    config = CopernicusMonthlyConfig(
        dataset_db=database_path,
        output_root=tmp_path / "monthly",
        year=2024,
        month=6,
        bbox=(7.0, 46.5, 8.0, 47.0),
        resolution=500.0,
        max_output_gib=1.0,
        staging_root=tmp_path / "staging",
        overwrite=True,
    )
    result = CopernicusMonthlyProcessor(config).run()

    assert result.reflectance_path.is_file()
    assert result.indices_path.is_file()
    assert result.scene_count == 2
    assert result.timings_seconds["monthly_mosaic_generation"] > 0
    assert result.timings_seconds["index_calculation"] > 0
    info = json.loads(
        subprocess.run(
            ["gdalinfo", "-json", "-stats", str(result.indices_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    expected = (0.4 / 0.8, 0.3 / 0.9, 0.33 / 0.83, 0.25 / 0.95)
    assert [band["mean"] for band in info["bands"]] == pytest.approx(expected, abs=5e-4)

    # Staging roots intentionally retain finished partial COGs. An explicit
    # overwrite must clear those before the next resumable run.
    second = CopernicusMonthlyProcessor(config).run()
    assert second.reflectance_path.is_file()
    assert second.indices_path.is_file()
