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


def _real_scene_pieces(
    tmp_path: Path, *, item_id: str, tile_id: str, day: int
) -> list[dict[str, Any]]:
    """Create one acquisition of real single-band rasters for every asset."""

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
    for key in VEGETATION_L2A_ASSET_KEYS:
        path = tmp_path / "assets" / item_id / f"{key}.tif"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "gdal_create", "-of", "GTiff", "-ot", "UInt16", "-outsize", "100", "50",
                "-bands", "1", "-burn", str(band_values[key]), "-a_srs", "EPSG:4326",
                "-a_ullr", "7.0", "47.0", "8.0", "46.5", "-a_nodata", "0", str(path),
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
                "acquisition_time": datetime(2024, 6, day, 10, tzinfo=timezone.utc),
                "tile_id": tile_id,
                "cloud_cover": 10.0,
                "nodata": 0,
                "raster_scale": 0.0001 if is_reflectance else None,
                "raster_offset": -0.1 if is_reflectance else None,
            }
        )
    return pieces


def test_interrupted_run_keeps_finished_tiles_and_resumes_from_them(
    monkeypatch,
    tmp_path: Path,
):
    """A drop mid-run must cost only the tile that was in flight.

    Tile medians are the expensive artefact of this route, so losing every
    finished one to a transient input-share failure made long months
    unrunnable.
    """

    database_path = tmp_path / "dataset.duckdb"
    database_path.write_bytes(b"catalog-placeholder")
    pieces = _real_scene_pieces(tmp_path, item_id="scene-a", tile_id="32TMT", day=6)
    pieces += _real_scene_pieces(tmp_path, item_id="scene-b", tile_id="32TNT", day=7)

    class FakeDatasetCatalog:
        def __init__(self, path: Path) -> None:
            assert path == database_path

        def query_raster_pieces(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return pieces

    monkeypatch.setattr(
        "terravault.copernicus_monthly.DatasetCatalog", FakeDatasetCatalog
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
    )

    import terravault.copernicus_monthly as module

    # Pin to one worker so "the tile that failed" is deterministic; parallel
    # ordering is covered separately.
    monkeypatch.setattr(module, "_SCENE_WORKERS", 1)
    monkeypatch.setattr(module, "_TILE_WORKERS", 1)
    monkeypatch.setattr(module, "_MEDIAN_WORKERS", 1)
    genuine_median = module.write_multiband_median
    first_pass: list[str] = []

    def median_failing_on_the_second_tile(**kwargs: Any) -> Any:
        if first_pass:
            raise RuntimeError("input share dropped")
        first_pass.append(Path(kwargs["output_path"]).name)
        return genuine_median(**kwargs)

    monkeypatch.setattr(module, "write_multiband_median", median_failing_on_the_second_tile)
    with pytest.raises(RuntimeError, match="input share dropped"):
        CopernicusMonthlyProcessor(config).run()

    checkpoint = config.staging_root / "work" / config.stem / "tiles"
    finished = sorted(path.name for path in checkpoint.glob("*.tif"))
    assert finished == ["32TMT.tif"], f"expected the first tile to survive, got {finished}"
    assert not list(checkpoint.glob(".*.partial")), "an interrupted write must not survive"

    recomputed: list[str] = []

    def counting_median(**kwargs: Any) -> Any:
        recomputed.append(Path(kwargs["output_path"]).name)
        return genuine_median(**kwargs)

    monkeypatch.setattr(module, "write_multiband_median", counting_median)
    result = CopernicusMonthlyProcessor(config).run()

    assert result.reflectance_path.is_file()
    assert result.indices_path.is_file()
    assert len(recomputed) == 1, f"resume recomputed {recomputed}, expected only the lost tile"
    # A completed month must not leave the tile medians behind as scratch.
    assert not checkpoint.exists()



def test_parallel_tile_building_matches_serial_output(monkeypatch, tmp_path: Path):
    """Concurrency must not change the composite.

    Tiles are built out of order once several run at once, but the median's
    inputs and the mosaic's tile order both have to stay in acquisition order
    or the result silently depends on scheduling.
    """

    import terravault.copernicus_monthly as module

    def build(root: Path, workers: int) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        database_path = root / "dataset.duckdb"
        database_path.write_bytes(b"catalog-placeholder")
        pieces = _real_scene_pieces(root, item_id="scene-a", tile_id="32TMT", day=6)
        pieces += _real_scene_pieces(root, item_id="scene-b", tile_id="32TNT", day=7)
        pieces += _real_scene_pieces(root, item_id="scene-c", tile_id="32TLS", day=8)

        class FakeDatasetCatalog:
            def __init__(self, path: Path) -> None:
                assert path == database_path

            def query_raster_pieces(self, **_kwargs: Any) -> list[dict[str, Any]]:
                return pieces

        monkeypatch.setattr(
            "terravault.copernicus_monthly.DatasetCatalog", FakeDatasetCatalog
        )
        monkeypatch.setattr(module, "_SCENE_WORKERS", workers)
        monkeypatch.setattr(module, "_TILE_WORKERS", workers)
        monkeypatch.setattr(module, "_MEDIAN_WORKERS", workers)
        config = CopernicusMonthlyConfig(
            dataset_db=database_path,
            output_root=root / "monthly",
            year=2024,
            month=6,
            bbox=(7.0, 46.5, 8.0, 47.0),
            resolution=500.0,
            max_output_gib=1.0,
            staging_root=root / "staging",
        )
        return CopernicusMonthlyProcessor(config).run().reflectance_path

    def checksums(path: Path) -> list[int]:
        info = json.loads(
            subprocess.run(
                ["gdalinfo", "-json", "-checksum", str(path)],
                check=True, capture_output=True, text=True,
            ).stdout
        )
        return [band["checksum"] for band in info["bands"]]

    serial = checksums(build(tmp_path / "serial", workers=1))
    parallel = checksums(build(tmp_path / "parallel", workers=4))
    assert serial == parallel, f"parallel output differs: {serial} != {parallel}"


def test_selection_accepts_assets_staged_locally_when_archive_is_offline(
    monkeypatch, tmp_path: Path
):
    """A staged input must not be rejected because the archive is unmounted.

    Inputs are copied to the local staging root precisely so a run does not
    depend on the network share; selection has to honour that or an unmounted
    share fails the run before any work starts.
    """

    database_path = tmp_path / "dataset.duckdb"
    database_path.write_bytes(b"catalog-placeholder")
    config = CopernicusMonthlyConfig(
        dataset_db=database_path,
        output_root=tmp_path / "monthly",
        year=2024,
        month=7,
        staging_root=tmp_path / "staging",
        dry_run=True,
    )
    monkeypatch.setattr(
        "terravault.copernicus_monthly.DatasetCatalog", lambda path: object()
    )
    processor = CopernicusMonthlyProcessor(config)

    offline_archive = Path("/Volumes/definitely-not-mounted/B03_10m.jp2")
    piece = {
        "item_id": "scene-a",
        "asset_key": "B03_10m",
        "local_path": str(offline_archive),
        "byte_count": 11,
        "href": "",
    }
    assert not processor._asset_available(piece), "nothing staged yet"

    staged = config.staging_root / "bands" / "scene-a" / "B03_10m.jp2"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"staged-band")
    assert processor._asset_available(piece), "staged copy must satisfy selection"

    # A truncated staged copy is not usable and must not pass.
    staged.write_bytes(b"short")
    assert not processor._asset_available(piece)


