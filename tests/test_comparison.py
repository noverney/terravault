"""Tests for three-source monthly comparison discovery and configuration."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from terravault.comparison import (
    COMPARISON_SOURCES,
    ComparisonConfig,
    ComparisonProduct,
    MonthlyComparisonRenderer,
    discover_comparison_products,
)


def _write_product_tree(root: Path, *, source: str, year: int, month: int) -> None:
    directory = root / f"year={year:04d}" / f"month={month:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{source}_{year:04d}_{month:02d}_reflectance.tif").write_bytes(b"reflectance")
    (directory / f"{source}_{year:04d}_{month:02d}_indices.tif").write_bytes(b"indices")
    (directory / f"{source}_{year:04d}_{month:02d}.manifest.json").write_text(
        json.dumps({"source": source}),
        encoding="utf-8",
    )


def _products(tmp_path: Path, *, year: int = 2024, months: tuple[int, ...] = (6,)):
    return tuple(
        ComparisonProduct(
            source=source,
            year=year,
            month=month,
            reflectance_path=tmp_path / f"{source}-{month:02d}-reflectance.tif",
            indices_path=tmp_path / f"{source}-{month:02d}-indices.tif",
            manifest_path=tmp_path / f"{source}-{month:02d}.manifest.json",
        )
        for source in COMPARISON_SOURCES
        for month in months
    )


def test_discovery_resolves_every_source_and_month_in_deterministic_order(tmp_path: Path):
    roots = {source: tmp_path / source for source in COMPARISON_SOURCES}
    for source, root in roots.items():
        for month in (6, 7, 8):
            _write_product_tree(root, source=source, year=2024, month=month)

    products = discover_comparison_products(
        year=2024,
        months=(6, 7, 8),
        copernicus_root=roots["copernicus"],
        force_root=roots["force"],
        swisseo_root=roots["swisseo"],
    )

    assert len(products) == 9
    assert [(product.source, product.month) for product in products] == [
        (source, month) for source in COMPARISON_SOURCES for month in (6, 7, 8)
    ]
    assert all(product.reflectance_path.is_file() for product in products)
    assert all(product.indices_path.is_file() for product in products)
    assert all(product.manifest_path.is_file() for product in products)


def test_discovery_rejects_ambiguous_product_files(tmp_path: Path):
    for source in COMPARISON_SOURCES:
        _write_product_tree(tmp_path / source, source=source, year=2024, month=6)
    duplicate = tmp_path / "force/year=2024/month=06/duplicate_reflectance.tif"
    duplicate.write_bytes(b"duplicate")

    with pytest.raises(FileNotFoundError, match="found 2"):
        discover_comparison_products(
            year=2024,
            months=(6,),
            copernicus_root=tmp_path / "copernicus",
            force_root=tmp_path / "force",
            swisseo_root=tmp_path / "swisseo",
        )


def test_discovery_and_config_accept_selected_source_subset(tmp_path: Path):
    roots = {source: tmp_path / source for source in COMPARISON_SOURCES}
    for source in ("copernicus", "swisseo"):
        _write_product_tree(roots[source], source=source, year=2024, month=7)

    products = discover_comparison_products(
        year=2024,
        months=(7,),
        copernicus_root=roots["copernicus"],
        force_root=roots["force"],
        swisseo_root=roots["swisseo"],
        sources=("swisseo", "copernicus"),
    )
    config = ComparisonConfig(
        products=products,
        output_root=tmp_path / "comparison",
        preview_width=512,
        atlas_width=1200,
        atlas_height=900,
    )

    assert [(product.source, product.month) for product in products] == [
        ("copernicus", 7),
        ("swisseo", 7),
    ]
    assert config.sources == ("copernicus", "swisseo")


def test_comparison_config_requires_a_complete_source_month_grid(tmp_path: Path):
    products = _products(tmp_path, months=(6, 7, 8))
    config = ComparisonConfig(
        products=products,
        output_root=tmp_path / "comparison",
        analysis_resolution=60.0,
        preview_width=512,
        atlas_width=1200,
        atlas_height=900,
        histogram_bins=16,
    )

    assert len(config.products) == 9
    assert config.analysis_resolution == 60.0
    july = ComparisonConfig(
        products=_products(tmp_path / "july", months=(7,)),
        output_root=tmp_path / "july-comparison",
        preview_width=512,
        atlas_width=1200,
        atlas_height=900,
    )
    assert len(july.products) == 3
    with pytest.raises(ValueError, match="every source"):
        ComparisonConfig(
            products=products[:-1],
            output_root=tmp_path / "incomplete",
            preview_width=512,
            atlas_width=1200,
            atlas_height=900,
        )
    with pytest.raises(ValueError, match="duplicate"):
        ComparisonConfig(
            products=(*products, products[0]),
            output_root=tmp_path / "duplicate",
            preview_width=512,
            atlas_width=1200,
            atlas_height=900,
        )
    with pytest.raises(ValueError, match="one to three"):
        ComparisonConfig(
            products=_products(tmp_path / "four-months", months=(5, 6, 7, 8)),
            output_root=tmp_path / "four-months-comparison",
            preview_width=512,
            atlas_width=1200,
            atlas_height=900,
        )


def test_timing_normalization_prefers_explicit_monthly_time_and_supports_force_stages():
    explicit = MonthlyComparisonRenderer._timings(
        {
            "timings_seconds": {
                "monthly_mosaic_generation": 21.25,
                "index_calculation": 3.5,
                "total": 25.0,
            }
        }
    )
    legacy_force = MonthlyComparisonRenderer._timings(
        {
            "timings_seconds": {
                "force_qai_screening": 2.0,
                "monthly_compositing": 5.0,
                "national_clipping_and_cog": 7.0,
                "index_calculation": 1.5,
                "total": 16.0,
            }
        }
    )

    assert explicit == {
        "monthly_mosaic_generation": 21.25,
        "index_calculation": 3.5,
        "total": 25.0,
    }
    assert legacy_force == {
        "monthly_mosaic_generation": 14.0,
        "index_calculation": 1.5,
        "total": 16.0,
    }
    assert MonthlyComparisonRenderer._reported_mask(
        {
            "average_catalog_cloud_cover_pct": 12.0,
            "average_source_percent_masked": 40.0,
        }
    ) == ("catalog cloud", 12.0)
    assert MonthlyComparisonRenderer._reported_mask(
        {"average_source_percent_masked": 40.0}
    ) == ("source masked", 40.0)
    with pytest.raises(ValueError, match="index and total"):
        MonthlyComparisonRenderer._timings(
            {"timings_seconds": {"monthly_mosaic_generation": 1.0}}
        )
    with pytest.raises(ValueError, match="monthly mosaic timing"):
        MonthlyComparisonRenderer._timings(
            {"timings_seconds": {"index_calculation": 1.0, "total": 1.0}}
        )
    with pytest.raises(ValueError, match="shorter"):
        MonthlyComparisonRenderer._timings(
            {
                "timings_seconds": {
                    "monthly_mosaic_generation": 10.0,
                    "index_calculation": 5.0,
                    "total": 12.0,
                }
            }
        )


def test_analysis_grid_uses_shared_bbox_crs_resolution_and_average_resampling(
    monkeypatch,
    tmp_path: Path,
):
    products = _products(tmp_path, months=(6, 7, 8))
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "terravault.comparison.locate_gdal",
        lambda *names: {name: name for name in names},
    )
    monkeypatch.setattr(
        "terravault.comparison.run_gdal",
        lambda command: commands.append(list(command)),
    )
    renderer = MonthlyComparisonRenderer(
        ComparisonConfig(
            products=products,
            output_root=tmp_path / "comparison",
            bbox=(7.0, 46.0, 8.0, 47.0),
            target_crs="EPSG:2056",
            analysis_resolution=60.0,
            nodata=-9999.0,
            preview_width=512,
            atlas_width=1200,
            atlas_height=900,
            histogram_bins=16,
        )
    )
    output = tmp_path / "analysis.vrt"

    renderer._analysis_vrt(products[0], output)

    assert len(commands) == 1
    command = commands[0]
    assert command[:3] == ["gdalwarp", "-overwrite", "-of"]
    assert command[command.index("-t_srs") + 1] == "EPSG:2056"
    assert command[command.index("-te_srs") + 1] == "EPSG:4326"
    extent_index = command.index("-te")
    assert command[extent_index + 1 : extent_index + 5] == ["7.0", "46.0", "8.0", "47.0"]
    resolution_index = command.index("-tr")
    assert command[resolution_index + 1 : resolution_index + 3] == ["60.0", "60.0"]
    assert "-tap" in command
    assert command[command.index("-r") + 1] == "average"
    assert command[command.index("-dstnodata") + 1] == "-9999.0"
    assert command[-2:] == [str(products[0].indices_path), str(output)]


@pytest.mark.skipif(
    any(
        shutil.which(command) is None
        for command in (
            "gdal_create",
            "gdal_translate",
            "gdalinfo",
            "gdalwarp",
            "gdaldem",
            "gdal_rasterize",
            "ogr2ogr",
        )
    ),
    reason="GDAL command-line tools are not installed",
)
def test_renderer_creates_complete_small_atlas_on_common_roi_grid(tmp_path: Path):
    roi = tmp_path / "roi.geojson"
    roi.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "test ROI"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [7.0, 46.75],
                                    [7.5, 47.0],
                                    [8.0, 46.75],
                                    [7.5, 46.5],
                                    [7.0, 46.75],
                                ]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    products: list[ComparisonProduct] = []
    for source_number, source in enumerate(COMPARISON_SOURCES, start=1):
        for month in (6, 7, 8):
            directory = tmp_path / source / f"year=2024/month={month:02d}"
            directory.mkdir(parents=True)
            reflectance = directory / f"{source}_reflectance.tif"
            indices = directory / f"{source}_indices.tif"
            manifest = directory / f"{source}.manifest.json"
            subprocess.run(
                [
                    "gdal_create",
                    "-of",
                    "GTiff",
                    "-ot",
                    "Float32",
                    "-outsize",
                    "200",
                    "300",
                    "-bands",
                    "6",
                    "-burn",
                    "0.30",
                    "-burn",
                    "0.20",
                    "-burn",
                    "0.25",
                    "-burn",
                    "0.60",
                    "-burn",
                    "0.58",
                    "-burn",
                    "0.35",
                    "-a_srs",
                    "EPSG:2056",
                    "-a_ullr",
                    "2550000",
                    "1250000",
                    "2650000",
                    "1100000",
                    "-a_nodata",
                    "-9999",
                    str(reflectance),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            adjustment = source_number * 0.02 + (month - 6) * 0.01
            subprocess.run(
                [
                    "gdal_create",
                    "-of",
                    "GTiff",
                    "-ot",
                    "Float32",
                    "-outsize",
                    "200",
                    "300",
                    "-bands",
                    "4",
                    "-burn",
                    str(0.50 + adjustment),
                    "-burn",
                    str(0.33 + adjustment),
                    "-burn",
                    str(0.40 + adjustment),
                    "-burn",
                    str(0.26 + adjustment),
                    "-a_srs",
                    "EPSG:2056",
                    "-a_ullr",
                    "2550000",
                    "1250000",
                    "2650000",
                    "1100000",
                    "-a_nodata",
                    "-9999",
                    str(indices),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            mosaic_seconds = 1000.0 * source_number + month
            index_seconds = 120.0 + month
            payload = {
                "timings_seconds": {
                    "monthly_mosaic_generation": mosaic_seconds,
                    "index_calculation": index_seconds,
                    "total": mosaic_seconds + index_seconds + 2.0,
                }
            }
            if source == "swisseo":
                payload["average_source_percent_masked"] = 25.0
            else:
                payload["average_catalog_cloud_cover_pct"] = 15.0
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            products.append(
                ComparisonProduct(
                    source=source,
                    year=2024,
                    month=month,
                    reflectance_path=reflectance,
                    indices_path=indices,
                    manifest_path=manifest,
                )
            )

    result = MonthlyComparisonRenderer(
        ComparisonConfig(
            products=tuple(products),
            output_root=tmp_path / "comparison",
            bbox=(7.0, 46.5, 8.0, 47.0),
            cutline_path=roi,
            analysis_resolution=500.0,
            preview_width=512,
            atlas_width=2400,
            atlas_height=1800,
            histogram_bins=32,
        )
    ).run()

    assert result.atlas_path.is_file()
    assert len(result.mosaic_paths) == 9
    assert len(result.index_map_paths) == 36
    assert len(result.index_tiff_paths) == 36
    assert all(path.is_file() for path in result.index_tiff_paths)
    assert len(result.source_overview_paths) == 3
    assert all(path.is_file() for path in result.source_overview_paths)
    assert result.report_manifest_path.is_file()
    assert result.report_readme_path.is_file()
    report = json.loads(result.report_manifest_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 2
    assert set(report["sources"]) == set(COMPARISON_SOURCES)
    assert all(len(source["months"]) == 3 for source in report["sources"].values())
    for source, source_folder in zip(
        COMPARISON_SOURCES,
        ("sentinel-copernicus", "force", "swisseo"),
        strict=True,
    ):
        for month in (6, 7, 8):
            month_root = (
                result.report_manifest_path.parent
                / f"sources/{source_folder}/year=2024/month={month:02d}"
            )
            assert (month_root / "false-colour.png").is_file()
            assert all(
                (month_root / f"{index_name}.png").is_file()
                for index_name in ("ndvi", "gndvi", "ndre", "ndwi")
            )
            month_manifest = next(
                entry
                for entry in report["sources"][source]["months"]
                if entry["month"] == month
            )
            for band_number, index_name in enumerate(
                ("NDVI", "GNDVI", "NDRE", "NDWI"),
                start=1,
            ):
                tiff = month_root / f"{index_name.lower()}.tif"
                assert tiff.is_file()
                details = month_manifest["indices"][index_name]
                assert details["png"] == str(
                    (month_root / f"{index_name.lower()}.png").relative_to(
                        result.report_manifest_path.parent
                    )
                )
                assert details["tiff"] == str(
                    tiff.relative_to(result.report_manifest_path.parent)
                )
                assert details["source_band"] == band_number
                assert details["source_band_name"] == index_name
                assert details["source_multi_band_cog"] == month_manifest["source_indices"]
                assert details["pixel_size"] == {"x": 500.0, "y": 500.0}
                assert details["raster_size"] == {"width": 200, "height": 300}
                assert details["nodata"] == -9999.0

                metadata = json.loads(
                    subprocess.run(
                        ["gdalinfo", "-json", str(tiff)],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                )
                assert metadata["size"] == [200, 300]
                assert metadata["geoTransform"] == [
                    2550000.0,
                    500.0,
                    0.0,
                    1250000.0,
                    0.0,
                    -500.0,
                ]
                assert metadata["bands"][0]["description"] == index_name
                assert metadata["bands"][0]["type"] == "Float32"
                assert metadata["bands"][0]["noDataValue"] == -9999.0
                assert metadata["metadata"]["IMAGE_STRUCTURE"]["LAYOUT"] == "COG"
    with Image.open(result.mosaic_paths[0]) as mosaic:
        assert mosaic.mode == "RGBA"
        assert mosaic.getpixel((0, 0))[3] == 0
        assert mosaic.getpixel((mosaic.width // 2, mosaic.height // 2))[3] > 0
    with Image.open(result.index_map_paths[0]) as index_map:
        assert index_map.mode == "RGBA"
        assert index_map.getpixel((0, 0))[3] == 0
        assert index_map.getpixel((index_map.width // 2, index_map.height // 2))[3] > 0
    statistics = json.loads(result.statistics_json_path.read_text(encoding="utf-8"))
    assert len(statistics["rows"]) == 9
    assert statistics["comparison_grid"]["roi_pixel_count"] > 0
    assert all(
        index["valid_pixel_coverage_pct"] == pytest.approx(100.0)
        for row in statistics["rows"]
        for index in row["indices"]
    )


def _write_product_tree_at_resolution(
    root: Path, *, source: str, year: int, month: int, resolution: str
) -> None:
    directory = root / f"year={year:04d}" / f"month={month:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{source}_{year:04d}_{month:02d}_{resolution}"
    (directory / f"{stem}_reflectance.tif").write_bytes(b"reflectance")
    (directory / f"{stem}_indices.tif").write_bytes(b"indices")
    (directory / f"{stem}.manifest.json").write_text(
        json.dumps({"source": source, "resolution": resolution}), encoding="utf-8"
    )


def test_discovery_selects_products_by_resolution(tmp_path: Path):
    roots = {source: tmp_path / source for source in COMPARISON_SOURCES}
    for source, root in roots.items():
        for resolution in ("10m", "20m"):
            _write_product_tree_at_resolution(
                root, source=source, year=2024, month=7, resolution=resolution
            )

    for resolution, token in ((10.0, "10m"), (20.0, "20m")):
        products = discover_comparison_products(
            year=2024,
            months=(7,),
            copernicus_root=roots["copernicus"],
            force_root=roots["force"],
            swisseo_root=roots["swisseo"],
            product_resolution=resolution,
        )
        assert len(products) == len(COMPARISON_SOURCES)
        for product in products:
            assert f"_{token}_" in product.reflectance_path.name
            assert f"_{token}_" in product.indices_path.name
            assert product.manifest_path.name.endswith(f"_{token}.manifest.json")


def test_discovery_without_resolution_is_ambiguous_when_two_coexist(tmp_path: Path):
    roots = {source: tmp_path / source for source in COMPARISON_SOURCES}
    for source, root in roots.items():
        for resolution in ("10m", "20m"):
            _write_product_tree_at_resolution(
                root, source=source, year=2024, month=7, resolution=resolution
            )

    with pytest.raises(FileNotFoundError, match="product_resolution"):
        discover_comparison_products(
            year=2024,
            months=(7,),
            copernicus_root=roots["copernicus"],
            force_root=roots["force"],
            swisseo_root=roots["swisseo"],
        )


def test_resolution_token_matches_the_monthly_output_stems():
    from terravault.comparison import resolution_token

    assert resolution_token(20.0) == "20m"
    assert resolution_token(10) == "10m"
    assert resolution_token(0.5) == "0p5m"
    with pytest.raises(ValueError):
        resolution_token(0)
