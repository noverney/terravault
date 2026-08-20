"""Top-level DuckDB catalogue for partitioned TerraVault raster pieces."""

from __future__ import annotations

import fcntl
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlparse

import duckdb
import pystac

from .catalog import CatalogClient
from .storage import tile_id_from_item

logger = logging.getLogger(__name__)

_RESOLUTION_KEY = re.compile(r"_(?P<metres>\d+)m$")
_RASTER_SUFFIXES = {".jp2", ".tif", ".tiff", ".vrt"}


def _absolute(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _asset_resolution(asset_key: str, asset: pystac.Asset) -> float | None:
    match = _RESOLUTION_KEY.search(asset_key)
    if match:
        return float(match.group("metres"))
    value = asset.extra_fields.get("gsd")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _asset_epsg(asset: pystac.Asset) -> int | None:
    value = asset.extra_fields.get("proj:epsg")
    if isinstance(value, int):
        return value
    code = asset.extra_fields.get("proj:code")
    if isinstance(code, str) and code.upper().startswith("EPSG:"):
        suffix = code.split(":", 1)[1]
        if suffix.isdigit():
            return int(suffix)
    return None


def _is_raster(asset: pystac.Asset) -> bool:
    media_type = (asset.media_type or "").lower()
    suffix = Path(urlparse(asset.href).path).suffix.lower()
    return (
        media_type.startswith("image/")
        or "geotiff" in media_type
        or "jp2" in media_type
        or suffix in _RASTER_SUFFIXES
    )


class DatasetCatalog:
    """DuckDB metadata index stored at the dataset root.

    SQLite remains the transactional worker queue. DuckDB is the analytical
    catalogue users query to find spatial/time-partitioned local raster pieces.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self._write(self._create_schema)

    @contextmanager
    def _file_lock(self, *, exclusive: bool) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as stream:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(stream.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _write(self, operation: Callable[[duckdb.DuckDBPyConnection], None]) -> None:
        with self._file_lock(exclusive=True):
            connection = duckdb.connect(str(self.path))
            try:
                connection.execute("BEGIN TRANSACTION")
                operation(connection)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    @staticmethod
    def _create_schema(connection: duckdb.DuckDBPyConnection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_info (
                key VARCHAR PRIMARY KEY,
                value VARCHAR NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
            );

            CREATE TABLE IF NOT EXISTS items (
                item_id VARCHAR PRIMARY KEY,
                collection_id VARCHAR,
                acquisition_time TIMESTAMPTZ NOT NULL,
                tile_id VARCHAR,
                status VARCHAR NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error VARCHAR,
                west DOUBLE,
                south DOUBLE,
                east DOUBLE,
                north DOUBLE,
                geometry_json VARCHAR,
                cloud_cover DOUBLE,
                metadata_path VARCHAR NOT NULL,
                status_path VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
            );

            CREATE TABLE IF NOT EXISTS assets (
                item_id VARCHAR NOT NULL,
                asset_key VARCHAR NOT NULL,
                href VARCHAR NOT NULL,
                local_path VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error VARCHAR,
                media_type VARCHAR,
                title VARCHAR,
                roles_json VARCHAR,
                is_raster BOOLEAN NOT NULL DEFAULT false,
                resolution_m DOUBLE,
                proj_epsg INTEGER,
                proj_shape_json VARCHAR,
                proj_transform_json VARCHAR,
                proj_bbox_json VARCHAR,
                bands_json VARCHAR,
                nodata VARCHAR,
                data_type VARCHAR,
                raster_scale DOUBLE,
                raster_offset DOUBLE,
                expected_byte_count BIGINT,
                source_checksum VARCHAR,
                byte_count BIGINT,
                sha256 VARCHAR,
                raster_driver VARCHAR,
                raster_dtype VARCHAR,
                raster_width INTEGER,
                raster_height INTEGER,
                raster_west DOUBLE,
                raster_south DOUBLE,
                raster_east DOUBLE,
                raster_north DOUBLE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
                PRIMARY KEY (item_id, asset_key)
            );

            CREATE TABLE IF NOT EXISTS runs (
                state_db VARCHAR NOT NULL,
                run_id BIGINT NOT NULL,
                mode VARCHAR NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ,
                status VARCHAR NOT NULL,
                discovered INTEGER NOT NULL,
                queued INTEGER NOT NULL,
                completed INTEGER NOT NULL,
                failed INTEGER NOT NULL,
                error VARCHAR,
                PRIMARY KEY (state_db, run_id)
            );

            """
        )
        for column_definition in (
            "proj_bbox_json VARCHAR",
            "bands_json VARCHAR",
            "nodata VARCHAR",
            "data_type VARCHAR",
            "raster_scale DOUBLE",
            "raster_offset DOUBLE",
            "expected_byte_count BIGINT",
            "source_checksum VARCHAR",
        ):
            connection.execute(
                f"ALTER TABLE assets ADD COLUMN IF NOT EXISTS {column_definition}"
            )
        connection.execute(
            """
            CREATE OR REPLACE VIEW raster_pieces AS
            SELECT
                i.item_id,
                i.collection_id,
                i.acquisition_time,
                i.tile_id,
                i.west AS item_west,
                i.south AS item_south,
                i.east AS item_east,
                i.north AS item_north,
                a.asset_key,
                a.local_path,
                a.status,
                a.resolution_m,
                a.proj_epsg,
                a.expected_byte_count,
                a.source_checksum,
                a.byte_count,
                a.sha256,
                a.raster_driver,
                a.raster_dtype,
                a.raster_width,
                a.raster_height,
                a.raster_west,
                a.raster_south,
                a.raster_east,
                a.raster_north
            FROM items i
            JOIN assets a USING (item_id)
            WHERE a.is_raster
            """
        )
        connection.execute(
            """
            INSERT INTO dataset_info(key, value)
            VALUES ('schema_version', '2')
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = now()
            """
        )

    def set_dataset_info(self, key: str, value: str) -> None:
        def operation(connection: duckdb.DuckDBPyConnection) -> None:
            connection.execute(
                """
                INSERT INTO dataset_info(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = now()
                """,
                [key, value],
            )

        self._write(operation)

    def upsert_item(
        self,
        item: pystac.Item,
        *,
        metadata_path: str | Path,
        status_path: str | Path,
    ) -> None:
        bbox = item.bbox or [None, None, None, None]
        geometry_json = (
            None if item.geometry is None else json.dumps(item.geometry, sort_keys=True)
        )
        cloud_cover = item.properties.get("eo:cloud_cover")

        def operation(connection: duckdb.DuckDBPyConnection) -> None:
            connection.execute(
                """
                INSERT INTO items(
                    item_id, collection_id, acquisition_time, tile_id, status,
                    west, south, east, north, geometry_json, cloud_cover,
                    metadata_path, status_path
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    collection_id = excluded.collection_id,
                    acquisition_time = excluded.acquisition_time,
                    tile_id = excluded.tile_id,
                    west = excluded.west,
                    south = excluded.south,
                    east = excluded.east,
                    north = excluded.north,
                    geometry_json = excluded.geometry_json,
                    cloud_cover = excluded.cloud_cover,
                    metadata_path = excluded.metadata_path,
                    status_path = excluded.status_path,
                    updated_at = now()
                """,
                [
                    item.id,
                    item.collection_id,
                    CatalogClient.item_datetime(item),
                    tile_id_from_item(item),
                    *bbox,
                    geometry_json,
                    cloud_cover,
                    _absolute(metadata_path),
                    _absolute(status_path),
                ],
            )

        self._write(operation)

    def upsert_asset(
        self,
        *,
        item_id: str,
        asset_key: str,
        asset: pystac.Asset,
        local_path: str | Path,
    ) -> None:
        extra = asset.extra_fields

        def operation(connection: duckdb.DuckDBPyConnection) -> None:
            connection.execute(
                """
                INSERT INTO assets(
                    item_id, asset_key, href, local_path, status, media_type,
                    title, roles_json, is_raster, resolution_m, proj_epsg,
                    proj_shape_json, proj_transform_json, proj_bbox_json,
                    bands_json, nodata, data_type, raster_scale, raster_offset,
                    expected_byte_count, source_checksum
                ) VALUES (
                    ?, ?, ?, ?, 'queued',
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(item_id, asset_key) DO UPDATE SET
                    href = excluded.href,
                    local_path = excluded.local_path,
                    media_type = excluded.media_type,
                    title = excluded.title,
                    roles_json = excluded.roles_json,
                    is_raster = excluded.is_raster,
                    resolution_m = excluded.resolution_m,
                    proj_epsg = excluded.proj_epsg,
                    proj_shape_json = excluded.proj_shape_json,
                    proj_transform_json = excluded.proj_transform_json,
                    proj_bbox_json = excluded.proj_bbox_json,
                    bands_json = excluded.bands_json,
                    nodata = excluded.nodata,
                    data_type = excluded.data_type,
                    raster_scale = excluded.raster_scale,
                    raster_offset = excluded.raster_offset,
                    expected_byte_count = excluded.expected_byte_count,
                    source_checksum = excluded.source_checksum,
                    status = CASE
                        WHEN assets.href != excluded.href
                          OR assets.local_path != excluded.local_path
                        THEN 'queued'
                        ELSE assets.status
                    END,
                    updated_at = now()
                """,
                [
                    item_id,
                    asset_key,
                    asset.href,
                    _absolute(local_path),
                    asset.media_type,
                    asset.title,
                    json.dumps(asset.roles or []),
                    _is_raster(asset),
                    _asset_resolution(asset_key, asset),
                    _asset_epsg(asset),
                    json.dumps(extra.get("proj:shape")),
                    json.dumps(extra.get("proj:transform")),
                    json.dumps(extra.get("proj:bbox")),
                    json.dumps(extra.get("bands") or extra.get("eo:bands")),
                    None
                    if extra.get("nodata") is None
                    else str(extra.get("nodata")),
                    None
                    if extra.get("data_type") is None
                    else str(extra.get("data_type")),
                    extra.get("raster:scale"),
                    extra.get("raster:offset"),
                    extra.get("file:size"),
                    extra.get("file:checksum"),
                ],
            )

        self._write(operation)

    def retain_assets(self, item_id: str, asset_keys: Iterable[str]) -> None:
        selected = tuple(dict.fromkeys(asset_keys))

        def operation(connection: duckdb.DuckDBPyConnection) -> None:
            if selected:
                placeholders = ", ".join("?" for _key in selected)
                connection.execute(
                    f"""
                    UPDATE assets
                       SET status = 'unselected', updated_at = now()
                     WHERE item_id = ? AND asset_key NOT IN ({placeholders})
                    """,
                    [item_id, *selected],
                )
            else:
                connection.execute(
                    """
                    UPDATE assets
                       SET status = 'unselected', updated_at = now()
                     WHERE item_id = ?
                    """,
                    [item_id],
                )

        self._write(operation)

    def sync_job_snapshot(self, snapshot: dict[str, Any]) -> None:
        def operation(connection: duckdb.DuckDBPyConnection) -> None:
            connection.execute(
                """
                UPDATE items
                   SET status = ?, attempts = ?, last_error = ?,
                       updated_at = now()
                 WHERE item_id = ?
                """,
                [
                    snapshot["status"],
                    snapshot["attempts"],
                    snapshot["last_error"],
                    snapshot["item_id"],
                ],
            )
            for asset in snapshot["assets"]:
                connection.execute(
                    """
                    UPDATE assets
                       SET status = ?, attempts = ?, last_error = ?,
                           byte_count = ?, sha256 = ?,
                           updated_at = now()
                     WHERE item_id = ? AND asset_key = ?
                    """,
                    [
                        asset["status"],
                        asset["attempts"],
                        asset["last_error"],
                        asset["bytes"],
                        asset["sha256"],
                        asset["item_id"],
                        asset["asset_key"],
                    ],
                )

        self._write(operation)

    def enrich_raster(self, *, item_id: str, asset_key: str, path: str | Path) -> None:
        """Add file-derived georeferencing when rasterio is available."""

        try:
            import rasterio
        except ImportError:
            logger.debug("rasterio unavailable; keeping STAC projection metadata for %s", path)
            return
        try:
            with rasterio.open(path) as dataset:
                epsg = dataset.crs.to_epsg() if dataset.crs is not None else None
                bounds = dataset.bounds
                values = [
                    dataset.driver,
                    dataset.dtypes[0] if dataset.dtypes else None,
                    dataset.width,
                    dataset.height,
                    epsg,
                    bounds.left,
                    bounds.bottom,
                    bounds.right,
                    bounds.top,
                    item_id,
                    asset_key,
                ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read raster georeferencing for %s: %s", path, exc)
            return

        def operation(connection: duckdb.DuckDBPyConnection) -> None:
            connection.execute(
                """
                UPDATE assets
                   SET raster_driver = ?, raster_dtype = ?, raster_width = ?,
                       raster_height = ?, proj_epsg = COALESCE(?, proj_epsg),
                       raster_west = ?, raster_south = ?, raster_east = ?,
                       raster_north = ?, updated_at = now()
                 WHERE item_id = ? AND asset_key = ?
                """,
                values,
            )

        self._write(operation)

    def upsert_run(
        self,
        run: dict[str, Any],
        *,
        mode: str,
        state_db: str | Path,
    ) -> None:
        def operation(connection: duckdb.DuckDBPyConnection) -> None:
            connection.execute(
                """
                INSERT INTO runs(
                    state_db, run_id, mode, started_at, finished_at, status,
                    discovered, queued, completed, failed, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state_db, run_id) DO UPDATE SET
                    finished_at = excluded.finished_at,
                    status = excluded.status,
                    discovered = excluded.discovered,
                    queued = excluded.queued,
                    completed = excluded.completed,
                    failed = excluded.failed,
                    error = excluded.error
                """,
                [
                    _absolute(state_db),
                    run["run_id"],
                    mode,
                    run["started_at"],
                    run["finished_at"],
                    run["status"],
                    run["discovered"],
                    run["queued"],
                    run["completed"],
                    run["failed"],
                    run["error"],
                ],
            )

        self._write(operation)

    def query_items(
        self,
        *,
        bbox: tuple[float, float, float, float] | None = None,
        start_datetime: datetime | None = None,
        end_datetime: datetime | None = None,
        include_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        """Return saved scene rows, including metadata paths, without requiring assets."""

        clauses: list[str] = []
        parameters: list[Any] = []
        if not include_incomplete:
            clauses.append("status = 'completed'")
        if bbox is not None:
            west, south, east, north = bbox
            clauses.extend(
                [
                    "east >= ?",
                    "west <= ?",
                    "north >= ?",
                    "south <= ?",
                ]
            )
            parameters.extend([west, east, south, north])
        if start_datetime is not None:
            clauses.append("acquisition_time >= ?")
            parameters.append(start_datetime)
        if end_datetime is not None:
            clauses.append("acquisition_time <= ?")
            parameters.append(end_datetime)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT item_id, collection_id, acquisition_time, tile_id, status,
                   attempts, last_error, west, south, east, north,
                   geometry_json, cloud_cover, metadata_path, status_path
              FROM items
              {where}
             ORDER BY acquisition_time, tile_id, item_id
        """
        with self._file_lock(exclusive=False):
            connection = duckdb.connect(str(self.path), read_only=True)
            try:
                cursor = connection.execute(sql, parameters)
                columns = [column[0] for column in cursor.description]
                return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
            finally:
                connection.close()

    def dataset_info(self, key: str | None = None) -> dict[str, str] | str | None:
        """Read one or all dataset metadata values."""

        with self._file_lock(exclusive=False):
            connection = duckdb.connect(str(self.path), read_only=True)
            try:
                if key is not None:
                    row = connection.execute(
                        "SELECT value FROM dataset_info WHERE key = ?", [key]
                    ).fetchone()
                    return None if row is None else str(row[0])
                rows = connection.execute(
                    "SELECT key, value FROM dataset_info ORDER BY key"
                ).fetchall()
                return {str(info_key): str(value) for info_key, value in rows}
            finally:
                connection.close()

    def query_raster_pieces(
        self,
        *,
        bbox: tuple[float, float, float, float] | None = None,
        start_datetime: datetime | None = None,
        end_datetime: datetime | None = None,
        asset_keys: Iterable[str] | None = None,
        include_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["a.is_raster"]
        parameters: list[Any] = []
        if not include_incomplete:
            clauses.append("a.status = 'completed'")
        if bbox is not None:
            west, south, east, north = bbox
            clauses.extend(
                [
                    "i.east >= ?",
                    "i.west <= ?",
                    "i.north >= ?",
                    "i.south <= ?",
                ]
            )
            parameters.extend([west, east, south, north])
        if start_datetime is not None:
            clauses.append("i.acquisition_time >= ?")
            parameters.append(start_datetime)
        if end_datetime is not None:
            clauses.append("i.acquisition_time <= ?")
            parameters.append(end_datetime)
        keys = tuple(dict.fromkeys(asset_keys or ()))
        if keys:
            placeholders = ", ".join("?" for _key in keys)
            clauses.append(f"a.asset_key IN ({placeholders})")
            parameters.extend(keys)

        sql = f"""
            SELECT
                i.item_id, i.collection_id, i.acquisition_time, i.tile_id,
                i.west, i.south, i.east, i.north, i.geometry_json,
                a.asset_key, a.local_path, a.status, a.resolution_m,
                a.proj_epsg, a.proj_shape_json, a.proj_transform_json,
                a.proj_bbox_json, a.bands_json, a.nodata, a.data_type,
                a.raster_scale, a.raster_offset, a.expected_byte_count,
                a.source_checksum, a.byte_count, a.sha256, a.raster_driver,
                a.raster_dtype, a.raster_width, a.raster_height,
                a.raster_west, a.raster_south, a.raster_east, a.raster_north
            FROM items i
            JOIN assets a USING (item_id)
            WHERE {" AND ".join(clauses)}
            ORDER BY i.acquisition_time, i.tile_id, a.asset_key
        """
        with self._file_lock(exclusive=False):
            connection = duckdb.connect(str(self.path), read_only=True)
            try:
                cursor = connection.execute(sql, parameters)
                columns = [column[0] for column in cursor.description]
                return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
            finally:
                connection.close()

    def summary(self) -> dict[str, Any]:
        with self._file_lock(exclusive=False):
            connection = duckdb.connect(str(self.path), read_only=True)
            try:
                item_count = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                asset_count = connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
                completed = connection.execute(
                    "SELECT COUNT(*) FROM assets WHERE status = 'completed'"
                ).fetchone()[0]
                total_bytes = connection.execute(
                    """
                    SELECT COALESCE(SUM(byte_count), 0)
                      FROM assets
                     WHERE status = 'completed'
                    """
                ).fetchone()[0]
            finally:
                connection.close()
        return {
            "path": _absolute(self.path),
            "items": int(item_count),
            "assets": int(asset_count),
            "completed_assets": int(completed),
            "completed_bytes": int(total_bytes),
        }
