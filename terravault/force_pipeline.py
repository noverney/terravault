"""High-level Copernicus discovery, download and native FORCE processing.

FORCE's Level-1 archiving tools traditionally combine four concerns: scene
selection, a clean Level-1 pool, a FORCE file queue, and Level-2 processing.
TerraVault uses the Copernicus Data Space STAC/S3 services for the first two
concerns.  This module joins those existing pieces into one Python API and
keeps a queryable DuckDB catalogue of both inputs and generated images.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import duckdb
import pystac

from .auth import CDSEDownloadAuthConfig
from .catalog import COPERNICUS_STAC_URL, CatalogClient
from .force import FORCE_DOCKER_IMAGE, FORCE_DOCKER_PLATFORM
from .force_level2 import ForceLevel2Config, ForceLevel2Processor, ForceLevel2Result
from .l1c_download import L1CDownloadConfig, L1CDownloadResult, L1CProductDownloader
from .s3_downloader import S3Config

logger = logging.getLogger(__name__)

_PRODUCT_NAME = re.compile(
    r"^(?P<platform>S2[ABC])_MSIL1C_"
    r"(?P<sensing>\d{8}T\d{6})_"
    r"N(?P<baseline>\d{4})_"
    r"(?P<orbit>R\d{3})_"
    r"(?P<tile>T\d{2}[A-Z]{3})_"
    r"(?P<production>\d{8}T\d{6})\.SAFE$"
)
_SAFE_ITEM_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_QUEUE_STATUSES = frozenset({"QUEUED", "DONE", "FAIL"})


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime | None = None) -> str:
    return _utc(value or datetime.now(timezone.utc)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _item_product_name(item: pystac.Item) -> str:
    private = item.properties.get("_private")
    name = private.get("product_name") if isinstance(private, dict) else None
    if not name:
        product = item.assets.get("Product")
        if product is not None:
            local_path = product.extra_fields.get("file:local_path")
            if local_path:
                name = Path(str(local_path)).name
    name = str(name or "").strip()
    if name.endswith(".SAFE.zip"):
        name = name[:-4]
    if not name and "_MSIL1C_" in item.id:
        name = f"{item.id.removesuffix('.SAFE')}.SAFE"
    if _PRODUCT_NAME.fullmatch(name) is None:
        raise ValueError(
            f"STAC item {item.id!r} has no supported S2*_MSIL1C_*.SAFE product name"
        )
    return name


def _product_parts(product_name: str) -> re.Match[str]:
    match = _PRODUCT_NAME.fullmatch(product_name)
    if match is None:  # Protected by _item_product_name; useful for direct callers.
        raise ValueError(f"Unsupported Sentinel-2 L1C product name: {product_name}")
    return match


@dataclass(frozen=True)
class ForceDownloadOptions:
    """Transfer tuning for complete Copernicus L1C SAFE products."""

    chunk_size: int = 8 * 1024 * 1024
    max_retries: int = 5
    retry_base_seconds: float = 2.0
    quota_wait_seconds: float = 900.0

    def __post_init__(self) -> None:
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self.max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if self.retry_base_seconds < 0 or self.quota_wait_seconds < 0:
            raise ValueError("download retry delays cannot be negative")

    def config(
        self,
        *,
        item_path: Path,
        output_root: Path,
        s3: S3Config | None,
        auth: CDSEDownloadAuthConfig | None,
    ) -> L1CDownloadConfig:
        return L1CDownloadConfig(
            item_path=item_path,
            output_root=output_root,
            s3=s3,
            auth=auth,
            chunk_size=self.chunk_size,
            max_retries=self.max_retries,
            retry_base_seconds=self.retry_base_seconds,
            quota_wait_seconds=self.quota_wait_seconds,
        )


@dataclass(frozen=True)
class ForceLevel2Options:
    """The practical FORCE L2PS options, independent of any one input scene."""

    runtime: str = "auto"
    docker_image: str = FORCE_DOCKER_IMAGE
    docker_platform: str | None = FORCE_DOCKER_PLATFORM
    mount_root: Path | None = None
    target_crs: str = "EPSG:2056"
    origin_lon: float = 5.5
    origin_lat: float = 48.0
    tile_size: int = 30_000
    resolution: float = 10.0
    aoi_path: Path | None = None
    dem_path: Path | None = None
    dem_nodata: int = -32767
    cloud_buffer: float = 300.0
    cirrus_buffer: float = 0.0
    shadow_buffer: float = 90.0
    snow_buffer: float = 30.0
    cloud_threshold: float = 0.225
    shadow_threshold: float = 0.02
    max_cloud_cover_frame: int = 100
    max_cloud_cover_tile: int = 100
    resolution_merge: str = "IMPROPHE"
    processes: int = 1
    threads: int = 2
    parallel_reads: bool = False
    output_overview: bool = True
    overwrite: bool = False
    retry_failed: bool = False
    dry_run: bool = False
    progress_interval_seconds: float = 30.0

    def config(self, *, input_path: Path, output_root: Path) -> ForceLevel2Config:
        """Create the validated per-scene configuration used by L2PS."""

        return ForceLevel2Config(
            input_path=input_path,
            output_root=output_root,
            runtime=self.runtime,
            docker_image=self.docker_image,
            docker_platform=self.docker_platform,
            mount_root=self.mount_root,
            target_crs=self.target_crs,
            origin_lon=self.origin_lon,
            origin_lat=self.origin_lat,
            tile_size=self.tile_size,
            resolution=self.resolution,
            aoi_path=self.aoi_path,
            dem_path=self.dem_path,
            dem_nodata=self.dem_nodata,
            cloud_buffer=self.cloud_buffer,
            cirrus_buffer=self.cirrus_buffer,
            shadow_buffer=self.shadow_buffer,
            snow_buffer=self.snow_buffer,
            cloud_threshold=self.cloud_threshold,
            shadow_threshold=self.shadow_threshold,
            max_cloud_cover_frame=self.max_cloud_cover_frame,
            max_cloud_cover_tile=self.max_cloud_cover_tile,
            resolution_merge=self.resolution_merge,
            nproc=self.processes,
            nthread=self.threads,
            parallel_reads=self.parallel_reads,
            output_overview=self.output_overview,
            overwrite=self.overwrite,
            retry_failed=self.retry_failed,
            dry_run=self.dry_run,
            progress_interval_seconds=self.progress_interval_seconds,
        )


@dataclass(frozen=True)
class ForcePipelineConfig:
    """Discovery and processing policy for a Copernicus-backed FORCE run.

    Exactly one of ``bbox`` and ``intersects`` is required.  The catalogue
    cloud threshold is applied before any full SAFE is downloaded.  FORCE's
    own frame/tile cloud thresholds remain separately configurable through
    :class:`ForceLevel2Options`.
    """

    output_root: Path
    start_datetime: datetime
    end_datetime: datetime
    bbox: tuple[float, float, float, float] | None = None
    intersects: dict[str, Any] | None = None
    catalog_url: str = COPERNICUS_STAC_URL
    collection: str = "sentinel-2-l1c"
    max_cloud_cover: float | None = 20.0
    sensors: tuple[str, ...] = ("S2A", "S2B", "S2C")
    max_scenes: int | None = None
    database_path: Path | None = None
    queue_path: Path | None = None
    s3: S3Config | None = None
    auth: CDSEDownloadAuthConfig | None = None
    download: ForceDownloadOptions = field(default_factory=ForceDownloadOptions)
    force: ForceLevel2Options = field(default_factory=ForceLevel2Options)

    def __post_init__(self) -> None:
        output_root = _absolute(self.output_root)
        object.__setattr__(self, "output_root", output_root)
        object.__setattr__(self, "start_datetime", _utc(self.start_datetime))
        object.__setattr__(self, "end_datetime", _utc(self.end_datetime))
        if self.start_datetime >= self.end_datetime:
            raise ValueError("start_datetime must be before end_datetime")
        if (self.bbox is None) == (self.intersects is None):
            raise ValueError("Specify exactly one of bbox or intersects")
        if self.bbox is not None:
            west, south, east, north = self.bbox
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError("bbox must contain valid WGS84 west, south, east, north")
        if self.max_cloud_cover is not None and not 0 <= self.max_cloud_cover <= 100:
            raise ValueError("max_cloud_cover must be between 0 and 100")
        sensors = tuple(dict.fromkeys(sensor.upper() for sensor in self.sensors))
        invalid_sensors = sorted(set(sensors) - {"S2A", "S2B", "S2C"})
        if invalid_sensors or not sensors:
            raise ValueError("sensors must contain one or more of S2A, S2B and S2C")
        object.__setattr__(self, "sensors", sensors)
        if self.max_scenes is not None and self.max_scenes < 1:
            raise ValueError("max_scenes must be at least 1")
        if "l1c" not in self.collection.casefold():
            raise ValueError("FORCE discovery requires a Sentinel-2 L1C collection")
        if self.database_path is not None:
            object.__setattr__(self, "database_path", _absolute(self.database_path))
        if self.queue_path is not None:
            object.__setattr__(self, "queue_path", _absolute(self.queue_path))

    @property
    def effective_database_path(self) -> Path:
        return self.database_path or self.output_root / "force_images.duckdb"

    @property
    def effective_queue_path(self) -> Path:
        return self.queue_path or self.output_root / "level1" / "queue.txt"


@dataclass(frozen=True)
class ForcePipelineResult:
    """Summary and durable outputs from one high-level FORCE pipeline run."""

    run_id: int
    status: str
    discovered: int
    selected: int
    downloaded: int
    downloads_skipped: int
    processed: int
    processing_skipped: int
    database_path: Path
    queue_path: Path
    selected_item_ids: tuple[str, ...]
    image_paths: tuple[Path, ...]
    errors: tuple[str, ...]


class ForcePipelineDatabase:
    """DuckDB catalogue for selected scenes, SAFE inputs and FORCE images."""

    def __init__(self, path: str | Path) -> None:
        self.path = _absolute(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(self.path))
        self._create_schema()

    @contextmanager
    def _transaction(self):
        self.connection.execute("BEGIN TRANSACTION")
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _create_schema(self) -> None:
        with self._transaction():
            self.connection.execute(
                """
                CREATE SEQUENCE IF NOT EXISTS force_pipeline_run_ids START 1;

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id BIGINT PRIMARY KEY DEFAULT nextval('force_pipeline_run_ids'),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    selection_json TEXT NOT NULL,
                    discovered INTEGER NOT NULL DEFAULT 0,
                    selected INTEGER NOT NULL DEFAULT 0,
                    downloaded INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS scenes (
                    item_id TEXT PRIMARY KEY,
                    product_name TEXT NOT NULL,
                    acquisition_time TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    mgrs_tile TEXT NOT NULL,
                    cloud_cover REAL,
                    geometry_json TEXT,
                    metadata_path TEXT NOT NULL,
                    product_path TEXT,
                    download_status TEXT NOT NULL DEFAULT 'selected',
                    force_status TEXT NOT NULL DEFAULT 'not_started',
                    source_mode TEXT,
                    source_byte_count INTEGER,
                    last_error TEXT,
                    selected_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS images (
                    item_id TEXT NOT NULL,
                    image_type TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    byte_count INTEGER,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (item_id, image_type, local_path)
                );

                CREATE INDEX IF NOT EXISTS scenes_time_idx
                    ON scenes(acquisition_time, mgrs_tile);
                CREATE INDEX IF NOT EXISTS scenes_status_idx
                    ON scenes(download_status, force_status);
                CREATE INDEX IF NOT EXISTS images_type_idx
                    ON images(image_type, local_path);
                """
            )
            self.connection.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES ('schema_version', '1', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [_utc_text()],
            )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> ForcePipelineDatabase:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def start_run(self, selection: dict[str, Any]) -> int:
        with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO runs(started_at, status, selection_json)
                VALUES (?, 'running', ?)
                RETURNING run_id
                """,
                [_utc_text(), json.dumps(selection, sort_keys=True)],
            )
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        discovered: int,
        selected: int,
        downloaded: int,
        processed: int,
        error: str | None,
    ) -> None:
        with self._transaction():
            self.connection.execute(
                """
                UPDATE runs
                   SET finished_at = ?, status = ?, discovered = ?, selected = ?,
                       downloaded = ?, processed = ?, error = ?
                 WHERE run_id = ?
                """,
                [
                    _utc_text(),
                    status,
                    discovered,
                    selected,
                    downloaded,
                    processed,
                    error,
                    run_id,
                ],
            )

    def upsert_scene(
        self,
        item: pystac.Item,
        *,
        product_name: str,
        metadata_path: Path,
    ) -> None:
        parts = _product_parts(product_name)
        now = _utc_text()
        geometry_json = (
            None if item.geometry is None else json.dumps(item.geometry, sort_keys=True)
        )
        cloud_cover = item.properties.get("eo:cloud_cover")
        with self._transaction():
            self.connection.execute(
                """
                INSERT INTO scenes(
                    item_id, product_name, acquisition_time, platform, mgrs_tile,
                    cloud_cover, geometry_json, metadata_path, selected_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    product_name = excluded.product_name,
                    acquisition_time = excluded.acquisition_time,
                    platform = excluded.platform,
                    mgrs_tile = excluded.mgrs_tile,
                    cloud_cover = excluded.cloud_cover,
                    geometry_json = excluded.geometry_json,
                    metadata_path = excluded.metadata_path,
                    selected_at = excluded.selected_at,
                    updated_at = excluded.updated_at
                """,
                [
                    item.id,
                    product_name,
                    _utc_text(CatalogClient.item_datetime(item)),
                    parts.group("platform"),
                    parts.group("tile"),
                    cloud_cover,
                    geometry_json,
                    str(metadata_path),
                    now,
                    now,
                ],
            )

    def record_download(self, item_id: str, result: L1CDownloadResult) -> None:
        now = _utc_text()
        with self._transaction():
            self.connection.execute(
                """
                UPDATE scenes
                   SET product_path = ?, download_status = ?, source_mode = ?,
                       source_byte_count = ?, last_error = NULL, updated_at = ?
                 WHERE item_id = ?
                """,
                [
                    str(result.product_path),
                    result.status,
                    result.source_mode,
                    result.byte_count,
                    now,
                    item_id,
                ],
            )
            self._upsert_image_locked(
                item_id=item_id,
                image_type="L1C_SAFE",
                path=result.product_path,
                byte_count=result.byte_count,
                created_at=now,
            )

    def record_force(self, item_id: str, result: ForceLevel2Result) -> tuple[Path, ...]:
        now = _utc_text()
        typed_paths: list[tuple[str, Path]] = []
        typed_paths.extend(("BOA", path) for path in result.boa_paths)
        typed_paths.extend(("QAI", path) for path in result.qai_paths)
        typed_paths.extend(("OVERVIEW", path) for path in result.overview_paths)
        if result.boa_mosaic_path is not None:
            typed_paths.append(("BOA_MOSAIC", result.boa_mosaic_path))
        if result.qai_mosaic_path is not None:
            typed_paths.append(("QAI_MOSAIC", result.qai_mosaic_path))
        with self._transaction():
            self.connection.execute(
                """
                UPDATE scenes
                   SET force_status = ?, last_error = NULL, updated_at = ?
                 WHERE item_id = ?
                """,
                (result.status, now, item_id),
            )
            self.connection.execute(
                "DELETE FROM images WHERE item_id = ? AND image_type != 'L1C_SAFE'",
                (item_id,),
            )
            for image_type, path in typed_paths:
                self._upsert_image_locked(
                    item_id=item_id,
                    image_type=image_type,
                    path=path,
                    byte_count=path.stat().st_size if path.is_file() else None,
                    created_at=now,
                )
        return tuple(path for _kind, path in typed_paths)

    def record_error(
        self,
        item_id: str,
        *,
        stage: str,
        error: str,
        status: str = "failed",
    ) -> None:
        if stage not in {"download", "force"}:
            raise ValueError("stage must be download or force")
        if status not in {"failed", "interrupted"}:
            raise ValueError("error status must be failed or interrupted")
        status_column = "download_status" if stage == "download" else "force_status"
        with self._transaction():
            self.connection.execute(
                f"""
                UPDATE scenes
                   SET {status_column} = ?, last_error = ?, updated_at = ?
                 WHERE item_id = ?
                """,
                [status, error, _utc_text(), item_id],
            )

    def product_path(self, item_id: str) -> Path | None:
        row = self.connection.execute(
            "SELECT product_path FROM scenes WHERE item_id = ?", [item_id]
        ).fetchone()
        if row is None or not row[0]:
            return None
        return Path(str(row[0]))

    def queue_entries(self) -> tuple[tuple[Path, str], ...]:
        rows = self.connection.execute(
            """
            SELECT product_path, force_status
              FROM scenes
             WHERE download_status = 'complete' AND product_path IS NOT NULL
             ORDER BY acquisition_time, item_id
            """
        ).fetchall()
        entries: list[tuple[Path, str]] = []
        for row in rows:
            force_status = str(row[1])
            if force_status == "complete":
                queue_status = "DONE"
            elif force_status in {"failed", "interrupted"}:
                queue_status = "FAIL"
            else:
                queue_status = "QUEUED"
            entries.append((Path(str(row[0])), queue_status))
        return tuple(entries)

    def scenes(self) -> tuple[dict[str, Any], ...]:
        cursor = self.connection.execute(
            "SELECT * FROM scenes ORDER BY acquisition_time, item_id"
        )
        return self._dict_rows(cursor)

    def images(self, *, image_type: str | None = None) -> tuple[dict[str, Any], ...]:
        if image_type is None:
            cursor = self.connection.execute(
                "SELECT * FROM images ORDER BY item_id, image_type, local_path"
            )
        else:
            cursor = self.connection.execute(
                """
                SELECT * FROM images
                 WHERE image_type = ?
                 ORDER BY item_id, local_path
                """,
                [image_type],
            )
        return self._dict_rows(cursor)

    def set_info(self, key: str, value: Any) -> None:
        """Persist JSON-serializable information alongside the scene catalogue."""

        with self._transaction():
            self.connection.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [key, json.dumps(value, sort_keys=True), _utc_text()],
            )

    def dataset_info(self, key: str | None = None) -> Any:
        """Read one setting, or all settings, decoding JSON when possible."""

        if key is None:
            rows = self.connection.execute(
                "SELECT key, value FROM settings ORDER BY key"
            ).fetchall()
            return {str(row[0]): self._decode_setting(str(row[1])) for row in rows}
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?", [key]
        ).fetchone()
        return None if row is None else self._decode_setting(str(row[0]))

    @staticmethod
    def _decode_setting(value: str) -> Any:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    @staticmethod
    def _dict_rows(cursor: duckdb.DuckDBPyConnection) -> tuple[dict[str, Any], ...]:
        columns = [str(description[0]) for description in cursor.description]
        return tuple(
            dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
        )

    def _upsert_image_locked(
        self,
        *,
        item_id: str,
        image_type: str,
        path: Path,
        byte_count: int | None,
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO images(item_id, image_type, local_path, byte_count, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(item_id, image_type, local_path) DO UPDATE SET
                byte_count = excluded.byte_count,
                created_at = excluded.created_at
            """,
            [item_id, image_type, str(path), byte_count, created_at],
        )


DownloadFactory = Callable[[L1CDownloadConfig], L1CProductDownloader]
ProcessorFactory = Callable[[ForceLevel2Config], ForceLevel2Processor]


class ForcePipeline:
    """Select Copernicus L1C scenes, download them, and run native FORCE L2PS."""

    def __init__(
        self,
        config: ForcePipelineConfig,
        *,
        catalog: CatalogClient | None = None,
        database: ForcePipelineDatabase | None = None,
        downloader_factory: DownloadFactory = L1CProductDownloader,
        processor_factory: ProcessorFactory = ForceLevel2Processor,
    ) -> None:
        self.config = config
        self.catalog = catalog or CatalogClient(
            catalog_url=config.catalog_url,
            collections=[config.collection],
            bbox=None if config.intersects is not None else list(config.bbox or ()),
            intersects=config.intersects,
            max_cloud_cover=config.max_cloud_cover,
        )
        self.database = database or ForcePipelineDatabase(config.effective_database_path)
        self._owns_database = database is None
        self.downloader_factory = downloader_factory
        self.processor_factory = processor_factory

    def close(self) -> None:
        if self._owns_database:
            self.database.close()

    def __enter__(self) -> ForcePipeline:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def discover(self) -> tuple[int, tuple[pystac.Item, ...]]:
        """Return the catalogue count and FORCE-style de-duplicated selection."""

        discovered_items = list(
            self.catalog.search(
                start_datetime=self.config.start_datetime,
                end_datetime=self.config.end_datetime,
            )
        )
        candidates: list[tuple[pystac.Item, str, re.Match[str]]] = []
        for item in discovered_items:
            product_name = _item_product_name(item)
            parts = _product_parts(product_name)
            if parts.group("platform") in self.config.sensors:
                candidates.append((item, product_name, parts))

        # FORCE L1AS keeps one publication of the same acquisition/tile: the
        # highest processing baseline, then the latest production timestamp.
        versions: dict[tuple[str, str, str, str], tuple[pystac.Item, str, re.Match[str]]] = {}
        for candidate in candidates:
            item, product_name, parts = candidate
            key = (
                parts.group("platform"),
                parts.group("sensing"),
                parts.group("orbit"),
                parts.group("tile"),
            )
            existing = versions.get(key)
            if existing is None:
                versions[key] = candidate
                continue
            existing_parts = existing[2]
            rank = (
                int(parts.group("baseline")),
                parts.group("production"),
                item.id,
            )
            existing_rank = (
                int(existing_parts.group("baseline")),
                existing_parts.group("production"),
                existing[0].id,
            )
            if rank > existing_rank:
                versions[key] = candidate

        selected = sorted(
            (candidate[0] for candidate in versions.values()),
            key=lambda item: (CatalogClient.item_datetime(item), item.id),
        )
        if self.config.max_scenes is not None and len(selected) > self.config.max_scenes:
            # A safety cap should retain the most recent observations, while
            # processing those retained observations in chronological order.
            selected = selected[-self.config.max_scenes :]
        logger.info(
            "FORCE discovery complete – catalogue=%d candidates=%d selected=%d",
            len(discovered_items),
            len(candidates),
            len(selected),
        )
        return len(discovered_items), tuple(selected)

    def _selection_payload(self) -> dict[str, Any]:
        return {
            "catalog_url": self.config.catalog_url,
            "collection": self.config.collection,
            "start_datetime": _utc_text(self.config.start_datetime),
            "end_datetime": _utc_text(self.config.end_datetime),
            "bbox": self.config.bbox,
            "intersects": self.config.intersects,
            "max_cloud_cover": self.config.max_cloud_cover,
            "sensors": self.config.sensors,
            "max_scenes": self.config.max_scenes,
        }

    def _metadata_path(self, item_id: str) -> Path:
        if _SAFE_ITEM_ID.fullmatch(item_id) is None:
            raise ValueError(f"STAC item id contains unsafe filename characters: {item_id!r}")
        return (
            self.config.output_root
            / "_terravault"
            / "force-pipeline"
            / "items"
            / f"{item_id}.json"
        )

    def _persist_item(self, item: pystac.Item) -> tuple[str, Path]:
        product_name = _item_product_name(item)
        metadata_path = self._metadata_path(item.id)
        _write_text_atomic(
            metadata_path,
            json.dumps(item.to_dict(include_self_link=True), indent=2, sort_keys=True),
        )
        self.database.upsert_scene(
            item,
            product_name=product_name,
            metadata_path=metadata_path,
        )
        return product_name, metadata_path

    def _write_force_queue(self) -> None:
        lines: list[str] = []
        for product_path, status in self.database.queue_entries():
            if status not in _QUEUE_STATUSES:
                raise RuntimeError(f"Unexpected FORCE queue status: {status}")
            if any(character.isspace() for character in str(product_path)):
                raise ValueError(f"FORCE queue paths cannot contain whitespace: {product_path}")
            lines.append(f"{product_path} {status}")
        text = "\n".join(lines)
        if text:
            text += "\n"
        _write_text_atomic(self.config.effective_queue_path, text)

    def run(self, *, download: bool = True, process: bool = True) -> ForcePipelineResult:
        """Execute discovery and optionally download/process every selected scene.

        ``download=False, process=False`` is the metadata-only mode.  Processing
        without downloading is supported when the catalogue already contains a
        valid local product path from an earlier run.
        """

        run_id = self.database.start_run(self._selection_payload())
        discovered = 0
        selected: tuple[pystac.Item, ...] = ()
        downloaded = 0
        downloads_skipped = 0
        processed = 0
        processing_skipped = 0
        errors: list[str] = []
        image_paths: list[Path] = []

        def persist_interruption(
            exc: BaseException,
            *,
            item_id: str | None = None,
            stage: str | None = None,
        ) -> None:
            message = f"{type(exc).__name__}: {exc}"
            if item_id is not None and stage is not None:
                self.database.record_error(
                    item_id,
                    stage=stage,
                    error=message,
                    status="interrupted",
                )
            try:
                self._write_force_queue()
            except Exception:  # noqa: BLE001
                logger.exception("Could not refresh the aggregate FORCE queue on interruption")
            self.database.finish_run(
                run_id,
                status="interrupted",
                discovered=discovered,
                selected=len(selected),
                downloaded=downloaded,
                processed=processed,
                error=message,
            )

        try:
            discovered, selected = self.discover()
        except (KeyboardInterrupt, SystemExit) as exc:
            persist_interruption(exc)
            raise
        except Exception as exc:  # noqa: BLE001
            error = f"Catalog discovery failed: {exc}"
            logger.exception(error)
            errors.append(error)
        else:
            for item in selected:
                try:
                    _product_name, metadata_path = self._persist_item(item)
                except (KeyboardInterrupt, SystemExit) as exc:
                    persist_interruption(exc, item_id=item.id, stage="download")
                    raise
                except Exception as exc:  # noqa: BLE001
                    error = f"Could not persist selected item {item.id}: {exc}"
                    logger.exception(error)
                    errors.append(error)
                    continue

                product_path = self.database.product_path(item.id)
                if download:
                    try:
                        download_result = self.downloader_factory(
                            self.config.download.config(
                                item_path=metadata_path,
                                output_root=self.config.output_root,
                                s3=self.config.s3,
                                auth=self.config.auth,
                            )
                        ).run()
                        self.database.record_download(item.id, download_result)
                        product_path = download_result.product_path
                        if download_result.skipped:
                            downloads_skipped += 1
                        else:
                            downloaded += 1
                        image_paths.append(download_result.product_path)
                        self._write_force_queue()
                    except (KeyboardInterrupt, SystemExit) as exc:
                        persist_interruption(exc, item_id=item.id, stage="download")
                        raise
                    except Exception as exc:  # noqa: BLE001
                        error = f"L1C download failed for {item.id}: {exc}"
                        logger.exception(error)
                        self.database.record_error(item.id, stage="download", error=str(exc))
                        errors.append(error)
                        self._write_force_queue()
                        continue

                if not process:
                    continue
                if product_path is None or not product_path.exists():
                    error = (
                        f"FORCE processing skipped for {item.id}: no complete local L1C SAFE"
                    )
                    self.database.record_error(item.id, stage="force", error=error)
                    errors.append(error)
                    continue
                try:
                    force_result = self.processor_factory(
                        self.config.force.config(
                            input_path=product_path,
                            output_root=self.config.output_root,
                        )
                    ).run()
                    image_paths.extend(self.database.record_force(item.id, force_result))
                    if force_result.skipped:
                        processing_skipped += 1
                    elif force_result.status == "complete":
                        processed += 1
                    self._write_force_queue()
                except (KeyboardInterrupt, SystemExit) as exc:
                    persist_interruption(exc, item_id=item.id, stage="force")
                    raise
                except Exception as exc:  # noqa: BLE001
                    error = f"FORCE processing failed for {item.id}: {exc}"
                    logger.exception(error)
                    self.database.record_error(item.id, stage="force", error=str(exc))
                    errors.append(error)
                    self._write_force_queue()

        self._write_force_queue()
        status = "complete" if not errors else ("partial" if selected else "failed")
        self.database.finish_run(
            run_id,
            status=status,
            discovered=discovered,
            selected=len(selected),
            downloaded=downloaded,
            processed=processed,
            error="\n".join(errors) or None,
        )
        return ForcePipelineResult(
            run_id=run_id,
            status=status,
            discovered=discovered,
            selected=len(selected),
            downloaded=downloaded,
            downloads_skipped=downloads_skipped,
            processed=processed,
            processing_skipped=processing_skipped,
            database_path=self.config.effective_database_path,
            queue_path=self.config.effective_queue_path,
            selected_item_ids=tuple(item.id for item in selected),
            image_paths=tuple(dict.fromkeys(image_paths)),
            errors=tuple(errors),
        )


def run_force_pipeline(
    *,
    output_root: str | Path,
    start_datetime: datetime,
    end_datetime: datetime | None = None,
    bbox: Iterable[float] | None = None,
    intersects: dict[str, Any] | None = None,
    max_cloud_cover: float | None = 20.0,
    sensors: Iterable[str] = ("S2A", "S2B", "S2C"),
    max_scenes: int | None = None,
    database_path: str | Path | None = None,
    queue_path: str | Path | None = None,
    s3: S3Config | None = None,
    auth: CDSEDownloadAuthConfig | None = None,
    dem_path: str | Path | None = None,
    runtime: str = "auto",
    target_crs: str = "EPSG:2056",
    resolution: float = 10.0,
    max_cloud_cover_frame: int = 100,
    max_cloud_cover_tile: int = 100,
    processes: int = 1,
    threads: int = 2,
    download: bool = True,
    process: bool = True,
) -> ForcePipelineResult:
    """Convenience API for the most commonly adjusted discovery/FORCE options.

    Explicit ``s3``/``auth`` values win.  Otherwise the same
    ``TERRAVAULT_CDSE_*`` environment variables used by the CLI are resolved.
    """

    bbox_tuple = None if bbox is None else tuple(float(value) for value in bbox)
    if bbox_tuple is not None and len(bbox_tuple) != 4:
        raise ValueError("bbox must contain west, south, east and north")
    force_options = ForceLevel2Options(
        runtime=runtime,
        target_crs=target_crs,
        resolution=resolution,
        dem_path=None if dem_path is None else Path(dem_path),
        max_cloud_cover_frame=max_cloud_cover_frame,
        max_cloud_cover_tile=max_cloud_cover_tile,
        processes=processes,
        threads=threads,
    )
    resolved_s3 = s3
    if resolved_s3 is None:
        access_key = os.environ.get("TERRAVAULT_CDSE_S3_ACCESS_KEY", "")
        secret_key = os.environ.get("TERRAVAULT_CDSE_S3_SECRET_KEY", "")
        if access_key and secret_key:
            resolved_s3 = S3Config(
                access_key=access_key,
                secret_key=secret_key,
                endpoint_url=os.environ.get(
                    "TERRAVAULT_CDSE_S3_ENDPOINT",
                    "https://eodata.dataspace.copernicus.eu",
                ),
                region_name=os.environ.get("TERRAVAULT_CDSE_S3_REGION", "default"),
            )
    resolved_auth = auth or CDSEDownloadAuthConfig.from_env(os.environ)
    config = ForcePipelineConfig(
        output_root=Path(output_root),
        start_datetime=start_datetime,
        end_datetime=end_datetime or datetime.now(timezone.utc),
        bbox=bbox_tuple,  # type: ignore[arg-type]
        intersects=intersects,
        max_cloud_cover=max_cloud_cover,
        sensors=tuple(sensors),
        max_scenes=max_scenes,
        database_path=None if database_path is None else Path(database_path),
        queue_path=None if queue_path is None else Path(queue_path),
        s3=resolved_s3,
        auth=resolved_auth,
        force=force_options,
    )
    with ForcePipeline(config) as pipeline:
        return pipeline.run(download=download, process=process)
