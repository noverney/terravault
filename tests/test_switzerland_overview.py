"""Tests for rendering the exact metadata-only selection stored in DuckDB."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pystac
from PIL import Image

from examples.switzerland_patch import build_switzerland_overview
from terravault.dataset_catalog import DatasetCatalog
from terravault.force_pipeline import ForcePipelineDatabase


def test_overview_can_be_built_from_saved_duckdb_items(tmp_path, monkeypatch):
    database_path = tmp_path / "dataset.duckdb"
    metadata_path = tmp_path / "scene_metadata.json"
    status_path = tmp_path / "job_status.json"
    output_path = tmp_path / "switzerland.jpg"
    item = pystac.Item(
        id="S2A_MSIL2A_20260813T102701_N0512_R108_T32TMT_20260813T170810",
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [5.96, 45.82],
                    [10.49, 45.82],
                    [10.49, 47.81],
                    [5.96, 47.81],
                    [5.96, 45.82],
                ]
            ],
        },
        bbox=[5.96, 45.82, 10.49, 47.81],
        datetime=datetime(2026, 8, 13, 10, 27, 1, tzinfo=timezone.utc),
        properties={"eo:cloud_cover": 8.5},
        collection="sentinel-2-l2a",
    )
    item.add_asset(
        "thumbnail",
        pystac.Asset("https://example.test/thumbnail.jpg", media_type="image/jpeg"),
    )
    metadata_path.write_text(json.dumps(item.to_dict()), encoding="utf-8")
    catalog = DatasetCatalog(database_path)
    catalog.upsert_item(item, metadata_path=metadata_path, status_path=status_path)
    catalog.sync_job_snapshot(
        {
            "item_id": item.id,
            "status": "completed",
            "attempts": 0,
            "last_error": None,
            "assets": [],
        }
    )

    monkeypatch.setattr(
        build_switzerland_overview,
        "_download_image",
        lambda _session, _url, _image: Image.new("RGB", (64, 64), (70, 130, 80)),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_switzerland_overview.py",
            "--dataset-db",
            str(database_path),
            "--max-cloud-cover",
            "20",
            "--width",
            "200",
            "--output",
            str(output_path),
        ],
    )

    assert build_switzerland_overview.main() == 0
    assert output_path.is_file()
    manifest = json.loads(output_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "duckdb-latest-per-tile"
    assert manifest["dataset_db"] == str(database_path.resolve())
    assert manifest["used_item_count"] == 1
    assert manifest["items"][0]["id"] == item.id
    assert catalog.dataset_info("latest_overview_path") == str(output_path.resolve())


def test_overview_can_be_built_from_force_pipeline_duckdb(tmp_path, monkeypatch):
    database_path = tmp_path / "force_images.duckdb"
    metadata_path = tmp_path / "scene_metadata.json"
    output_path = tmp_path / "force-switzerland.jpg"
    item = pystac.Item(
        id="S2A_MSIL1C_20260813T102701_N0512_R108_T32TMT_20260813T170810",
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [5.96, 45.82],
                    [10.49, 45.82],
                    [10.49, 47.81],
                    [5.96, 47.81],
                    [5.96, 45.82],
                ]
            ],
        },
        bbox=[5.96, 45.82, 10.49, 47.81],
        datetime=datetime(2026, 8, 13, 10, 27, 1, tzinfo=timezone.utc),
        properties={"eo:cloud_cover": 4.5},
        collection="sentinel-2-l1c",
    )
    item.add_asset(
        "thumbnail",
        pystac.Asset("https://example.test/thumbnail.jpg", media_type="image/jpeg"),
    )
    metadata_path.write_text(json.dumps(item.to_dict()), encoding="utf-8")
    with ForcePipelineDatabase(database_path) as catalog:
        catalog.upsert_scene(
            item,
            product_name=f"{item.id}.SAFE",
            metadata_path=metadata_path,
        )

    monkeypatch.setattr(
        build_switzerland_overview,
        "_download_image",
        lambda _session, _url, _image: Image.new("RGB", (64, 64), (70, 130, 80)),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_switzerland_overview.py",
            "--dataset-db",
            str(database_path),
            "--database-selection",
            "all",
            "--width",
            "200",
            "--output",
            str(output_path),
        ],
    )

    assert build_switzerland_overview.main() == 0
    manifest = json.loads(output_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "duckdb-all"
    assert manifest["dataset_kind"] == "force-pipeline"
    assert manifest["used_item_count"] == 1
    with ForcePipelineDatabase(database_path) as catalog:
        assert catalog.dataset_info("latest_overview_path") == str(
            output_path.resolve()
        )