def test_scene_extent_cuts_a_tile_to_the_bbox_on_the_output_grid(monkeypatch, tmp_path: Path):
    database_path = tmp_path / "dataset.duckdb"
    database_path.write_bytes(b"catalog-placeholder")
    monkeypatch.setattr("terravault.copernicus_monthly.DatasetCatalog", lambda path: object())
    monkeypatch.setattr(
        "terravault.copernicus_monthly.locate_gdal",
        lambda *names: {name: name for name in names},
    )
    envelopes = []

    def fake_envelope(_tool, bbox, srs):
        envelopes.append((tuple(bbox), srs))
        return (2_650_003.0, 1_200_007.0, 2_672_995.0, 1_219_991.0)

    monkeypatch.setattr("terravault.copernicus_monthly.bbox_envelope", fake_envelope)
    processor = CopernicusMonthlyProcessor(
        CopernicusMonthlyConfig(
            dataset_db=database_path,
            output_root=tmp_path / "monthly",
            year=2024,
            month=7,
            bbox=(8.17, 46.975, 8.47, 47.155),
            resolution=10.0,
        )
    )
    tile = (2_600_000.0, 1_150_000.0, 2_710_000.0, 1_260_000.0)

    # Snapped outwards to the 10 m grid, then four pixels of margin.
    assert processor._scene_extent(tile) == (
        2_649_960.0,
        1_199_960.0,
        2_673_040.0,
        1_220_040.0,
    )
    # Clipped to a tile that only covers part of the bbox.
    assert processor._scene_extent((2_660_000.0, 1_150_000.0, 2_710_000.0, 1_210_000.0)) == (
        2_660_000.0,
        1_199_960.0,
        2_673_040.0,
        1_210_000.0,
    )
    # A tile that misses the bbox keeps its own extent.
    outside = (2_700_000.0, 1_100_000.0, 2_800_000.0, 1_150_000.0)
    assert processor._scene_extent(outside) == outside
    assert envelopes == [((8.17, 46.975, 8.47, 47.155), "EPSG:2056")]
