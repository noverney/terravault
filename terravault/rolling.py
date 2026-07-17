"""Restartable rolling Sentinel-2 ingestion for an explicit region of interest."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import pystac

from .catalog import COPERNICUS_STAC_URL, CatalogClient
from .dataset_catalog import DatasetCatalog
from .rolling_state import RollingState, as_utc_text, utc_now
from .s3_downloader import (
    DownloadInterrupted,
    QuotaExceededError,
    S3Config,
    S3Downloader,
)
from .spatial import geometry_area
from .storage import StorageManager, tile_id_from_item

logger = logging.getLogger(__name__)

# One canonical, highest/native-resolution representation of the documented
# Sentinel-2 L2A analysis layers, plus the SAFE provenance metadata.
NATIVE_L2A_ASSET_KEYS: tuple[str, ...] = (
    "B01_60m",
    "B02_10m",
    "B03_10m",
    "B04_10m",
    "B05_20m",
    "B06_20m",
    "B07_20m",
    "B08_10m",
    "B8A_20m",
    "B09_60m",
    "B11_20m",
    "B12_20m",
    "AOT_10m",
    "WVP_10m",
    "SCL_20m",
    "SNW_20m",
    "CLD_20m",
    "safe_manifest",
    "product_metadata",
    "granule_metadata",
    "datastrip_metadata",
    "inspire_metadata",
)

# Compact analysis-ready profile for standard NDVI.  B04 and B08 are the
# native 10 m red/NIR inputs; SCL and CLD support invalid/cloud masking.
NDVI_L2A_ASSET_KEYS: tuple[str, ...] = (
    "B04_10m",
    "B08_10m",
    "SCL_20m",
    "CLD_20m",
)

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_MIN_BOOTSTRAP_FOOTPRINT_RATIO = 0.9


@dataclass(frozen=True)
class RegionOfInterest:
    """Validated WGS84 region used for discovery and state identity."""

    geometry: dict[str, Any]
    bbox: tuple[float, float, float, float]
    fingerprint: str
    canonical_geojson: str
    source: str


@dataclass
class RollingConfig:
    """Configuration for a rolling watcher."""

    roi: RegionOfInterest
    catalog_url: str = COPERNICUS_STAC_URL
    collection: str = "sentinel-2-l2a"
    max_cloud_cover: float | None = None
    lookback_hours: float = 72.0
    bootstrap_lookback_days: float = 14.0
    poll_seconds: float = 900.0
    max_items_per_cycle: int | None = None
    max_jobs_per_cycle: int = 100
    asset_profile: str = "native"
    asset_keys: list[str] = field(default_factory=list)
    require_all_assets: bool = True
    state_db: Path = Path("terravault_rolling.db")
    storage_root: Path = Path("satellite_data")
    dataset_db: Path | None = None
    lock_file: Path | None = None
    max_attempts: int = 5
    retry_base_seconds: float = 60.0
    retry_max_seconds: float = 86400.0
    quota_retry_seconds: float = 900.0
    retry_failed: bool = False
    s3: S3Config | None = None

    def __post_init__(self) -> None:
        if self.lookback_hours <= 0:
            raise ValueError("lookback_hours must be positive")
        if self.bootstrap_lookback_days <= 0:
            raise ValueError("bootstrap_lookback_days must be positive")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.max_jobs_per_cycle <= 0:
            raise ValueError("max_jobs_per_cycle must be positive")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if (
            self.retry_base_seconds < 0
            or self.retry_max_seconds < 0
            or self.quota_retry_seconds < 0
        ):
            raise ValueError("retry delays cannot be negative")
        if self.asset_profile not in {"native", "ndvi", "metadata-only", "custom"}:
            raise ValueError(
                "asset_profile must be native, ndvi, metadata-only or custom"
            )
        if self.asset_profile == "custom" and not self.asset_keys:
            raise ValueError("custom asset profile requires at least one --asset-key")
        if self.asset_profile != "custom" and self.asset_keys:
            raise ValueError("--asset-keys can only be used with the custom asset profile")

    @property
    def requested_asset_keys(self) -> tuple[str, ...]:
        if self.asset_profile == "metadata-only":
            return ()
        if self.asset_profile == "custom":
            return tuple(dict.fromkeys(self.asset_keys))
        if self.asset_profile == "ndvi":
            return NDVI_L2A_ASSET_KEYS
        return NATIVE_L2A_ASSET_KEYS

    @property
    def effective_lock_file(self) -> Path:
        return self.lock_file or self.state_db.with_suffix(f"{self.state_db.suffix}.lock")

    @property
    def effective_dataset_db(self) -> Path:
        return self.dataset_db or self.storage_root / "dataset.duckdb"


@dataclass(frozen=True)
class RollingRunResult:
    """Counts and errors from one discovery/processing cycle."""

    run_id: int
    discovered: int
    queued: int
    completed: int
    failed: int
    errors: tuple[str, ...]
    stopped: bool = False


def _walk_positions(value: Any) -> Iterable[tuple[float, float]]:
    if (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        yield float(value[0]), float(value[1])
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_positions(child)


def _normalize_geometry(document: dict[str, Any]) -> dict[str, Any]:
    document_type = document.get("type")
    if document_type == "Feature":
        geometry = document.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError("GeoJSON Feature has no geometry")
        return geometry
    if document_type == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list) or not features:
            raise ValueError("GeoJSON FeatureCollection has no features")
        polygons: list[Any] = []
        for feature in features:
            if not isinstance(feature, dict):
                raise ValueError("Invalid feature in GeoJSON FeatureCollection")
            geometry = _normalize_geometry(feature)
            if geometry.get("type") == "Polygon":
                polygons.append(geometry.get("coordinates"))
            elif geometry.get("type") == "MultiPolygon":
                polygons.extend(geometry.get("coordinates", []))
            else:
                raise ValueError("ROI features must be Polygon or MultiPolygon geometries")
        return {"type": "MultiPolygon", "coordinates": polygons}
    return document


def load_roi(
    *,
    bbox: Iterable[float] | None = None,
    geojson_path: str | Path | None = None,
) -> RegionOfInterest:
    """Load and validate exactly one WGS84 bbox or GeoJSON polygon ROI."""

    if (bbox is None) == (geojson_path is None):
        raise ValueError("Specify exactly one ROI using --bbox or --roi")

    source: str
    if bbox is not None:
        values = tuple(float(value) for value in bbox)
        if len(values) != 4:
            raise ValueError("bbox must contain WEST SOUTH EAST NORTH")
        west, south, east, north = values
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bbox must be valid WGS84 WEST SOUTH EAST NORTH coordinates")
        geometry: dict[str, Any] = {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        }
        source = "bbox"
    else:
        path = Path(geojson_path)  # type: ignore[arg-type]
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read ROI GeoJSON {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise ValueError("ROI GeoJSON root must be an object")
        geometry = _normalize_geometry(document)
        source = str(path.resolve())

    if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("ROI must be a GeoJSON Polygon or MultiPolygon")
    coordinates = geometry.get("coordinates")
    positions = list(_walk_positions(coordinates))
    if not positions:
        raise ValueError("ROI geometry has no coordinates")
    if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in positions):
        raise ValueError("ROI coordinates must use WGS84 longitude/latitude")
    west = min(position[0] for position in positions)
    south = min(position[1] for position in positions)
    east = max(position[0] for position in positions)
    north = max(position[1] for position in positions)
    if west == east or south == north:
        raise ValueError("ROI geometry has zero area")

    canonical = json.dumps(geometry, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return RegionOfInterest(
        geometry=geometry,
        bbox=(west, south, east, north),
        fingerprint=fingerprint,
        canonical_geojson=canonical,
        source=source,
    )


class RunLock:
    """Non-blocking filesystem lock preventing overlapping cron/daemon workers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._stream: Any | None = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._stream.close()
            self._stream = None
            raise RuntimeError(
                f"Another rolling worker holds the lock {self.path}"
            ) from exc
        self._stream.seek(0)
        self._stream.truncate()
        self._stream.write(f"pid={os.getpid()} started={as_utc_text()}\n")
        self._stream.flush()
        return self

    def __exit__(self, *_args: object) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


