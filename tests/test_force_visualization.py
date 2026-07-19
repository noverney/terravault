"""Tests for FORCE NDVI visualization products."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from terravault.cli import _default_log_file, build_parser
from terravault.force_visualization import (
    ForceVisualizationConfig,
    ForceVisualizer,
)

GDAL_COMMANDS = ("gdal_create", "gdal_calc.py", "gdal_translate", "gdaldem", "gdalinfo")
HAS_GDAL = all(shutil.which(command) for command in GDAL_COMMANDS)


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
        ]
    )

    assert args.cloud_threshold == 35
    assert args.quicklook_width == 1400
    assert _default_log_file(args) == (
        output_dir / "_terravault/logs/force-visualize.log"
    )


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


@pytest.mark.skipif(not HAS_GDAL, reason="GDAL command-line tools are not installed")
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
    )

    result = ForceVisualizer(config).run()

    assert result.status == "complete"
    assert not result.skipped
    assert (result.width, result.height) == (20, 10)
    assert result.valid_percent == 100
    assert result.ndvi_path.is_file()
    assert result.quicklook_path.is_file()
    assert result.worldfile_path.is_file()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["bands"] == {"red": 1, "nir": 2, "scl": 3, "cloud": 4}
    assert manifest["mask_quality"]
    assert manifest["statistics"]["STATISTICS_MINIMUM"] == "0.5"
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
