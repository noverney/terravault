"""Tests for FORCE NDVI visualization products."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from terravault.cli import _default_log_file, build_parser
from terravault.force_visualization import (
    ForceVisualizationConfig,
    ForceVisualizer,
    _same_crs,
)

GDAL_COMMANDS = (
    "gdal_create",
    "gdal_calc.py",
    "gdal_translate",
    "gdalbuildvrt",
    "gdaldem",
    "gdalinfo",
    "gdalwarp",
)
HAS_GDAL = all(shutil.which(command) for command in GDAL_COMMANDS)
HAS_PILLOW = importlib.util.find_spec("PIL") is not None


def test_crs_comparison_accepts_equivalent_wkt_serializations():
    assert _same_crs(
        'PROJCS["LV95",AUTHORITY["EPSG","2056"]]',
        'PROJCRS["CH1903+ / LV95", ID["EPSG",2056]]',
    )
    assert not _same_crs(
        'PROJCRS["LV95",ID["EPSG",2056]]',
        'GEOGCRS["WGS 84",ID["EPSG",4326]]',
    )


def _create_three_way_safety_fixture(
    tmp_path: Path,
    *,
    force_orbit: str = "R108",
    force_tile: str = "T32TMT",
    qai_type: str = "UInt16",
    qai_nodata: int = 1,
    write_provenance: bool = True,
) -> tuple[Path, Path]:
    """Create the smallest georeferenced inputs needed for QAI safety checks."""

    force_root = tmp_path / "external-force"
    source = force_root / "datacube" / "mosaic" / "safety-feature.tif"
    source.parent.mkdir(parents=True)
    subprocess.run(
        [
            shutil.which("gdal_create") or "gdal_create",
            "-of",
            "GTiff",
            "-ot",
            "Int16",
            "-bands",
            "4",
            "-outsize",
            "4",
            "4",
            "-burn",
            "1000",
            "-burn",
            "3000",
            "-burn",
            "4",
            "-burn",
            "10",
            "-a_nodata",
            "-9999",
            "-a_srs",
            "EPSG:2056",
            "-a_ullr",
            "2600000",
            "1200040",
            "2600040",
            "1200000",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    product_stem = "S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T140459"
    native_root = tmp_path / "native-force"
    product_root = native_root / "level2" / "products" / product_stem
    qai = product_root / "mosaic" / "20260717_LEVEL2_SEN2B_QAI.tif"
    qai.parent.mkdir(parents=True)
    subprocess.run(
        [
            shutil.which("gdal_create") or "gdal_create",
            "-of",
            "GTiff",
            "-ot",
            qai_type,
            "-bands",
            "1",
            "-outsize",
            "4",
            "4",
            "-burn",
            "0",
            "-a_nodata",
            str(qai_nodata),
            "-a_srs",
            "EPSG:2056",
            "-a_ullr",
            "2600000",
            "1200040",
            "2600040",
            "1200000",
            str(qai),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    if write_provenance:
        force_job = force_root / "_terravault" / "force" / "jobs" / "safety-feature.json"
        force_job.parent.mkdir(parents=True)
        force_job.write_text(json.dumps({"input_path": str(source)}), encoding="utf-8")
        source.with_suffix(".tif.manifest.json").write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "item_id": (
                                "S2B_MSIL2A_20260717T103029_N0512_R108_T32TMT_20260717T142404"
                            )
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        native_manifest = native_root / "_terravault" / "force-l2" / "jobs" / f"{product_stem}.json"
        native_manifest.parent.mkdir(parents=True)
        native_manifest.write_text(
            json.dumps(
                {
                    "status": "complete",
                    "product_identity": {
                        "platform": "S2B",
                        "sensing_time": "20260717T103029",
                        "relative_orbit": force_orbit,
                        "mgrs_tile": force_tile,
                    },
                    "qai_paths": [str(qai)],
                    "qai_mosaic_path": str(qai),
                }
            ),
            encoding="utf-8",
        )
    return source, qai


def test_force_visualize_parser_and_log_path(tmp_path):
    output_dir = tmp_path / "visualizations"
    args = build_parser().parse_args(
        [
            "force-visualize",
            "--input",
            "mosaic.vrt",
            "--output-dir",
            str(output_dir),
            "--cloud-threshold",
            "35",
            "--force-qai",
            "force-qai.vrt",
            "--force-qai-mask",
            "0x031F",
        ]
    )

    assert args.cloud_threshold == 35
    assert args.quicklook_width == 1400
    assert args.debug_plot_width == 1800
    assert args.force_qai == "force-qai.vrt"
    assert args.force_qai_mask == 0x031F
    assert not args.no_debug_plot
    assert _default_log_file(args) == (output_dir / "_terravault/logs/force-visualize.log")


def test_force_visualization_config_validation(tmp_path):
    with pytest.raises(ValueError, match="between 0 and 100"):
        ForceVisualizationConfig(
            input_path=tmp_path / "mosaic.vrt",
            cloud_threshold=101,
        )
    with pytest.raises(ValueError, match="at least 1"):
        ForceVisualizationConfig(
            input_path=tmp_path / "mosaic.vrt",
            red_band=0,
        )
    with pytest.raises(ValueError, match="at least 640"):
        ForceVisualizationConfig(
            input_path=tmp_path / "mosaic.vrt",
            debug_plot_width=639,
        )
    with pytest.raises(ValueError, match="between 1 and 65535"):
        ForceVisualizationConfig(
            input_path=tmp_path / "mosaic.vrt",
            force_qai_mask=0,
        )


@pytest.mark.skipif(
    not HAS_GDAL or not HAS_PILLOW,
    reason="GDAL command-line tools and Pillow are required",
)
def test_force_visualizer_writes_ndvi_cog_png_manifest_and_skips(tmp_path):
    source = tmp_path / "force-feature.tif"
    subprocess.run(
        [
            shutil.which("gdal_create") or "gdal_create",
            "-of",
            "GTiff",
            "-ot",
            "Int16",
            "-bands",
            "4",
            "-outsize",
            "20",
            "10",
            "-burn",
            "1000",
            "-burn",
            "3000",
            "-burn",
            "4",
            "-burn",
            "10",
            "-a_nodata",
            "-9999",
            "-a_srs",
            "EPSG:2056",
            "-a_ullr",
            "2600000",
            "1200100",
            "2600200",
            "1200000",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    config = ForceVisualizationConfig(
        input_path=source,
        output_dir=tmp_path / "visualizations",
        quicklook_width=64,
        debug_plot_width=640,
    )

    result = ForceVisualizer(config).run()

    assert result.status == "complete"
    assert not result.skipped
    assert (result.width, result.height) == (20, 10)
    assert result.valid_percent == 100
    assert result.ndvi_path.is_file()
    assert result.quicklook_path.is_file()
    assert result.debug_plot_path is not None
    assert result.debug_plot_path.is_file()
    assert result.worldfile_path.is_file()
    assert result.raw_valid_percent == 100
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 4
    assert manifest["algorithm_version"] == 2
    assert manifest["band_semantics"]["source"] == "unverified_identity"
    assert manifest["output_files"]
    assert manifest["bands"] == {"red": 1, "nir": 2, "scl": 3, "cloud": 4}
    assert manifest["mask_quality"]
    assert manifest["debug_plot_path"] == str(result.debug_plot_path)
    assert manifest["raw_valid_percent"] == 100
    assert manifest["masked_percentage_points"] == 0
    assert manifest["statistics"]["STATISTICS_MINIMUM"] == "0.5"
    from PIL import Image

    with Image.open(result.debug_plot_path) as debug_plot:
        assert debug_plot.width == 640
        assert debug_plot.height > 300
    info = json.loads(
        subprocess.run(
            [shutil.which("gdalinfo") or "gdalinfo", "-json", str(result.ndvi_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert info["metadata"]["IMAGE_STRUCTURE"]["LAYOUT"] == "COG"
    assert info["bands"][0]["type"] == "Float32"
    assert info["bands"][0]["noDataValue"] == -9999

    repeated = ForceVisualizer(config).run()
    assert repeated.skipped
    assert repeated.ndvi_path == result.ndvi_path

    result.quicklook_path.write_bytes(b"")
    repaired = ForceVisualizer(config).run()
    assert not repaired.skipped
    assert repaired.quicklook_path.stat().st_size > 0

    legacy_manifest = json.loads(repaired.manifest_path.read_text(encoding="utf-8"))
    legacy_manifest["schema_version"] = 2
    legacy_manifest.pop("algorithm_version")
    legacy_manifest["fingerprint"] = "legacy-formula-fingerprint"
    repaired.manifest_path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
    upgraded = ForceVisualizer(config).run()
    assert not upgraded.skipped
    assert json.loads(upgraded.manifest_path.read_text(encoding="utf-8"))["algorithm_version"] == 2


@pytest.mark.skipif(
    not HAS_GDAL or not HAS_PILLOW,
    reason="GDAL command-line tools and Pillow are required",
)
def test_force_visualizer_writes_three_panel_qai_comparison(tmp_path):
    force_root = tmp_path / "external-force"
    source = force_root / "datacube" / "mosaic" / "force-feature.tif"
    source.parent.mkdir(parents=True)
    subprocess.run(
        [
            shutil.which("gdal_create") or "gdal_create",
            "-of",
            "GTiff",
            "-ot",
            "Int16",
            "-bands",
            "4",
            "-outsize",
            "20",
            "10",
            "-burn",
            "1000",
            "-burn",
            "3000",
            "-burn",
            "4",
            "-burn",
            "10",
            "-a_nodata",
            "-9999",
            "-a_srs",
            "EPSG:2056",
            "-a_ullr",
            "2600000",
            "1200100",
            "2600200",
            "1200000",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    force_job = force_root / "_terravault" / "force" / "jobs" / "force-feature.json"
    force_job.parent.mkdir(parents=True)
    force_job.write_text(
        json.dumps({"input_path": str(source)}),
        encoding="utf-8",
    )
    source.with_suffix(".tif.manifest.json").write_text(
        json.dumps(
            {
                "sources": [
                    {"item_id": ("S2B_MSIL2A_20260717T103029_N0512_R108_T32TMT_20260717T142404")}
                ]
            }
        ),
        encoding="utf-8",
    )
    product_stem = "S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T140459"
    native_root = tmp_path / "native-force"
    product_root = native_root / "level2" / "products" / product_stem
    qai_left = product_root / "X0000_Y0000" / "qai-left.tif"
    qai_right = product_root / "X0001_Y0000" / "qai-right.tif"
    qai_left.parent.mkdir(parents=True)
    qai_right.parent.mkdir(parents=True)
    for path, burn, left, right in (
        (qai_left, "2", "2600000", "2600100"),
        (qai_right, "0", "2600100", "2600200"),
    ):
        subprocess.run(
            [
                shutil.which("gdal_create") or "gdal_create",
                "-of",
                "GTiff",
                "-ot",
                "UInt16",
                "-bands",
                "1",
                "-outsize",
                "10",
                "10",
                "-burn",
                burn,
                "-a_nodata",
                "1",
                "-a_srs",
                "EPSG:2056",
                "-a_ullr",
                left,
                "1200100",
                right,
                "1200000",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    qai = product_root / "mosaic" / "20260717_LEVEL2_SEN2B_QAI.vrt"
    qai.parent.mkdir(parents=True)
    subprocess.run(
        [
            shutil.which("gdalbuildvrt") or "gdalbuildvrt",
            str(qai),
            str(qai_left),
            str(qai_right),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    native_manifest = native_root / "_terravault" / "force-l2" / "jobs" / f"{product_stem}.json"
    native_manifest.parent.mkdir(parents=True)
    native_manifest.write_text(
        json.dumps(
            {
                "status": "complete",
                "product_identity": {
                    "platform": "S2B",
                    "sensing_time": "20260717T103029",
                    "relative_orbit": "R108",
                    "mgrs_tile": "T32TMT",
                },
                "qai_paths": [str(qai_left), str(qai_right)],
                "qai_mosaic_path": str(qai),
            }
        ),
        encoding="utf-8",
    )
    config = ForceVisualizationConfig(
        input_path=source,
        force_qai_path=qai,
        output_dir=tmp_path / "visualizations",
        quicklook_width=64,
        debug_plot_width=900,
    )

    result = ForceVisualizer(config).run()

    assert result.status == "complete"
    assert result.valid_percent == 100
    assert result.raw_valid_percent == 100
    assert result.force_valid_percent == 50
    assert result.force_ndvi_path is not None
    assert result.force_ndvi_path.is_file()
    assert result.force_quicklook_path is not None
    assert result.force_quicklook_path.is_file()
    assert result.force_worldfile_path is not None
    assert result.force_worldfile_path.is_file()
    assert result.debug_plot_path is not None
    assert result.debug_plot_path.name.endswith("_raw_cdse_force.png")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 4
    assert manifest["force_qai_mask"] == 0x031F
    assert manifest["force_qai_mask_hex"] == "0x031F"
    assert manifest["force_temporal_match"]["status"] == "matched"
    assert manifest["force_alignment"]["resampling"] == "nearest"
    assert manifest["force_valid_percent"] == 50
    assert manifest["force_masked_percentage_points"] == 50
    assert "same L2A B04/B08" in manifest["comparison_semantics"]
    from PIL import Image

    with Image.open(result.debug_plot_path) as debug_plot:
        assert debug_plot.width == 900
        assert debug_plot.height > 300

    repeated = ForceVisualizer(config).run()
    assert repeated.skipped
    assert repeated.force_valid_percent == 50


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are required")
def test_force_visualizer_rejects_unknown_qai_provenance_by_default(tmp_path):
    source, qai = _create_three_way_safety_fixture(tmp_path, write_provenance=False)

    with pytest.raises(ValueError, match="Cannot verify full FORCE QAI acquisition provenance"):
        ForceVisualizer(
            ForceVisualizationConfig(
                input_path=source,
                force_qai_path=qai,
                output_dir=tmp_path / "visualizations",
                debug_plot=False,
            )
        ).run()


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are required")
def test_force_visualizer_rejects_mismatched_full_acquisition_identity(tmp_path):
    source, qai = _create_three_way_safety_fixture(tmp_path, force_orbit="R109")

    with pytest.raises(ValueError, match="product membership does not match"):
        ForceVisualizer(
            ForceVisualizationConfig(
                input_path=source,
                force_qai_path=qai,
                output_dir=tmp_path / "visualizations",
                debug_plot=False,
            )
        ).run()


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are required")
@pytest.mark.parametrize(
    ("qai_type", "qai_nodata", "error"),
    (
        ("Byte", 1, "signed or unsigned 16-bit integer"),
        ("UInt16", 0, "native nodata bit value 1"),
    ),
)
def test_force_visualizer_rejects_invalid_qai_raster_semantics(
    tmp_path,
    qai_type,
    qai_nodata,
    error,
):
    source, qai = _create_three_way_safety_fixture(
        tmp_path,
        qai_type=qai_type,
        qai_nodata=qai_nodata,
    )

    with pytest.raises(ValueError, match=error):
        ForceVisualizer(
            ForceVisualizationConfig(
                input_path=source,
                force_qai_path=qai,
                output_dir=tmp_path / "visualizations",
                debug_plot=False,
                quicklook_width=64,
            )
        ).run()


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are required")
def test_force_visualizer_applies_legacy_stac_reflectance_and_preserves_clear_zero(
    tmp_path,
):
    source = tmp_path / "legacy-external-feature.tif"
    subprocess.run(
        [
            shutil.which("gdal_create") or "gdal_create",
            "-of",
            "GTiff",
            "-ot",
            "UInt16",
            "-bands",
            "4",
            "-outsize",
            "4",
            "4",
            "-burn",
            "10000",
            "-burn",
            "60000",
            "-burn",
            "4",
            "-burn",
            "0",
            "-a_nodata",
            "0",
            "-a_srs",
            "EPSG:2056",
            "-a_ullr",
            "2600000",
            "1200040",
            "2600040",
            "1200000",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    item_root = tmp_path / "item"
    item_root.mkdir()
    scene_metadata = item_root / "scene_metadata.json"
    scene_metadata.write_text(
        json.dumps(
            {
                "assets": {
                    "B04_10m": {
                        "raster:scale": 0.0001,
                        "raster:offset": -0.1,
                        "nodata": 0,
                    },
                    "B08_10m": {
                        "raster:scale": 0.0001,
                        "raster:offset": -0.1,
                        "nodata": 0,
                    },
                    "SCL_20m": {"nodata": 0},
                    "CLD_20m": {"nodata": None},
                }
            }
        ),
        encoding="utf-8",
    )
    source.with_suffix(".tif.manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "nodata": "0",
                "asset_keys_in_band_order": [
                    "B04_10m",
                    "B08_10m",
                    "SCL_20m",
                    "CLD_20m",
                ],
                "sources": [
                    {
                        "asset_key": asset_key,
                        "local_path": str(item_root / f"{asset_key}.jp2"),
                    }
                    for asset_key in ("B04_10m", "B08_10m", "SCL_20m", "CLD_20m")
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ForceVisualizer(
        ForceVisualizationConfig(
            input_path=source,
            output_dir=tmp_path / "visualizations",
            quicklook_width=64,
            debug_plot=False,
        )
    ).run()

    assert result.valid_percent == 100
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    semantics = manifest["band_semantics"]
    assert semantics["source"] == "legacy_extraction_stac_provenance"
    assert semantics["reflectance"]["red"]["scale"] == 0.0001
    assert semantics["reflectance"]["red"]["offset"] == -0.1
    assert semantics["cloud"]["zero_is_valid"] is True
    assert semantics["cloud"]["legacy_zero_nodata_recovery"] is True
    assert float(manifest["statistics"]["STATISTICS_MINIMUM"]) == pytest.approx(
        5.0 / 6.8,
        rel=1e-6,
    )
    calculation = next(
        argument
        for command in manifest["commands"]
        for argument in command
        if argument.startswith("--calc=")
    )
    assert "A.astype(float32)" in calculation
    assert "B.astype(float32)" in calculation
    assert "(A+B)" not in calculation
    assert "where(D==0,0,D)" in calculation
    assert any("--hideNoData" in command for command in manifest["commands"])


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are required")
def test_force_visualizer_rejects_native_force_boa_signature(tmp_path):
    source = tmp_path / "native-boa.tif"
    source.write_bytes(b"metadata-only-test")
    visualizer = ForceVisualizer(
        ForceVisualizationConfig(
            input_path=source,
            output_dir=tmp_path / "visualizations",
            mask_quality=False,
            debug_plot=False,
        )
    )
    domains = (
        "BLUE",
        "GREEN",
        "RED",
        "REDEDGE1",
        "REDEDGE2",
        "REDEDGE3",
        "BROADNIR",
        "NIR",
        "SWIR1",
        "SWIR2",
    )
    metadata = {
        "bands": [
            {
                "band": index,
                "type": "Int16",
                "noDataValue": -9999,
                "description": domain,
                "metadata": {"FORCE": {"Domain": domain}},
            }
            for index, domain in enumerate(domains, start=1)
        ]
    }

    with pytest.raises(ValueError, match="Native FORCE BOA"):
        visualizer._bands(metadata)


@pytest.mark.skipif(
    not HAS_GDAL or not HAS_PILLOW,
    reason="GDAL command-line tools and Pillow are required",
)
def test_force_visualizer_debug_plot_supports_legacy_pillow_font_api(tmp_path, monkeypatch):
    from PIL import Image, ImageFont

    input_path = tmp_path / "input.tif"
    input_path.write_bytes(b"label-only")
    quicklook = tmp_path / "quicklook.png"
    Image.new("RGBA", (32, 16), (20, 120, 40, 255)).save(quicklook)
    visualizer = ForceVisualizer(
        ForceVisualizationConfig(
            input_path=input_path,
            output_dir=tmp_path / "visualizations",
            debug_plot_width=640,
        )
    )
    original_load_default = ImageFont.load_default
    original_truetype = ImageFont.truetype

    def legacy_load_default(*args, **kwargs):
        if args or kwargs:
            raise TypeError("legacy Pillow has no size argument")
        return original_load_default()

    monkeypatch.setattr(ImageFont, "load_default", legacy_load_default)

    def missing_named_font(font, *args, **kwargs):
        if font == "DejaVuSans.ttf":
            raise OSError("font unavailable")
        return original_truetype(font, *args, **kwargs)

    monkeypatch.setattr(ImageFont, "truetype", missing_named_font)
    output = tmp_path / "comparison.png"

    visualizer._compose_debug_plot(
        raw_quicklook_path=quicklook,
        masked_quicklook_path=quicklook,
        force_quicklook_path=None,
        output_path=output,
        raw_valid_percent=100,
        valid_percent=90,
        force_valid_percent=None,
    )

    assert output.is_file()
