"""Tests for network-free swissEO monthly dry runs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from terravault.cli import build_parser
from terravault.swisseo import SWISSEO_COLLECTION, SWISSEO_STAC_URL
from terravault.swisseo_monthly import (
    SwissEOMonthlyConfig,
    SwissEOMonthlyProcessor,
    _copy_file_sequential,
    _replace_after_reconnect,
)
from terravault.vegetation import REFLECTANCE_BAND_NAMES


def _item(item_id: str, timestamp: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": item_id,
        "collection": SWISSEO_COLLECTION,
        "bbox": [6.35, 45.73, 10.67, 47.91],
        "properties": {"datetime": timestamp},
        "assets": {
            "a": {"title": "BANDS-10M", "href": "https://example/bands10.tif"},
            "b": {"title": "BANDS-20M", "href": "https://example/bands20.tif"},
            "c": {"title": "MASKS-10M", "href": "https://example/masks.tif"},
            "d": {"title": "METADATA", "href": "https://example/metadata.json"},
        },
    }


def _metadata(*, masked: float, data: float, orbit: int, scenes: int) -> dict[str, Any]:
    return {
        "BANDS-10M": {
            "SOURCE_COLLECTION_PROPERTIES": {
                "percent_masked": masked,
                "percent_data": data,
                "SENSING_ORBIT_NUMBER": orbit,
                "scene_count": scenes,
            }
        }
    }


class FakeMonthlyClient:
    stac_url = SWISSEO_STAC_URL
    collection = SWISSEO_COLLECTION

    def __init__(self) -> None:
        self.items = (
            _item("2024-06-04t101559", "2024-06-04T10:15:59Z"),
            _item("2024-06-11t102031", "2024-06-11T10:20:31Z"),
        )
        self.search_calls: list[tuple[int, int]] = []
        self.metadata_calls: list[str] = []

    def search_month(self, *, year: int, month: int) -> tuple[dict[str, Any], ...]:
        self.search_calls.append((year, month))
        return self.items

    def get_metadata(self, item: dict[str, Any]) -> dict[str, Any]:
        item_id = str(item["id"])
        self.metadata_calls.append(item_id)
        if item_id.endswith("101559"):
            return _metadata(masked=20.0, data=80.0, orbit=65, scenes=11)
        return _metadata(masked=40.0, data=60.0, orbit=108, scenes=9)


def test_swisseo_monthly_dry_run_uses_all_items_and_records_timings(tmp_path: Path):
    client = FakeMonthlyClient()
    result = SwissEOMonthlyProcessor(
        SwissEOMonthlyConfig(
            output_root=tmp_path / "monthly",
            year=2024,
            month=6,
            bbox=(8.0, 46.0, 8.01, 46.01),
            dry_run=True,
        ),
        client=client,
    ).run()

    assert result.dry_run is True
    assert result.item_count == 2
    assert client.search_calls == [(2024, 6)]
    assert client.metadata_calls == ["2024-06-04t101559", "2024-06-11t102031"]
    assert not result.reflectance_path.exists()
    assert not result.indices_path.exists()

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["processing_option"] == "swisseo-monthly-composite"
    assert manifest["source"] == "swissEO-S2-SR-published-orbit-mosaics"
    assert manifest["source_is_force"] is False
    assert manifest["stac_authentication_required"] is False
    assert manifest["item_count"] == 2
    assert manifest["average_source_percent_masked"] == 30.0
    assert [source["orbit_number"] for source in manifest["sources"]] == [65, 108]
    assert [source["source_scene_count"] for source in manifest["sources"]] == [11, 9]
    assert manifest["timings_seconds"]["monthly_mosaic_generation"] == 0.0
    assert manifest["timings_seconds"]["index_calculation"] == 0.0
    assert manifest["timings_seconds"]["total"] >= 0.0
    assert manifest["publication_wait_seconds"] == 21_600.0
    assert manifest["publication_poll_seconds"] == 30.0


def test_swisseo_monthly_cli_accepts_publication_wait():
    args = build_parser().parse_args(
        [
            "swisseo-monthly",
            "--year",
            "2024",
            "--publication-wait-seconds",
            "900",
        ]
    )

    assert args.publication_wait_seconds == 900.0


def test_sequential_publication_waits_for_output_volume_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "local-stage.tif"
    source.write_bytes(b"locally completed raster")
    output_root = tmp_path / "disconnected-share"
    destination = output_root / "year=2024" / "month=06" / ".raster.partial.tif"
    sleeps: list[float] = []

    def reconnect(seconds: float) -> None:
        sleeps.append(seconds)
        output_root.mkdir()

    monkeypatch.setattr("terravault.swisseo_monthly.time.sleep", reconnect)

    _copy_file_sequential(
        source,
        destination,
        output_root=output_root,
        wait_seconds=10.0,
        poll_seconds=0.25,
    )

    assert sleeps == [0.25]
    assert destination.read_bytes() == source.read_bytes()


def test_publication_rename_waits_for_output_volume_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    output_root = tmp_path / "disconnected-share"
    partial = output_root / ".raster.partial.tif"
    destination = output_root / "raster.tif"

    def reconnect(_seconds: float) -> None:
        output_root.mkdir()
        partial.write_bytes(b"published bytes")

    monkeypatch.setattr("terravault.swisseo_monthly.time.sleep", reconnect)

    _replace_after_reconnect(
        partial,
        destination,
        output_root=output_root,
        wait_seconds=10.0,
        poll_seconds=0.25,
    )

    assert destination.read_bytes() == b"published bytes"
    assert not partial.exists()


def test_monthly_median_uses_one_spectral_band_per_calculation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    processor = SwissEOMonthlyProcessor(
        SwissEOMonthlyConfig(
            output_root=tmp_path / "monthly",
            year=2024,
            month=6,
            dry_run=True,
        ),
        client=FakeMonthlyClient(),
    )
    processor._gdal = {
        "gdal_calc.py": "gdal_calc.py",
        "gdal_translate": "gdal_translate",
        "gdalbuildvrt": "gdalbuildvrt",
    }
    aligned_items = [
        {
            **{
                band_name: tmp_path / f"item-{item_number}-{band_name.casefold()}.vrt"
                for band_name in REFLECTANCE_BAND_NAMES
            },
            "TERRAIN_MASK": tmp_path / f"item-{item_number}-terrain.vrt",
            "CLOUD_MASK": tmp_path / f"item-{item_number}-cloud.vrt",
        }
        for item_number in (1, 2, 3)
    ]
    commands: list[tuple[str, ...]] = []
    descriptions: list[tuple[Path, tuple[str, ...]]] = []

    monkeypatch.setattr(
        "terravault.swisseo_monthly.run_gdal",
        lambda command: commands.append(tuple(command)),
    )
    monkeypatch.setattr(
        "terravault.swisseo_monthly.set_vrt_band_descriptions",
        lambda path, names: descriptions.append((path, tuple(names))),
    )
    output = tmp_path / "monthly-median.tif"

    processor._monthly_reflectance_median(
        aligned_items=aligned_items,
        output=output,
    )

    calculations = [command for command in commands if command[0] == "gdal_calc.py"]
    assert len(calculations) == len(REFLECTANCE_BAND_NAMES)
    calculated_outputs: list[str] = []
    for band_name, command in zip(
        REFLECTANCE_BAND_NAMES,
        calculations,
        strict=True,
    ):
        a_start = command.index("-A") + 1
        a_end = command.index("--A_band")
        t_start = command.index("-T") + 1
        t_end = command.index("--T_band")
        q_start = command.index("-Q") + 1
        q_end = command.index("--Q_band")
        assert command[a_start:a_end] == tuple(str(item[band_name]) for item in aligned_items)
        assert command[t_start:t_end] == tuple(str(item["TERRAIN_MASK"]) for item in aligned_items)
        assert command[q_start:q_end] == tuple(str(item["CLOUD_MASK"]) for item in aligned_items)
        assert command.count("--calc") == 1
        assert "INTERLEAVE=BAND" in command
        calculated_outputs.append(command[command.index("--outfile") + 1])

    stack_command = commands[-2]
    assert stack_command[:8] == (
        "gdalbuildvrt",
        "-overwrite",
        "-separate",
        "-srcnodata",
        "-9999.0",
        "-vrtnodata",
        "-9999.0",
        str(tmp_path / "monthly-median.stack.vrt"),
    )
    assert stack_command[8:] == tuple(calculated_outputs)
    assert descriptions == [
        (
            tmp_path / "monthly-median.stack.vrt",
            REFLECTANCE_BAND_NAMES,
        )
    ]
    translate_command = commands[-1]
    assert translate_command[0] == "gdal_translate"
    assert translate_command[-2:] == (
        str(tmp_path / "monthly-median.stack.vrt"),
        str(output),
    )
    assert "INTERLEAVE=BAND" in translate_command


@pytest.mark.skipif(
    any(
        shutil.which(command) is None
        for command in (
            "gdal_create",
            "gdal_calc.py",
            "gdalbuildvrt",
            "gdalwarp",
            "gdalinfo",
        )
    ),
    reason="GDAL command-line tools are not installed",
)
def test_swisseo_monthly_runs_end_to_end_across_all_orbit_items(tmp_path: Path):
    items: list[dict[str, Any]] = []
    metadata: dict[str, dict[str, Any]] = {}
    for scene_number in (1, 2):
        item_id = f"2024-06-{scene_number:02d}t101559"
        scene_root = tmp_path / item_id
        scene_root.mkdir()
        assets: dict[str, dict[str, Any]] = {}
        raster_specs = {
            "BANDS-10M": (100, 50, [300, 400, 200, 700]),
            "BANDS-20M": (50, 25, [680, 350, 350]),
            "MASKS-10M": (100, 50, [0, 0]),
        }
        for title, (width, height, base_values) in raster_specs.items():
            path = scene_root / f"{title.casefold()}.tif"
            values = [
                value if title == "MASKS-10M" else value + (scene_number - 1) * 20
                for value in base_values
            ]
            command = [
                "gdal_create",
                "-of",
                "GTiff",
                "-ot",
                "UInt16" if title != "MASKS-10M" else "Byte",
                "-outsize",
                str(width),
                str(height),
                "-bands",
                str(len(values)),
            ]
            for value in values:
                command.extend(("-burn", str(value)))
            command.extend(
                (
                    "-a_srs",
                    "EPSG:4326",
                    "-a_ullr",
                    "7.0",
                    "47.0",
                    "8.0",
                    "46.5",
                    "-a_nodata",
                    "255" if title == "MASKS-10M" else "9999",
                    str(path),
                )
            )
            subprocess.run(command, check=True, capture_output=True, text=True)
            assets[title] = {"title": title, "href": str(path)}
        assets["METADATA"] = {
            "title": "METADATA",
            "href": str(scene_root / "metadata.json"),
        }
        items.append(
            {
                "type": "Feature",
                "id": item_id,
                "collection": SWISSEO_COLLECTION,
                "bbox": [7.0, 46.5, 8.0, 47.0],
                "properties": {"datetime": f"2024-06-{scene_number:02d}T10:15:59Z"},
                "assets": assets,
            }
        )
        metadata[item_id] = {
            "BANDS-10M": {
                "BANDS": [
                    {"id": "B4", "dimensions": [100, 50]},
                    {"id": "B3", "dimensions": [100, 50]},
                    {"id": "B2", "dimensions": [100, 50]},
                    {"id": "B8", "dimensions": [100, 50]},
                ],
                "SOURCE_COLLECTION_PROPERTIES": {
                    "percent_masked": 5.0,
                    "percent_data": 95.0,
                    "SENSING_ORBIT_NUMBER": 65,
                    "scene_count": 1,
                },
            },
            "BANDS-20M": {
                "BANDS": [
                    {"id": "B8A", "dimensions": [50, 25]},
                    {"id": "B11", "dimensions": [50, 25]},
                    {"id": "B5", "dimensions": [50, 25]},
                ]
            },
        }

    class LocalMonthlyClient:
        stac_url = SWISSEO_STAC_URL
        collection = SWISSEO_COLLECTION

        def search_month(self, *, year: int, month: int):
            assert (year, month) == (2024, 6)
            return tuple(items)

        def get_metadata(self, item: dict[str, Any]):
            return metadata[str(item["id"])]

    staging_root = tmp_path / "local-staging"
    result = SwissEOMonthlyProcessor(
        SwissEOMonthlyConfig(
            output_root=tmp_path / "monthly",
            year=2024,
            month=6,
            bbox=(7.0, 46.5, 8.0, 47.0),
            resolution=500.0,
            max_output_gib=1.0,
            max_temporary_gib=1.0,
            temporary_root=staging_root,
        ),
        client=LocalMonthlyClient(),
    ).run()

    assert result.reflectance_path.is_file()
    assert result.indices_path.is_file()
    assert result.item_count == 2
    assert result.estimated_peak_temporary_bytes > 0
    assert result.timings_seconds["monthly_mosaic_generation"] > 0
    assert result.timings_seconds["index_calculation"] > 0
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["temporary_root"] == str(staging_root.resolve())
    assert "copied sequentially" in manifest["temporary_strategy"]
    assert "six band-specific blockwise masked medians" in manifest["temporary_strategy"]
    assert "downloaded once" in manifest["temporary_strategy"]
    assert "removed before the" in manifest["temporary_strategy"]
    assert "disconnected output volume is retried" in manifest["temporary_strategy"]
    assert not tuple(staging_root.glob("terravault-swisseo-monthly-*"))
    info = json.loads(
        subprocess.run(
            ["gdalinfo", "-json", "-stats", str(result.indices_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    expected = (0.4 / 1.02, 0.3 / 1.12, 0.33 / 1.05, 0.35 / 1.07)
    assert [band["mean"] for band in info["bands"]] == pytest.approx(expected, abs=5e-4)