class RollingIngestor:
    """Discover, queue and acquire all requested assets for an explicit ROI."""

    def __init__(
        self,
        config: RollingConfig,
        *,
        catalog: CatalogClient | None = None,
        state: RollingState | None = None,
        downloader: S3Downloader | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config
        self.now = now
        self.stop_event = threading.Event()
        self.catalog = catalog or CatalogClient(
            catalog_url=config.catalog_url,
            collections=[config.collection],
            bbox=None,
            intersects=config.roi.geometry,
            max_cloud_cover=config.max_cloud_cover,
        )
        self.storage = StorageManager(
            config.storage_root,
            mission=config.collection,
            hive_partitions=True,
        )

        if config.requested_asset_keys:
            if downloader is None and config.s3 is None:
                raise ValueError(
                    "An S3 configuration is required unless --asset-profile metadata-only is used"
                )
            self.downloader = downloader or S3Downloader(config.s3)  # type: ignore[arg-type]
        else:
            self.downloader = downloader
        self.state = state or RollingState(config.state_db)
        self._owns_state = state is None
        self.dataset = DatasetCatalog(config.effective_dataset_db)
        self.dataset.set_dataset_info("storage_root", str(config.storage_root.resolve()))
        self.dataset.set_dataset_info("roi_fingerprint", config.roi.fingerprint)
        self.dataset.set_dataset_info("roi_geojson", config.roi.canonical_geojson)
        logger.info(
            "Dataset storage initialized – root=%s state=%s duckdb=%s",
            config.storage_root.resolve(),
            config.state_db.resolve(),
            config.effective_dataset_db.resolve(),
        )
        self._prepared = False

    def _prepare_state(self) -> None:
        """Validate and recover state after the process owns the worker lock."""

        if self._prepared:
            return
        self.state.configure_roi(
            self.config.roi.fingerprint,
            self.config.roi.canonical_geojson,
        )
        policy = json.dumps(
            {
                "requested_assets": self.config.requested_asset_keys,
                "require_all_assets": self.config.require_all_assets,
            },
            sort_keys=True,
        )
        policy_fingerprint = hashlib.sha256(policy.encode("utf-8")).hexdigest()
        previous_policy = self.state.get_setting("asset_policy_fingerprint")
        if previous_policy is not None and previous_policy != policy_fingerprint:
            self.state.delete_setting("bootstrap_completed")
            self.state.delete_setting("last_successful_discovery_at")
            self.state.record_event(
                level="INFO",
                message="Asset policy changed; latest-per-tile bootstrap will run again",
            )
        self.state.set_setting("asset_policy_fingerprint", policy_fingerprint)
        jobs, assets = self.state.recover_interrupted()
        if jobs or assets:
            logger.info(
                "Recovered interrupted state – jobs=%d assets=%d", jobs, assets
            )
        if self.config.retry_failed:
            jobs, assets = self.state.requeue_failed_jobs()
            logger.info(
                "Operator-requested terminal retry – jobs=%d assets=%d", jobs, assets
            )
        self._prepared = True

    def close(self) -> None:
        if self._owns_state:
            self.state.close()

    def request_stop(self, reason: str = "stop requested") -> None:
        if not self.stop_event.is_set():
            logger.info("%s; the current partial download will be retained", reason)
        self.stop_event.set()

    @staticmethod
    def _suffix_for_href(href: str) -> str:
        suffix = Path(urlparse(href).path).suffix
        return suffix if suffix and len(suffix) <= 12 else ".bin"

    def _status_path(self, item: pystac.Item) -> Path:
        return self.storage.scene_dir(item) / "job_status.json"

    @staticmethod
    def _item_recency(item: pystac.Item) -> tuple[datetime, str, str]:
        """Sort acquisition duplicates toward their most recently published form."""

        published = str(
            item.properties.get("updated")
            or item.properties.get("created")
            or item.properties.get("published")
            or ""
        )
        return CatalogClient.item_datetime(item), published, item.id

    def _write_status(self, item_id: str) -> None:
        snapshot = self.state.job_snapshot(item_id)
        path = Path(snapshot["status_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        self.dataset.sync_job_snapshot(snapshot)
        logger.debug(
            "Job state persisted – item=%s status=%s sidecar=%s duckdb=%s",
            item_id,
            snapshot["status"],
            path.resolve(),
            self.config.effective_dataset_db.resolve(),
        )

    def _queue_items(self, run_id: int, items: Iterable[pystac.Item]) -> int:
        """Persist discovered items and their complete requested asset policy."""

        queued = 0
        requested = self.config.requested_asset_keys
        for item in items:
            if self.stop_event.is_set():
                break
            metadata_path = self.storage.save_metadata(item)
            status_path = self._status_path(item)
            missing = [key for key in requested if key not in item.assets]
            changed = self.state.upsert_job(
                item_id=item.id,
                item_datetime=CatalogClient.item_datetime(item),
                collection_id=item.collection_id,
                metadata_path=metadata_path,
                status_path=status_path,
                requested_assets=requested,
                missing_assets=missing,
            )
            self.dataset.upsert_item(
                item,
                metadata_path=metadata_path,
                status_path=status_path,
            )
            present_keys = [key for key in requested if key in item.assets]
            self.state.retain_assets(item.id, present_keys)
            self.dataset.retain_assets(item.id, present_keys)
            asset_changed = False
            for asset_key in requested:
                asset = item.assets.get(asset_key)
                if asset is None:
                    continue
                local_path = self.storage.asset_path(
                    item,
                    _SAFE_FILENAME.sub("_", asset_key).strip("._") or "asset",
                    suffix=self._suffix_for_href(asset.href),
                )
                asset_changed = self.state.upsert_asset(
                    item_id=item.id,
                    asset_key=asset_key,
                    href=asset.href,
                    local_path=local_path,
                ) or asset_changed
                self.dataset.upsert_asset(
                    item_id=item.id,
                    asset_key=asset_key,
                    asset=asset,
                    local_path=local_path,
                )
            local_asset_invalid = False
            for asset_state in self.state.assets_for_job(item.id):
                if asset_state["status"] != "completed":
                    continue
                local_path = Path(asset_state["local_path"])
                expected_bytes = asset_state["bytes"]
                if (
                    expected_bytes is None
                    or not local_path.is_file()
                    or local_path.stat().st_size != expected_bytes
                ):
                    error = "Completed asset is missing locally or has an unexpected size"
                    self.state.requeue_asset(item.id, asset_state["asset_key"], error)
                    self.state.record_event(
                        level="WARNING",
                        run_id=run_id,
                        item_id=item.id,
                        asset_key=asset_state["asset_key"],
                        message=error,
                    )
                    local_asset_invalid = True
            if local_asset_invalid and not changed:
                self.state.requeue_job(item.id, "A completed local asset needs repair")
                changed = True
            if asset_changed and not changed:
                self.state.requeue_job(item.id, "A catalogue asset source changed")
                changed = True
            if changed:
                queued += 1
                self.state.record_event(
                    level="INFO",
                    run_id=run_id,
                    item_id=item.id,
                    message="Item inserted or refreshed in the durable queue",
                    details={"missing_assets": missing},
                )
                logger.info(
                    "Queued item – item=%s acquired=%s tile=%s metadata=%s "
                    "scene_dir=%s assets=%d missing=%d",
                    item.id,
                    as_utc_text(CatalogClient.item_datetime(item)),
                    tile_id_from_item(item),
                    metadata_path.resolve(),
                    status_path.parent.resolve(),
                    len(present_keys),
                    len(missing),
                )
            self._write_status(item.id)
        return queued

    def _discover(self, run_id: int) -> tuple[int, int]:
        end = self.now()
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        else:
            end = end.astimezone(timezone.utc)

        bootstrap = self.state.get_setting("bootstrap_completed") != "true"
        last_successful_raw = self.state.get_setting("last_successful_discovery_at")
        if bootstrap:
            start = end - timedelta(days=self.config.bootstrap_lookback_days)
        elif last_successful_raw:
            last_successful = datetime.fromisoformat(
                last_successful_raw.replace("Z", "+00:00")
            )
            start = last_successful - timedelta(hours=self.config.lookback_hours)
        else:
            start = end - timedelta(hours=self.config.lookback_hours)

        logger.info(
            "Rolling discovery mode=%s start=%s end=%s",
            "latest-per-tile bootstrap" if bootstrap else "new-product follow",
            as_utc_text(start),
            as_utc_text(end),
        )

        discovered_items: list[pystac.Item] = []
        for item in self.catalog.search(
            start_datetime=start,
            end_datetime=end,
            max_items=self.config.max_items_per_cycle,
        ):
            if self.stop_event.is_set():
                break
            discovered_items.append(item)

        discovered = len(discovered_items)
        selected_items = discovered_items
        if bootstrap:
            items_by_tile: dict[str, list[pystac.Item]] = {}
            for item in discovered_items:
                items_by_tile.setdefault(tile_id_from_item(item), []).append(item)
            latest_by_tile: dict[str, pystac.Item] = {}
            for tile_id, tile_items in items_by_tile.items():
                largest_footprint = max(geometry_area(item.geometry) for item in tile_items)
                candidates = [
                    item
                    for item in tile_items
                    if largest_footprint <= 0
                    or geometry_area(item.geometry)
                    >= largest_footprint * _MIN_BOOTSTRAP_FOOTPRINT_RATIO
                ]
                latest_by_tile[tile_id] = max(candidates, key=self._item_recency)
            selected_items = sorted(
                latest_by_tile.values(),
                key=self._item_recency,
            )
            logger.info(
                "Bootstrap selected %d latest near-full-footprint products from %d "
                "matching catalogue items",
                len(selected_items),
                discovered,
            )
        else:
            selected_items = [
                item for item in discovered_items if not self.state.is_ignored(item.id)
            ]

        queued = self._queue_items(run_id, selected_items)

        if not self.stop_event.is_set():
            self.state.set_setting("last_successful_discovery_at", as_utc_text(end))
            if bootstrap and selected_items:
                selected_ids = {item.id for item in selected_items}
                for item_id in selected_ids:
                    self.state.unignore_item(item_id)
                for item in discovered_items:
                    if item.id not in selected_ids:
                        self.state.ignore_item(
                            item.id,
                            "Older product superseded by latest-per-tile bootstrap",
                        )
                self.state.set_setting("bootstrap_completed", "true")
                self.state.record_event(
                    level="INFO",
                    run_id=run_id,
                    message="Latest-per-tile bootstrap completed",
                    details={
                        "catalogue_items": discovered,
                        "selected_latest_products": len(selected_items),
                        "lookback_days": self.config.bootstrap_lookback_days,
                    },
                )
        return discovered, queued

    def _retry_delay(self, completed_attempts: int) -> float:
        exponent = max(0, completed_attempts)
        return min(
            self.config.retry_max_seconds,
            self.config.retry_base_seconds * (2**exponent),
        )

    def _fail_job(
        self,
        *,
        run_id: int,
        job: dict[str, Any],
        error: str,
        asset_key: str | None = None,
        retry_delay_seconds: float | None = None,
        terminal_status: str = "failed",
    ) -> bool:
        attempt_number = int(job["attempts"]) + 1
        terminal = attempt_number >= self.config.max_attempts
        next_attempt = None
        if not terminal:
            next_attempt = self.now() + timedelta(
                seconds=(
                    self._retry_delay(int(job["attempts"]))
                    if retry_delay_seconds is None
                    else retry_delay_seconds
                )
            )
        self.state.mark_job_retry(
            job["item_id"],
            error=error,
            next_attempt_at=next_attempt,
            terminal=terminal,
            terminal_status=terminal_status,
        )
        self.state.record_event(
            level="ERROR",
            run_id=run_id,
            item_id=job["item_id"],
            asset_key=asset_key,
            message=error,
            details={
                "attempt": attempt_number,
                "terminal": terminal,
                "terminal_status": terminal_status if terminal else None,
                "next_attempt_at": (
                    None if next_attempt is None else as_utc_text(next_attempt)
                ),
            },
        )
        logger.error(
            "Job attempt failed – item=%s asset=%s attempt=%d terminal=%s "
            "terminal_status=%s next_attempt=%s error=%s",
            job["item_id"],
            asset_key,
            attempt_number,
            terminal,
            terminal_status if terminal else None,
            None if next_attempt is None else as_utc_text(next_attempt),
            error,
        )
        self._write_status(job["item_id"])
        return terminal

    def _process_job(self, run_id: int, job: dict[str, Any]) -> str:
        item_id = str(job["item_id"])
        if not self.state.claim_job(item_id):
            return "skipped"
        refreshed = self.state.job_snapshot(item_id)
        missing = list(refreshed["missing_assets"])
        if missing and self.config.require_all_assets:
            error = f"Required STAC assets are missing: {', '.join(missing)}"
            self.state.mark_job_retry(
                item_id,
                error=error,
                next_attempt_at=None,
                terminal=True,
            )
            self.state.record_event(
                level="ERROR",
                run_id=run_id,
                item_id=item_id,
                message=error,
                details={"terminal": True},
            )
            logger.error(
                "Required assets missing – item=%s missing=%s status=%s",
                item_id,
                ",".join(missing),
                "failed",
            )
            self._write_status(item_id)
            return "failed"

        assets = self.state.assets_for_job(item_id)
        for asset in assets:
            if asset["status"] == "completed":
                continue
            asset_key = str(asset["asset_key"])
            self.state.mark_asset_processing(item_id, asset_key)
            try:
                if self.downloader is None:
                    raise RuntimeError("S3 downloader is not configured")
                logger.info(
                    "Downloading asset – item=%s asset=%s source=%s destination=%s",
                    item_id,
                    asset_key,
                    asset["href"],
                    Path(asset["local_path"]).resolve(),
                )
                result = self.downloader.download(
                    asset["href"],
                    asset["local_path"],
                    stop_requested=self.stop_event.is_set,
                )
                self.state.mark_asset_complete(
                    item_id,
                    asset_key,
                    byte_count=result.byte_count,
                    sha256=result.sha256,
                )
                self.dataset.enrich_raster(
                    item_id=item_id,
                    asset_key=asset_key,
                    path=result.path,
                )
                self.state.record_event(
                    level="INFO",
                    run_id=run_id,
                    item_id=item_id,
                    asset_key=asset_key,
                    message="Asset download complete",
                    details={
                        "bytes": result.byte_count,
                        "sha256": result.sha256,
                        "resumed_from": result.resumed_from,
                        "skipped": result.skipped,
                    },
                )
                self._write_status(item_id)
                logger.info(
                    "Asset complete – item=%s asset=%s bytes=%d sha256=%s path=%s",
                    item_id,
                    asset_key,
                    result.byte_count,
                    result.sha256,
                    result.path.resolve(),
                )
            except DownloadInterrupted as exc:
                self.state.requeue_asset(item_id, asset_key, str(exc))
                self.state.requeue_job(item_id, str(exc))
                self.state.record_event(
                    level="WARNING",
                    run_id=run_id,
                    item_id=item_id,
                    asset_key=asset_key,
                    message=str(exc),
                )
                self._write_status(item_id)
                return "stopped"
            except QuotaExceededError as exc:
                error = str(exc)
                self.state.mark_asset_retry(item_id, asset_key, error)
                retry_delay = (
                    exc.retry_after_seconds
                    if exc.retry_after_seconds is not None
                    else self.config.quota_retry_seconds
                )
                terminal = self._fail_job(
                    run_id=run_id,
                    job=job,
                    error=error,
                    asset_key=asset_key,
                    retry_delay_seconds=retry_delay,
                    terminal_status="retired",
                )
                if terminal:
                    self.state.mark_asset_retired(item_id, asset_key, error)
                    self._write_status(item_id)
                    logger.error(
                        "Retired %s after %d quota/throttling attempts",
                        item_id,
                        self.config.max_attempts,
                    )
                    return "retired"
                logger.warning(
                    "Quota/throttling limit for %s/%s; waiting %.0f seconds before retry",
                    item_id,
                    asset_key,
                    retry_delay,
                )
                return "retry"
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                self.state.mark_asset_retry(item_id, asset_key, error)
                terminal = self._fail_job(
                    run_id=run_id,
                    job=job,
                    error=error,
                    asset_key=asset_key,
                )
                return "failed" if terminal else "retry"

        self.state.mark_job_complete(item_id)
        self.state.record_event(
            level="INFO",
            run_id=run_id,
            item_id=item_id,
            message="All requested assets are complete",
        )
        self._write_status(item_id)
        return "completed"

    def run_once(self) -> RollingRunResult:
        """Run one reconciliation scan and process the currently due queue."""

        self._prepare_state()
        run_id = self.state.start_run()
        discovered = queued = completed = failed = 0
        errors: list[str] = []
        try:
            try:
                discovered, queued = self._discover(run_id)
            except Exception as exc:  # noqa: BLE001
                error = f"Discovery failed: {type(exc).__name__}: {exc}"
                logger.exception(error)
                errors.append(error)
                self.state.record_event(level="ERROR", run_id=run_id, message=error)

            if not self.stop_event.is_set():
                for job in self.state.due_jobs(limit=self.config.max_jobs_per_cycle):
                    if self.stop_event.is_set():
                        break
                    outcome = self._process_job(run_id, job)
                    if outcome == "completed":
                        completed += 1
                    elif outcome == "failed":
                        failed += 1
                    elif outcome == "retired":
                        failed += 1
                    elif outcome == "retry":
                        errors.append(f"{job['item_id']}: queued for retry")
                    elif outcome == "stopped":
                        break
        except Exception as exc:  # noqa: BLE001
            error = f"Rolling cycle failed: {type(exc).__name__}: {exc}"
            logger.exception(error)
            errors.append(error)

        status = "stopped" if self.stop_event.is_set() else ("error" if errors else "complete")
        self.state.finish_run(
            run_id,
            status=status,
            discovered=discovered,
            queued=queued,
            completed=completed,
            failed=failed,
            error="; ".join(errors) or None,
        )
        self.dataset.upsert_run(
            self.state.run_snapshot(run_id),
            mode="watch",
            state_db=self.config.state_db,
        )
        logger.info(
            "Rolling cycle complete – run=%d discovered=%d queued=%d completed=%d "
            "failed=%d stopped=%s",
            run_id,
            discovered,
            queued,
            completed,
            failed,
            self.stop_event.is_set(),
        )
        return RollingRunResult(
            run_id=run_id,
            discovered=discovered,
            queued=queued,
            completed=completed,
            failed=failed,
            errors=tuple(errors),
            stopped=self.stop_event.is_set(),
        )

    def run(self, *, once: bool) -> RollingRunResult:
        """Run once for cron, or poll continuously until SIGINT/SIGTERM."""

        previous_handlers: dict[signal.Signals, Any] = {}

        def handle_signal(signum: int, _frame: FrameType | None) -> None:
            self.request_stop(f"received signal {signal.Signals(signum).name}")

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, handle_signal)

        last_result = RollingRunResult(0, 0, 0, 0, 0, ())
        try:
            with RunLock(self.config.effective_lock_file):
                self._prepare_state()
                while not self.stop_event.is_set():
                    last_result = self.run_once()
                    if once or self.stop_event.is_set():
                        break
                    logger.info(
                        "Next rolling reconciliation in %.0f seconds", self.config.poll_seconds
                    )
                    self.stop_event.wait(self.config.poll_seconds)
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
        if self.stop_event.is_set() and not last_result.stopped:
            last_result = RollingRunResult(
                run_id=last_result.run_id,
                discovered=last_result.discovered,
                queued=last_result.queued,
                completed=last_result.completed,
                failed=last_result.failed,
                errors=last_result.errors,
                stopped=True,
            )
        return last_result
