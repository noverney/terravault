"""Tests for block-wise query extraction and COG mosaicking."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pystac
import pytest

from terravault.cli import build_parser, cmd_extract
from terravault.dataset_catalog import DatasetCatalog
from terravault.extractor import ExtractionConfig, RasterExtractor, _promote_dtype

GDAL_COMMANDS = ("gdal_create", "gdalwarp", "gdalbuildvrt", "gdal_translate", "gdalinfo")
HAS_GDAL = all(shutil.which(command) for command in GDAL_COMMANDS)


def _create_raster(
    path: Path,
    *,
    west: float,
    south: float,
    east: float,
    north: float,
    value: int,
) -> None:
    subprocess.run(
        [
            shutil.which("gdal_create") or "gdal_create",
            "-of",
            "GTiff",
            "-ot",
            "UInt16",
            "-outsize",
            "10",
            "10",
            "-burn",
            str(value),
            "-a_srs",
            "EPSG:4326",
            "-a_ullr",
            str(west),
            str(north),
            str(east),
            str(south),
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _index_raster(
    catalog: DatasetCatalog,
    *,
    item_id: str,
    tile_id: str,
    acquired: datetime,
    bbox: tuple[float, float, float, float],
    path: Path,
) -> None:
    west, south, east, north = bbox
    item = pystac.Item(
        id=item_id,
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        },
        bbox=list(bbox),
        datetime=acquired,
        properties={"s2:mgrs_tile": tile_id},
        collection="sentinel-2-l2a",
    )
    item.add_asset(
        "B04_10m",
        pystac.Asset(
            path.resolve().as_uri(),
            media_type="image/tiff; application=geotiff",
            roles=["data"],
            extra_fields={
                "proj:epsg": 4326,
                "gsd": 10,
                "nodata": 0,
                "data_type": "uint16",
                "raster:scale": 0.0001,
                "raster:offset": -0.1,
            },
        ),
    )
    catalog.upsert_item(
        item,
        metadata_path=path.with_suffix(".json"),
        status_path=path.with_suffix(".status.json"),
    )
    catalog.upsert_asset(
        item_id=item.id,
        asset_key="B04_10m",
        asset=item.assets["B04_10m"],
        local_path=path,
    )
    catalog.sync_job_snapshot(
        {
            "item_id": item.id,
            "status": "completed",
            "attempts": 0,
            "last_error": None,
            "assets": [
                {
                    "item_id": item.id,
                    "asset_key": "B04_10m",
                    "status": "completed",
                    "attempts": 0,
                    "last_error": None,
                    "bytes": path.stat().st_size,
                    "sha256": None,
                }
            ],
        }
    )


def test_dtype_promotion_preserves_mixed_integer_ranges():
    assert _promote_dtype(["Byte", "UInt16"]) == "UInt16"
    assert _promote_dtype(["Int16", "UInt16"]) == "Int32"
    assert _promote_dtype(["Int32", "UInt32"]) == "Float64"
    assert _promote_dtype(["Float32", "UInt16"]) == "Float32"


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are not installed")
def test_extractor_band_contract_reserves_valid_cloud_probability_zero(tmp_path):
    extractor = RasterExtractor(
        ExtractionConfig(
            dataset_db=tmp_path / "dataset.duckdb",
            bbox=(8.0, 46.9, 8.1, 47.0),
            asset_keys=("B04_10m", "CLD_20m"),
            output_path=tmp_path / "mixed.tif",
        )
    )
    pieces = [
        {
            "asset_key": "B04_10m",
            "nodata": "0",
            "data_type": "uint16",
            "raster_dtype": "UInt16",
            "raster_scale": 0.0001,
            "raster_offset": -0.1,
        },
        {
            "asset_key": "CLD_20m",
            "nodata": None,
            "data_type": "uint8",
            "raster_dtype": "Byte",
            "raster_scale": None,
            "raster_offset": None,
        },
    ]

    nodata, working_dtype = extractor._destination_nodata(pieces)
    contract = extractor._band_contract(pieces, destination_nodata=nodata)

    assert nodata == "-9999"
    assert working_dtype == "Int32"
    assert contract["bands"][1]["zero_is_valid"] is True
    assert contract["bands"][1]["output_nodata"] == -9999


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are not installed")
def test_destination_nodata_does_not_truncate_floating_sources(tmp_path):
    extractor = RasterExtractor(
        ExtractionConfig(
            dataset_db=tmp_path / "dataset.duckdb",
            bbox=(8.0, 46.9, 8.1, 47.0),
            asset_keys=("NDVI",),
            output_path=tmp_path / "ndvi.tif",
            nodata="-9999",
        )
    )

    nodata, working_dtype = extractor._destination_nodata(
        [
            {
                "asset_key": "NDVI",
                "nodata": "-9999",
                "data_type": "float32",
                "raster_dtype": "Float32",
            }
        ]
    )

    assert nodata == "-9999"
    assert working_dtype is None


def test_warp_feature_maps_source_nodata_but_preserves_valid_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(
        RasterExtractor,
        "_locate_gdal",
        staticmethod(
            lambda: {
                "gdalwarp": "gdalwarp",
                "gdalbuildvrt": "gdalbuildvrt",
                "gdal_translate": "gdal_translate",
                "gdalinfo": "gdalinfo",
            }
        ),
    )
    extractor = RasterExtractor(
        ExtractionConfig(
            dataset_db=tmp_path / "dataset.duckdb",
            bbox=(8.0, 46.9, 8.1, 47.0),
            asset_keys=("B04_10m",),
            output_path=tmp_path / "mixed.tif",
        )
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        extractor,
        "_run_command",
        lambda command: commands.append(list(command)),
    )

    extractor._warp_feature(
        asset_key="B04_10m",
        sources=(
            {"local_path": tmp_path / "red.jp2", "nodata": 0},
            {"local_path": tmp_path / "cloud-probability.jp2", "nodata": None},
        ),
        target_crs="EPSG:2056",
        resolution=10,
        destination_nodata="-9999",
        working_dtype="Int32",
        output_vrt=tmp_path / "feature.vrt",
    )

    red_warp, cloud_warp = commands[:2]
    assert red_warp[red_warp.index("-srcnodata") + 1] == "0"
    assert "-srcnodata" not in cloud_warp


def test_extract_parser_requires_region_features_and_output():
    args = build_parser().parse_args(
        [
            "extract",
            "--bbox",
            "8",
            "46",
            "9",
            "47",
            "--asset-keys",
            "B04_10m",
            "SCL_20m",
            "--output",
            "result.tif",
            "--target-crs",
            "EPSG:2056",
        ]
    )
    assert args.asset_keys == ["B04_10m", "SCL_20m"]
    assert args.target_crs == "EPSG:2056"
    assert args.selection == "latest-per-tile"


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are not installed")
def test_extractor_selects_latest_per_tile_and_streams_cog(tmp_path):
    database = tmp_path / "dataset.duckdb"
    catalog = DatasetCatalog(database)
    old_left = tmp_path / "old-left.tif"
    new_left = tmp_path / "new-left.tif"
    right = tmp_path / "right.tif"
    _create_raster(
        old_left,
        west=8.0,
        south=46.9,
        east=8.1,
        north=47.0,
        value=1,
    )
    _create_raster(
        new_left,
        west=8.0,
        south=46.9,
        east=8.1,
        north=47.0,
        value=10,
    )
    _create_raster(
        right,
        west=8.1,
        south=46.9,
        east=8.2,
        north=47.0,
        value=20,
    )
    _index_raster(
        catalog,
        item_id="old-left",
        tile_id="32TMT",
        acquired=datetime(2026, 7, 1, tzinfo=timezone.utc),
        bbox=(8.0, 46.9, 8.1, 47.0),
        path=old_left,
    )
    _index_raster(
        catalog,
        item_id="new-left",
        tile_id="32TMT",
        acquired=datetime(2026, 7, 2, tzinfo=timezone.utc),
        bbox=(8.0, 46.9, 8.1, 47.0),
        path=new_left,
    )
    _index_raster(
        catalog,
        item_id="right",
        tile_id="32TNT",
        acquired=datetime(2026, 7, 2, tzinfo=timezone.utc),
        bbox=(8.1, 46.9, 8.2, 47.0),
        path=right,
    )

    output = tmp_path / "stitched.tif"
    result = RasterExtractor(
        ExtractionConfig(
            dataset_db=database,
            bbox=(8.0, 46.9, 8.2, 47.0),
            asset_keys=("B04_10m",),
            output_path=output,
            target_crs="EPSG:4326",
            resolution=0.01,
            max_output_gib=0.01,
        )
    ).extract()

    assert result.source_count == 2
    assert (result.width, result.height, result.band_count) == (20, 10, 1)
    assert result.output_dtype == "UInt16"
    assert output.is_file()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["asset_keys_in_band_order"] == ["B04_10m"]
    assert manifest["band_contract"]["storage"] == "raw"
    assert manifest["band_contract"]["bands"][0]["scale"] == 0.0001
    assert manifest["band_contract"]["bands"][0]["offset"] == -0.1
    assert manifest["sources"][0]["raster_scale"] == 0.0001
    assert {source["item_id"] for source in manifest["sources"]} == {
        "new-left",
        "right",
    }

    info = json.loads(
        subprocess.run(
            [shutil.which("gdalinfo") or "gdalinfo", "-json", "-stats", str(output)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert info["metadata"]["IMAGE_STRUCTURE"]["LAYOUT"] == "COG"
    assert info["bands"][0]["description"] == "B04_10m"
    assert info["bands"][0]["scale"] == 0.0001
    assert info["bands"][0]["offset"] == -0.1
    assert json.loads(info["metadata"][""]["TERRAVAULT_BAND_CONTRACT"])["storage"] == "raw"
    assert info["bands"][0]["metadata"][""]["STATISTICS_MINIMUM"] == "10"
    assert info["bands"][0]["metadata"][""]["STATISTICS_MAXIMUM"] == "20"


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are not installed")
def test_extract_cli_dry_run_writes_manifest_but_not_pixels(tmp_path, capsys):
    database = tmp_path / "dataset.duckdb"
    catalog = DatasetCatalog(database)
    raster = tmp_path / "piece.tif"
    _create_raster(
        raster,
        west=8.0,
        south=46.9,
        east=8.1,
        north=47.0,
        value=10,
    )
    _index_raster(
        catalog,
        item_id="piece",
        tile_id="32TMT",
        acquired=datetime(2026, 7, 2, tzinfo=timezone.utc),
        bbox=(8.0, 46.9, 8.1, 47.0),
        path=raster,
    )
    output = tmp_path / "dry-run.tif"
    args = build_parser().parse_args(
        [
            "extract",
            "--dataset-db",
            str(database),
            "--bbox",
            "8.0",
            "46.9",
            "8.1",
            "47.0",
            "--asset-keys",
            "B04_10m",
            "--output",
            str(output),
            "--target-crs",
            "EPSG:4326",
            "--resolution",
            "0.01",
            "--dry-run",
        ]
    )

    assert cmd_extract(args) == 0
    assert "Extraction planned" in capsys.readouterr().out
    assert not output.exists()
    assert output.with_suffix(".tif.manifest.json").is_file()
