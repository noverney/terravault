"""Tests for post-FORCE monthly compositing."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from terravault.force_pipeline import ForcePipelineDatabase
from terravault.monthly import (
    FORCE_BOA_DOMAINS,
    FORCE_REFLECTANCE_BAND_MAP,
    MONTHLY_BAND_NAMES,
    MONTHLY_INDEX_NAMES,
    MonthlyCompositeConfig,
    MonthlyCompositeProcessor,
    calendar_month_window,
    discover_force_month,
)
from terravault.vegetation import VEGETATION_INDEX_DEFINITIONS, calculate_index_values


def _force_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "force_images.duckdb"
    boa = tmp_path / "complete" / "mosaic" / "20240615_LEVEL2_SEN2A_BOA.vrt"
    qai = tmp_path / "complete" / "mosaic" / "20240615_LEVEL2_SEN2A_QAI.vrt"
    boa.parent.mkdir(parents=True)
    boa.write_text("<VRTDataset/>", encoding="utf-8")
    qai.write_text("<VRTDataset/>", encoding="utf-8")
    now = datetime.now(timezone.utc).isoformat()
    with ForcePipelineDatabase(database_path) as database:
        for item_id, acquisition, force_status in (
            ("complete", "2024-06-15T10:00:00+00:00", "complete"),
            ("failed", "2024-06-16T10:00:00+00:00", "failed"),
            ("outside", "2024-07-02T10:00:00+00:00", "complete"),
        ):
            database.connection.execute(
                """
                INSERT INTO scenes(
                    item_id, product_name, acquisition_time, platform, mgrs_tile,
                    metadata_path, download_status, force_status, selected_at, updated_at
                ) VALUES (?, ?, ?, 'S2A', '32TMT', ?, 'complete', ?, ?, ?)
                """,
                [item_id, f"{item_id}.SAFE", acquisition, f"{item_id}.json", force_status, now, now],
            )
        for image_type, path in (("BOA_MOSAIC", boa), ("QAI_MOSAIC", qai)):
            database.connection.execute(
                """
                INSERT INTO images(item_id, image_type, local_path, byte_count, created_at)
                VALUES ('complete', ?, ?, ?, ?)
                """,
                [image_type, str(path), path.stat().st_size, now],
            )
    return database_path


def test_calendar_month_window_is_half_open_and_handles_december():
    start, end = calendar_month_window(2024, 6)
    assert start == datetime(2024, 6, 1, tzinfo=timezone.utc)
    assert end == datetime(2024, 7, 1, tzinfo=timezone.utc)
    start, end = calendar_month_window(2024, 12)
    assert start == datetime(2024, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_force_band_mapping_distinguishes_broad_and_narrow_nir():
    assert FORCE_BOA_DOMAINS == (
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
    assert FORCE_REFLECTANCE_BAND_MAP == {
        "B03": ("GREEN", 2),
        "B04": ("RED", 3),
        "B05": ("REDEDGE1", 4),
        "B08": ("BROADNIR", 7),
        "B8A": ("NIR", 8),
        "B11": ("SWIR1", 9),
    }


def test_index_equations_are_source_neutral_and_match_reference():
    assert MONTHLY_BAND_NAMES == ("B03", "B04", "B05", "B08", "B8A", "B11")
    assert MONTHLY_INDEX_NAMES == ("NDVI", "GNDVI", "NDRE", "NDWI")
    assert VEGETATION_INDEX_DEFINITIONS["NDWI"]["formula"] == (
        "(B08 - B11) / (B08 + B11)"
    )
    values = calculate_index_values(
        {"B03": 0.3, "B04": 0.4, "B05": 0.45, "B08": 0.9, "B8A": 0.85, "B11": 0.5}
    )
    assert values["NDVI"] == (0.9 - 0.4) / (0.9 + 0.4)
    assert values["GNDVI"] == (0.9 - 0.3) / (0.9 + 0.3)
    assert values["NDRE"] == (0.85 - 0.45) / (0.85 + 0.45)
    assert values["NDWI"] == (0.9 - 0.5) / (0.9 + 0.5)


def test_discovery_pairs_only_completed_native_force_mosaics(tmp_path):
    database_path = _force_database(tmp_path)
    discovery = discover_force_month(year=2024, month=6, force_db=database_path)

    assert [scene.item_id for scene in discovery.scenes] == ["complete"]
    assert discovery.scenes[0].boa_path.name.endswith("_BOA.vrt")
    assert discovery.scenes[0].qai_path.name.endswith("_QAI.vrt")
    assert discovery.skipped_items["failed"] == (
        "force-status:failed",
        "boa-mosaic-count:0",
        "qai-mosaic-count:0",
    )
    assert discovery.to_dict()["source"] == "native-force-level2-boa-qai"


def test_dry_run_manifest_is_explicitly_post_force(tmp_path):
    database_path = _force_database(tmp_path)
    result = MonthlyCompositeProcessor(
        MonthlyCompositeConfig(
            force_db=database_path,
            output_root=tmp_path / "monthly",
            year=2024,
            month=6,
            dry_run=True,
        )
    ).run()

    assert result.dry_run is True
    assert result.scene_count == 1
    assert not result.reflectance_path.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["processing_option"] == "post-force-monthly-composite"
    assert manifest["source"] == "native-force-level2-boa-qai"
    assert manifest["source_is_swisseo"] is False
    assert manifest["source_is_treeai"] is False
    assert manifest["quality_policy"]["force_qai_invalid_bit_mask_hex"] == "0x031F"
    assert manifest["reflectance_band_order"] == list(MONTHLY_BAND_NAMES)
    assert manifest["timings_seconds"]["total"] >= 0


def test_scene_reflectance_command_uses_force_boa_and_qai(monkeypatch, tmp_path):
    database_path = _force_database(tmp_path)
    scene = discover_force_month(year=2024, month=6, force_db=database_path).scenes[0]
    processor = MonthlyCompositeProcessor(
        MonthlyCompositeConfig(
            force_db=database_path,
            output_root=tmp_path / "monthly",
            year=2024,
            month=6,
        )
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(processor, "_validate_scene", lambda _scene: {})
    monkeypatch.setattr(
        processor,
        "_windowed_sources",
        lambda scene, _boa, _directory: (scene.boa_path, scene.qai_path),
    )
    monkeypatch.setattr("terravault.monthly.run_gdal", lambda command: commands.append(list(command)))

    assert processor._scene_reflectance(scene, tmp_path / "scene.tif") is True

    command = commands[0]
    assert str(scene.boa_path) in command
    assert str(scene.qai_path) in command
    calculations = [command[index + 1] for index, value in enumerate(command) if value == "--calc"]
    assert all("bitwise_and(Q,799)==0" in expression for expression in calculations)
    assert any("A*0.0001" in expression for expression in calculations)


def _boa_metadata() -> dict:
    # One FORCE granule mosaic: 11,000 x 11,000 pixels at 10 m in LV95.
    return {
        "size": [11_000, 11_000],
        "geoTransform": [2_600_000.0, 10.0, 0.0, 1_260_000.0, 0.0, -10.0],
        "coordinateSystem": {"wkt": "LV95"},
    }


def _windowing_processor(monkeypatch, tmp_path, envelope):
    database_path = _force_database(tmp_path)
    scene = discover_force_month(year=2024, month=6, force_db=database_path).scenes[0]
    processor = MonthlyCompositeProcessor(
        MonthlyCompositeConfig(
            force_db=database_path,
            output_root=tmp_path / "monthly",
            year=2024,
            month=6,
            bbox=(8.17, 46.975, 8.47, 47.155),
        )
    )
    envelopes: list[tuple] = []

    def fake_envelope(_gdaltransform, bbox, srs):
        envelopes.append((tuple(bbox), srs))
        return envelope

    commands: list[list[str]] = []
    monkeypatch.setattr("terravault.monthly.bbox_envelope", fake_envelope)
    monkeypatch.setattr("terravault.monthly.run_gdal", lambda command: commands.append(list(command)))
    return processor, scene, commands, envelopes


def test_windowed_sources_cut_boa_and_qai_to_the_bbox_on_the_native_grid(
    monkeypatch, tmp_path
):
    processor, scene, commands, envelopes = _windowing_processor(
        monkeypatch, tmp_path, (2_650_005.0, 1_200_000.0, 2_673_000.0, 1_220_000.0)
    )

    boa, qai = processor._windowed_sources(scene, _boa_metadata(), tmp_path / "scene")

    assert (boa, qai) == (tmp_path / "scene" / "boa.vrt", tmp_path / "scene" / "qai.vrt")
    # 5000.5 px from the left edge floors to 5000, minus the 4-pixel buffer.
    window = ["-srcwin", "4996", "3996", "2308", "2008"]
    assert commands == [
        [processor._gdal["gdal_translate"], "-of", "VRT", *window, str(scene.boa_path), str(boa)],
        [processor._gdal["gdal_translate"], "-of", "VRT", *window, str(scene.qai_path), str(qai)],
    ]
    # The envelope is computed once per CRS, not once per scene.
    processor._windowed_sources(scene, _boa_metadata(), tmp_path / "again")
    assert envelopes == [((8.17, 46.975, 8.47, 47.155), "LV95")]


def test_windowed_sources_keep_the_original_when_the_bbox_covers_the_scene(
    monkeypatch, tmp_path
):
    processor, scene, commands, _ = _windowing_processor(
        monkeypatch, tmp_path, (2_500_000.0, 1_100_000.0, 2_800_000.0, 1_300_000.0)
    )

    sources = processor._windowed_sources(scene, _boa_metadata(), tmp_path / "scene")

    assert sources == (scene.boa_path, scene.qai_path)
    assert commands == []


def test_scene_outside_the_bbox_is_skipped_without_screening(monkeypatch, tmp_path):
    processor, scene, commands, _ = _windowing_processor(
        monkeypatch, tmp_path, (2_800_000.0, 1_100_000.0, 2_810_000.0, 1_110_000.0)
    )
    monkeypatch.setattr(processor, "_validate_scene", lambda _scene: _boa_metadata())

    assert processor._scene_reflectance(scene, tmp_path / "scene.tif") is False
    assert commands == []
