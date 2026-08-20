"""Tests for the top-level DuckDB raster-piece catalogue."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import duckdb
import pystac

from terravault.cli import _default_log_file, _setup_logging, build_parser, cmd_query
from terravault.dataset_catalog import DatasetCatalog


def _catalog_item() -> pystac.Item:
    item = pystac.Item(
        id="S2A_MSIL2A_20260704T102031_N0511_R065_T32TNT_catalog",
        geometry={
            "type": "Polygon",
            "coordinates": [
                [[8.0, 46.0], [9.0, 46.0], [9.0, 47.0], [8.0, 46.0]]
            ],
        },
        bbox=[8.0, 46.0, 9.0, 47.0],
        datetime=datetime(2026, 7, 4, 10, 20, tzinfo=timezone.utc),
        properties={"eo:cloud_cover": 12.5},
        collection="sentinel-2-l2a",
    )
    item.add_asset(
        "B04_10m",
        pystac.Asset(
            "s3://eodata/scene/B04.jp2",
            media_type="image/jp2",
            roles=["data"],
            extra_fields={
                "proj:code": "EPSG:32632",
                "proj:shape": [10980, 10980],
                "proj:transform": [10, 0, 399960, 0, -10, 5300040],
                "file:size": 2048,
                "file:checksum": "1220source",
            },
        ),
    )
    return item


def test_duckdb_catalog_queries_partitioned_raster_paths(tmp_path):
    item = _catalog_item()
    database = tmp_path / "dataset.duckdb"
    metadata_path = tmp_path / "sentinel2-l2a/2026/07/04/32TNT/item/scene_metadata.json"
    status_path = metadata_path.with_name("job_status.json")
    raster_path = metadata_path.with_name("B04_10m.jp2")
    catalog = DatasetCatalog(database)
    catalog.upsert_item(
        item,
        metadata_path=metadata_path,
        status_path=status_path,
    )
    catalog.upsert_asset(
        item_id=item.id,
        asset_key="B04_10m",
        asset=item.assets["B04_10m"],
        local_path=raster_path,
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
                    "bytes": 1234,
                    "sha256": "d" * 64,
                }
            ],
        }
    )

    pieces = catalog.query_raster_pieces(
        bbox=(8.2, 46.2, 8.4, 46.4),
        start_datetime=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 7, 5, tzinfo=timezone.utc),
        asset_keys=["B04_10m"],
    )

    assert len(pieces) == 1
    assert pieces[0]["local_path"] == str(raster_path.resolve())
    assert pieces[0]["resolution_m"] == 10
    assert pieces[0]["proj_epsg"] == 32632
    assert pieces[0]["byte_count"] == 1234
    assert pieces[0]["expected_byte_count"] == 2048
    assert pieces[0]["source_checksum"] == "1220source"
    assert not catalog.query_raster_pieces(bbox=(0, 0, 1, 1))
    assert catalog.summary()["completed_bytes"] == 1234
    items = catalog.query_items(bbox=(8.2, 46.2, 8.4, 46.4))
    assert len(items) == 1
    assert items[0]["item_id"] == item.id
    assert items[0]["metadata_path"] == str(metadata_path.resolve())
    assert items[0]["cloud_cover"] == 12.5
    assert catalog.query_items(bbox=(0, 0, 1, 1)) == []
    catalog.set_dataset_info("overview_path", "/tmp/overview.jpg")
    assert catalog.dataset_info("overview_path") == "/tmp/overview.jpg"
    assert catalog.dataset_info()["schema_version"] == "2"

    connection = duckdb.connect(str(database), read_only=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM raster_pieces").fetchone()[0] == 1
    finally:
        connection.close()


def test_query_cli_outputs_local_paths(tmp_path, capsys):
    item = _catalog_item()
    database = tmp_path / "dataset.duckdb"
    raster_path = tmp_path / "pieces/B04_10m.jp2"
    catalog = DatasetCatalog(database)
    catalog.upsert_item(
        item,
        metadata_path=tmp_path / "scene_metadata.json",
        status_path=tmp_path / "job_status.json",
    )
    catalog.upsert_asset(
        item_id=item.id,
        asset_key="B04_10m",
        asset=item.assets["B04_10m"],
        local_path=raster_path,
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
                    "bytes": 1,
                    "sha256": "e" * 64,
                }
            ],
        }
    )
    args = build_parser().parse_args(
        [
            "query",
            "--dataset-db",
            str(database),
            "--bbox",
            "8.1",
            "46.1",
            "8.5",
            "46.5",
            "--asset-keys",
            "B04_10m",
        ]
    )

    assert cmd_query(args) == 0
    assert capsys.readouterr().out.strip() == str(raster_path.resolve())


def test_rotating_log_default_records_progress_and_paths(tmp_path):
    args = build_parser().parse_args(
        [
            "watch",
            "--bbox",
            "8",
            "46",
            "9",
            "47",
            "--storage-root",
            str(tmp_path / "dataset"),
        ]
    )
    expected = tmp_path / "dataset/_terravault/logs/watch.log"
    assert _default_log_file(args) == expected

    _setup_logging(False, expected)
    logging.getLogger("terravault.test").info(
        "Asset complete – path=%s progress=1/1", tmp_path / "piece.jp2"
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = expected.read_text(encoding="utf-8")
    assert "Asset complete" in text
    assert "progress=1/1" in text
    _setup_logging(False)
