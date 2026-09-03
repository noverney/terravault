"""Focused tests for source-neutral vegetation-index raster helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from terravault.vegetation import (
    VEGETATION_INDEX_NAMES,
    locate_gdal,
    run_gdal,
    write_vegetation_indices,
)


def test_run_gdal_adds_resilient_http_defaults_without_overriding_user_values(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_environment: dict[str, str] = {}

    def fake_run(arguments, **kwargs):
        captured_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setenv("GDAL_HTTP_TIMEOUT", "45")
    monkeypatch.delenv("GDAL_HTTP_MAX_RETRY", raising=False)
    monkeypatch.delenv("GDAL_HTTP_RETRY_DELAY", raising=False)
    monkeypatch.setattr("terravault.vegetation.subprocess.run", fake_run)

    run_gdal(("gdalinfo", "remote.tif"))

    assert captured_environment["GDAL_HTTP_TIMEOUT"] == "45"
    assert captured_environment["GDAL_HTTP_MAX_RETRY"] == "6"
    assert captured_environment["GDAL_HTTP_RETRY_DELAY"] == "2"
    assert captured_environment["GDAL_HTTP_MULTIRANGE"] == "YES"
    assert captured_environment["GDAL_HTTP_MERGE_CONSECUTIVE_RANGES"] == "YES"


def test_index_calculations_read_only_their_two_required_bands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    reflectance = tmp_path / "reflectance.tif"
    output = tmp_path / "indices.tif"
    commands: list[tuple[str, ...]] = []
    cog_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "terravault.vegetation.run_gdal",
        lambda command: commands.append(tuple(command)),
    )
    monkeypatch.setattr(
        "terravault.vegetation.write_described_cog",
        lambda **kwargs: cog_calls.append(kwargs),
    )

    write_vegetation_indices(
        reflectance_path=reflectance,
        output_path=output,
        gdal={
            "gdal_calc.py": "gdal_calc.py",
            "gdalbuildvrt": "gdalbuildvrt",
            "gdal_translate": "gdal_translate",
        },
        metadata={"SOURCE": "test"},
    )

    calculations = commands[: len(VEGETATION_INDEX_NAMES)]
    expected_bands = (
        ("4", "2"),  # NDVI: B08, B04
        ("4", "1"),  # GNDVI: B08, B03
        ("5", "3"),  # NDRE: B8A, B05
        ("4", "6"),  # NDWI: B08, B11
    )
    calculated_outputs: list[str] = []
    for command, (positive_band, negative_band) in zip(
        calculations,
        expected_bands,
        strict=True,
    ):
        assert command[0] == "gdal_calc.py"
        assert command.count(str(reflectance)) == 2
        assert command[command.index("--A_band") + 1] == positive_band
        assert command[command.index("--B_band") + 1] == negative_band
        assert not any(f"-{variable}" in command for variable in "CDEFGHIJKLMNOPQRSTUVWXYZ")
        assert command.count("--calc") == 1
        assert "(A-B)/(A+B)" in command[command.index("--calc") + 1]
        assert "INTERLEAVE=BAND" in command
        calculated_outputs.append(command[command.index("--outfile") + 1])

    stack_command = commands[len(VEGETATION_INDEX_NAMES)]
    assert stack_command[:7] == (
        "gdalbuildvrt",
        "-overwrite",
        "-separate",
        "-srcnodata",
        "-9999.0",
        "-vrtnodata",
        "-9999.0",
    )
    assert stack_command[8:] == tuple(calculated_outputs)
    assert len(cog_calls) == 1
    assert cog_calls[0]["source_path"] == Path(stack_command[7])
    assert cog_calls[0]["output_path"] == output
    assert cog_calls[0]["band_names"] == VEGETATION_INDEX_NAMES
    assert cog_calls[0]["metadata"] == {"SOURCE": "test"}


@pytest.mark.skipif(
    any(
        shutil.which(command) is None
        for command in ("gdal_create", "gdal_calc.py", "gdalbuildvrt", "gdal_translate", "gdalinfo")
    ),
    reason="GDAL command-line tools are not installed",
)
def test_index_stack_preserves_values_order_descriptions_and_metadata(tmp_path: Path):
    reflectance = tmp_path / "reflectance.tif"
    output = tmp_path / "indices.tif"
    subprocess.run(
        (
            "gdal_create",
            "-of",
            "GTiff",
            "-outsize",
            "8",
            "6",
            "-bands",
            "6",
            "-ot",
            "Float32",
            "-a_srs",
            "EPSG:2056",
            "-a_ullr",
            "2600000",
            "1200000",
            "2600080",
            "1199940",
            "-a_nodata",
            "-9999",
            "-burn",
            "0.3",
            "-burn",
            "0.4",
            "-burn",
            "0.45",
            "-burn",
            "0.9",
            "-burn",
            "0.85",
            "-burn",
            "0.5",
            str(reflectance),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    gdal = locate_gdal("gdal_calc.py", "gdalbuildvrt", "gdal_translate")

    write_vegetation_indices(
        reflectance_path=reflectance,
        output_path=output,
        gdal=gdal,
        metadata={"TERRAVAULT_TEST": "preserved"},
    )

    info = json.loads(
        subprocess.run(
            ("gdalinfo", "-json", "-stats", str(output)),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    expected = (
        (0.9 - 0.4) / (0.9 + 0.4),
        (0.9 - 0.3) / (0.9 + 0.3),
        (0.85 - 0.45) / (0.85 + 0.45),
        (0.9 - 0.5) / (0.9 + 0.5),
    )
    assert [band["mean"] for band in info["bands"]] == pytest.approx(expected, abs=5e-4)
    assert [band["description"] for band in info["bands"]] == list(VEGETATION_INDEX_NAMES)
    assert all(band["noDataValue"] == -9999.0 for band in info["bands"])
    assert info["metadata"][""]["TERRAVAULT_TEST"] == "preserved"
    assert info["metadata"]["IMAGE_STRUCTURE"]["INTERLEAVE"] == "BAND"
    assert info["metadata"]["IMAGE_STRUCTURE"]["LAYOUT"] == "COG"
