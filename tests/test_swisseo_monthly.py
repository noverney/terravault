"""Tests for network-free swissEO monthly dry runs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from terravault.cli import build_parser
from terravault.swisseo import SWISSEO_COLLECTION, SWISSEO_STAC_URL, SWISSEO_V200_COLLECTION
from terravault.swisseo_monthly import (
    SwissEOMonthlyConfig,
    SwissEOMonthlyProcessor,
    _copy_file_sequential,
    _replace_after_reconnect,
)
from terravault.vegetation import REFLECTANCE_BAND_NAMES

requires_gdal_cli = pytest.mark.skipif(
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
        self.search_bboxes: list[Any] = []
        self.metadata_calls: list[str] = []

    def search_month(
        self, *, year: int, month: int, bbox=None
    ) -> tuple[dict[str, Any], ...]:
        self.search_calls.append((year, month))
        self.search_bboxes.append(bbox)
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
    assert client.search_bboxes == [(8.0, 46.0, 8.01, 46.01)], "orbits are filtered by bbox"
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


def _build_local_month(tmp_path: Path):
    """Build two on-disk swissEO orbit mosaics and a client that serves them."""

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

        def search_month(self, *, year: int, month: int, bbox=None):
            assert (year, month) == (2024, 6)
            return tuple(items)

        def get_metadata(self, item: dict[str, Any]):
            return metadata[str(item["id"])]

    return LocalMonthlyClient()


@requires_gdal_cli
def test_swisseo_monthly_runs_end_to_end_across_all_orbit_items(tmp_path: Path):
    client = _build_local_month(tmp_path)
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
        client=client,
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


@requires_gdal_cli
def test_interrupted_run_keeps_aligned_items_and_resumes_from_them(tmp_path: Path):
    """A failure after alignment must not discard the staged orbit mosaics.

    Aligning one orbit costs a multi-gigabyte download plus three warps, and a
    single transient read timeout from the publication host used to throw all
    of it away with the TemporaryDirectory.
    """

    client = _build_local_month(tmp_path)
    staging_root = tmp_path / "local-staging"
    config = SwissEOMonthlyConfig(
        output_root=tmp_path / "monthly",
        year=2024,
        month=6,
        bbox=(7.0, 46.5, 8.0, 47.0),
        resolution=500.0,
        max_output_gib=1.0,
        max_temporary_gib=1.0,
        temporary_root=staging_root,
    )

    class Interrupted(RuntimeError):
        pass

    def explode(self, **kwargs):
        raise Interrupted("publication host dropped the connection")

    original = SwissEOMonthlyProcessor._monthly_reflectance_median
    SwissEOMonthlyProcessor._monthly_reflectance_median = explode
    try:
        with pytest.raises(Interrupted):
            SwissEOMonthlyProcessor(config, client=client).run()
    finally:
        SwissEOMonthlyProcessor._monthly_reflectance_median = original

    checkpoint = staging_root / "checkpoints" / config.stem
    survivors = sorted(path.name for path in checkpoint.glob("item-*"))
    assert survivors == ["item-001", "item-002"], (
        f"expected both aligned items to survive the failure, got {survivors}"
    )
    assert (checkpoint / "item-001" / "aligned.json").is_file()
    # No download must be repeated on the resumed run.
    downloads: list[str] = []
    original_download = SwissEOMonthlyProcessor._download_asset

    def counting_download(self, *, href, output):
        downloads.append(output.name)
        return original_download(self, href=href, output=output)

    SwissEOMonthlyProcessor._download_asset = counting_download
    try:
        result = SwissEOMonthlyProcessor(config, client=client).run()
    finally:
        SwissEOMonthlyProcessor._download_asset = original_download

    assert result.reflectance_path.is_file()
    assert result.indices_path.is_file()
    assert downloads == [], f"resumed run re-staged assets: {downloads}"
    # A finished month must not leave its staging behind.
    assert not checkpoint.exists()


def test_v200_is_opt_in_and_never_reuses_v100_names(tmp_path: Path):
    v100 = SwissEOMonthlyConfig(output_root=tmp_path, year=2023, month=7, resolution=10.0)
    v200 = SwissEOMonthlyConfig(
        output_root=tmp_path, year=2023, month=7, resolution=10.0, product_version="v200"
    )

    assert v100.product_version == "v100"
    assert v100.collection == SWISSEO_COLLECTION
    assert v100.stem == "switzerland_swisseo_s2_sr_2023_07_10m"
    assert v200.collection == SWISSEO_V200_COLLECTION
    assert v200.stem == "switzerland_swisseo_s2_sr_v200_2023_07_10m"
    # A v200 quality rule must never be pointed at another collection's data.
    with pytest.raises(ValueError, match="reads"):
        SwissEOMonthlyConfig(
            output_root=tmp_path, year=2023, month=7,
            product_version="v200", collection="ch.example.other",
        )
    with pytest.raises(ValueError, match="Unknown swissEO product version"):
        SwissEOMonthlyConfig(output_root=tmp_path, year=2023, month=7, product_version="v300")


def test_swisseo_monthly_cli_defaults_to_v100_and_accepts_v200():
    default = build_parser().parse_args(["swisseo-monthly", "--year", "2023"])
    chosen = build_parser().parse_args(
        ["swisseo-monthly", "--year", "2023", "--product-version", "v200"]
    )

    assert default.product_version == "v100"
    assert default.collection is None
    assert chosen.product_version == "v200"


def test_median_expression_follows_the_product_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def expression_for(version: str) -> str:
        processor = SwissEOMonthlyProcessor(
            SwissEOMonthlyConfig(
                output_root=tmp_path / version, year=2024, month=6,
                dry_run=True, product_version=version,
            ),
            client=FakeMonthlyClient(),
        )
        processor._gdal = {
            "gdal_calc.py": "gdal_calc.py",
            "gdal_translate": "gdal_translate",
            "gdalbuildvrt": "gdalbuildvrt",
        }
        commands: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            "terravault.swisseo_monthly.run_gdal", lambda command: commands.append(tuple(command))
        )
        monkeypatch.setattr(
            "terravault.swisseo_monthly.set_vrt_band_descriptions", lambda path, names: None
        )
        aligned = [
            {
                **{band: tmp_path / f"{n}-{band}.vrt" for band in REFLECTANCE_BAND_NAMES},
                "TERRAIN_MASK": tmp_path / f"{n}-t.vrt",
                "CLOUD_MASK": tmp_path / f"{n}-q.vrt",
            }
            for n in (1, 2)
        ]
        processor._monthly_reflectance_median(
            aligned_items=aligned, output=tmp_path / f"{version}.tif"
        )
        calc = next(command for command in commands if command[0] == "gdal_calc.py")
        return calc[calc.index("--calc") + 1]

    v100 = expression_for("v100")
    v200 = expression_for("v200")

    # v100 must keep exactly the rule and scaling it always had.
    assert "A!=9999,T!=255,Q!=255,Q!=1,T!=100" in v100
    assert "A*0.001," in v100
    assert "A!=0,T<90,Q==0" in v200
    assert "(A*0.0001-0.1)" in v200


def _build_local_v200_month(tmp_path: Path):
    """Four v200 orbits where only the first one is usable.

    Each of the other three is rejected by exactly one part of the v200 rule
    and carries values that would visibly move the median if it leaked in:
    thin cloud (class 2 - survives a rule that only drops thick cloud),
    a self-shadowed slope (incidence 120 - survives a rule that only drops the
    cast-shadow value), and cast shadow (200).
    """

    usable = {"b03": 1700, "b04": 1500, "b08": 4000, "b05": 1800, "b8a": 4200, "b11": 2800}
    leaky = {"b03": 9000, "b04": 9000, "b08": 1100, "b05": 9000, "b8a": 1100, "b11": 9000}
    orbits = [("2024-06-01t101559", usable, 0, 45),
              ("2024-06-02t101559", leaky, 2, 45),
              ("2024-06-03t101559", leaky, 0, 120),
              ("2024-06-04t101559", leaky, 0, 200)]

    def raster(path: Path, size: tuple[int, int], value: int, kind: str, nodata: str | None):
        command = ["gdal_create", "-of", "GTiff", "-ot", kind,
                   "-outsize", str(size[0]), str(size[1]), "-bands", "1", "-burn", str(value),
                   "-a_srs", "EPSG:4326", "-a_ullr", "7.0", "47.0", "8.0", "46.5"]
        if nodata is not None:
            command.extend(("-a_nodata", nodata))
        subprocess.run([*command, str(path)], check=True, capture_output=True, text=True)

    items: list[dict[str, Any]] = []
    for item_id, values, cloud, terrain in orbits:
        root = tmp_path / item_id
        root.mkdir()
        assets: dict[str, dict[str, Any]] = {}

        def add(name: str, size, value: int, kind: str, nodata: str | None) -> None:
            key = f"swisseo_s2-sr_v200_mosaic_{item_id}_{name}.tif"
            raster(root / key, size, value, kind, nodata)
            assets[key] = {"title": name, "href": str(root / key)}

        for band in ("b03", "b04", "b08"):
            add(f"{band}_10m", (100, 50), values[band], "UInt16", "0")
        for band in ("b05", "b8a", "b11"):
            add(f"{band}_20m", (50, 25), values[band], "UInt16", "0")
        add("cloudmask_10m", (100, 50), cloud, "Byte", None)
        add("terrainmask_10m", (100, 50), terrain, "Byte", "255")
        assets["metadata"] = {"title": "Metadata", "href": str(root / "metadata.json")}
        items.append({
            "type": "Feature", "id": item_id, "collection": SWISSEO_V200_COLLECTION,
            "bbox": [7.0, 46.5, 8.0, 47.0],
            "properties": {"datetime": f"{item_id[:10]}T10:15:59Z"}, "assets": assets,
        })

    class LocalV200Client:
        stac_url = SWISSEO_STAC_URL
        collection = SWISSEO_V200_COLLECTION

        def search_month(self, *, year: int, month: int, bbox=None):
            return tuple(items)

        def get_metadata(self, item: dict[str, Any]):
            return {"PROPERTIES": {"CLOUDPERCENTAGE": "12.5"},
                    "SOURCE": {"GRANULES": {"g1": {}, "g2": {}}}}

    return LocalV200Client()


@requires_gdal_cli
def test_v200_month_applies_offset_and_rejects_cloud_and_terrain_shadow(tmp_path: Path):
    result = SwissEOMonthlyProcessor(
        SwissEOMonthlyConfig(
            output_root=tmp_path / "monthly",
            year=2024,
            month=6,
            bbox=(7.0, 46.5, 8.0, 47.0),
            resolution=500.0,
            max_output_gib=1.0,
            max_temporary_gib=1.0,
            temporary_root=tmp_path / "staging",
            product_version="v200",
        ),
        client=_build_local_v200_month(tmp_path),
    ).run()

    assert "_s2_sr_v200_" in result.reflectance_path.name
    assert result.item_count == 4
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["product_version"] == "v200"
    assert manifest["collection"] == SWISSEO_V200_COLLECTION
    assert manifest["reflectance_scaling"] == {"scale": 0.0001, "offset": -0.1, "nodata": 0}
    assert manifest["average_source_percent_cloud"] == pytest.approx(12.5)
    assert manifest["average_source_percent_masked"] is None

    def means(path: Path) -> list[float]:
        info = json.loads(subprocess.run(
            ["gdalinfo", "-json", "-stats", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout)
        return [band["mean"] for band in info["bands"]]

    # Only the clear orbit survives, so the median is exactly its reflectance:
    # stored value x 0.0001 - 0.1.  Without the offset B04 would read 0.15.
    assert means(result.reflectance_path) == pytest.approx(
        [0.07, 0.05, 0.08, 0.30, 0.32, 0.18], abs=5e-4
    )  # B03 B04 B05 B08 B8A B11
    # NDVI would be 0.4545 without the offset, and any leaked orbit drags it down.
    assert means(result.indices_path) == pytest.approx(
        [0.25 / 0.35, 0.23 / 0.37, 0.24 / 0.40, 0.12 / 0.48], abs=5e-4
    )  # NDVI GNDVI NDRE NDWI


def _windowing_processor(monkeypatch, tmp_path: Path, *, size, envelope):
    processor = SwissEOMonthlyProcessor(
        SwissEOMonthlyConfig(
            output_root=tmp_path / "monthly",
            year=2024,
            month=7,
            bbox=(8.17, 46.975, 8.47, 47.155),
        ),
        client=FakeMonthlyClient(),
    )
    metadata = {
        "size": list(size),
        "geoTransform": [2_480_000.0, 10.0, 0.0, 1_300_000.0, 0.0, -10.0],
        "coordinateSystem": {"wkt": "LV95"},
    }
    inspected: list[str] = []
    commands: list[list[str]] = []

    def fake_inspect(_gdalinfo, path):
        inspected.append(str(path))
        return metadata

    def fake_run_gdal(command, **_kwargs):
        commands.append(list(command))
        Path(command[-1]).write_bytes(b"window")

    monkeypatch.setattr("terravault.swisseo_monthly.inspect_raster", fake_inspect)
    monkeypatch.setattr(
        "terravault.swisseo_monthly.bbox_envelope", lambda _tool, _bbox, _srs: envelope
    )
    monkeypatch.setattr("terravault.swisseo_monthly.run_gdal", fake_run_gdal)
    return processor, inspected, commands


def test_small_bbox_reads_a_window_of_the_remote_cog_instead_of_the_file(
    monkeypatch, tmp_path: Path
):
    # A 23 x 20 km patch inside a 450 x 230 km national COG.
    processor, inspected, commands = _windowing_processor(
        monkeypatch,
        tmp_path,
        size=(45_000, 23_000),
        envelope=(2_650_000.0, 1_200_000.0, 2_673_000.0, 1_220_000.0),
    )

    def no_download(*_args, **_kwargs):
        raise AssertionError("the whole national file must not be downloaded")

    monkeypatch.setattr("requests.get", no_download)
    href = "https://data.geo.admin.ch/b04_10m.tif"
    output = tmp_path / "item" / "source-b04_10m.tif"

    assert processor._download_asset(href=href, output=output) == output

    assert inspected == [f"/vsicurl/{href}"]
    (command,) = commands
    srcwin = command.index("-srcwin")
    assert command[srcwin + 1 : srcwin + 5] == ["16992", "7992", "2316", "2016"]
    assert command[-2] == f"/vsicurl/{href}"
    assert output.read_bytes() == b"window"
    assert not list(output.parent.glob(".*partial*"))
    assert processor._fetch_modes == {"bbox-window"}


def test_large_bbox_keeps_the_whole_file_download(monkeypatch, tmp_path: Path):
    processor, _, commands = _windowing_processor(
        monkeypatch,
        tmp_path,
        size=(45_000, 23_000),
        envelope=(2_480_000.0, 1_070_000.0, 2_930_000.0, 1_300_000.0),
    )

    assert processor._bbox_window("https://data.geo.admin.ch/b04_10m.tif") is None
    assert commands == []


def test_failed_window_read_is_retried_from_scratch(monkeypatch, tmp_path: Path):
    processor, _, _ = _windowing_processor(
        monkeypatch,
        tmp_path,
        size=(45_000, 23_000),
        envelope=(2_650_000.0, 1_200_000.0, 2_673_000.0, 1_220_000.0),
    )
    attempts: list[bool] = []

    def flaky_run_gdal(command, **_kwargs):
        # Record whether a previous attempt's partial file was still there.
        attempts.append(Path(command[-1]).exists())
        Path(command[-1]).write_bytes(b"truncated" if len(attempts) == 1 else b"window")
        if len(attempts) == 1:
            raise RuntimeError("GDAL command failed: HTTP 503")

    monkeypatch.setattr("terravault.swisseo_monthly.run_gdal", flaky_run_gdal)
    monkeypatch.setattr("terravault.swisseo_monthly.time.sleep", lambda _seconds: None)
    output = tmp_path / "item" / "source-b04_10m.tif"

    processor._download_asset(href="https://data.geo.admin.ch/b04_10m.tif", output=output)

    assert attempts == [False, False], "a truncated partial file is removed before retrying"
    assert output.read_bytes() == b"window"
    assert not list(output.parent.glob(".*partial*"))
