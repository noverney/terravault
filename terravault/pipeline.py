"""Main ingestion pipeline.

Orchestrates the full workflow:

1. **Discovery** – query the STAC catalog for new items within a configurable
   time window.
2. **Deduplication** – skip items that have already been ingested.
3. **Metadata persistence** – write STAC item JSON to the local storage tree.
4. **Asset download** – download configured assets via
   :class:`~terravault.downloader.AssetDownloader`.
5. **State update** – record processed items so the next run ingests only
   newer scenes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pystac

from .catalog import CatalogClient, DEFAULT_COLLECTIONS, SWITZERLAND_BBOX
from .downloader import AssetDownloader, DownloadConfig, DownloadResult
from .state import StateManager, SQLiteStateManager
from .storage import StorageManager

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_HOURS = 72


# ---------------------------------------------------------------------------
# Pipeline configuration
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """Top-level configuration for :class:`Pipeline`.

    Parameters
    ----------
    catalog_url:
        Root URL of the STAC API.
    collections:
        STAC collection IDs to query.
    bbox:
        Spatial filter ``[west, south, east, north]`` in WGS-84.
    max_cloud_cover:
        Maximum cloud-cover percentage; items above this are skipped.
    lookback_hours:
        How many hours back to search when there is no recorded state.
        On subsequent runs the pipeline uses the last-processed timestamp
        from the state store instead.
    download:
        Download tuning parameters.
    asset_keys:
        Asset keys to download.  Empty list means *all* assets.
    state_db:
        Path to the SQLite state database.
    storage_root:
        Root directory for downloaded data.
    """

    catalog_url: str = "https://stac.dataspace.copernicus.eu/v1"
    collections: list[str] = field(default_factory=lambda: list(DEFAULT_COLLECTIONS))
    bbox: list[float] = field(default_factory=lambda: list(SWITZERLAND_BBOX))
    max_cloud_cover: float | None = 20.0
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS
    download: DownloadConfig = field(default_factory=DownloadConfig)
    asset_keys: list[str] = field(default_factory=list)
    state_db: str = "terravault_state.db"
    storage_root: str = "satellite_data"


# ---------------------------------------------------------------------------
# Pipeline run result
# ---------------------------------------------------------------------------

@dataclass
class PipelineRunResult:
    """Summary of a single pipeline execution."""

    items_discovered: int = 0
    items_skipped_duplicate: int = 0
    items_processed: int = 0
    download_results: list[DownloadResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def downloads_ok(self) -> int:
        return sum(1 for r in self.download_results if r.success and not r.skipped)

    @property
    def downloads_skipped(self) -> int:
        return sum(1 for r in self.download_results if r.skipped)

    @property
    def downloads_failed(self) -> int:
        return sum(1 for r in self.download_results if not r.success)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Pipeline:
    """Sentinel data ingestion pipeline.

    Example usage::

        from terravault import Pipeline, PipelineConfig

        cfg = PipelineConfig(asset_keys=["B04", "B08"])
        result = Pipeline(cfg).run()
        print(result)

    Parameters
    ----------
    config:
        Pipeline configuration.  A default :class:`PipelineConfig` is used
        when omitted.
    """

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()
        self._catalog = CatalogClient(
            catalog_url=self.config.catalog_url,
            collections=self.config.collections,
            bbox=self.config.bbox,
            max_cloud_cover=self.config.max_cloud_cover,
        )
        self._storage = StorageManager(root=self.config.storage_root)
        dl_config = self.config.download
        dl_config.asset_keys = self.config.asset_keys
        self._downloader = AssetDownloader(storage=self._storage, config=dl_config)
        self._state: StateManager = SQLiteStateManager(self.config.state_db)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _search_window(self) -> tuple[datetime, datetime]:
        """Determine the (start, end) search window for this run."""
        end = datetime.now(tz=timezone.utc)
        last = self._state.last_processed
        if last is not None:
            start = last
            logger.info("Resuming from last processed timestamp: %s", start.isoformat())
        else:
            start = end - timedelta(hours=self.config.lookback_hours)
            logger.info(
                "No prior state found – looking back %d hours to %s",
                self.config.lookback_hours,
                start.isoformat(),
            )
        return start, end

    def _item_datetime(self, item: pystac.Item) -> datetime:
        """Return the datetime of *item*, falling back to *now* on error."""
        dt = item.datetime
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        raw = item.properties.get("datetime") or item.properties.get("start_datetime")
        if raw:
            try:
                from dateutil.parser import parse as _parse  # type: ignore[import-untyped]
                parsed = _parse(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except Exception:  # noqa: BLE001
                pass
        return datetime.now(tz=timezone.utc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, download: bool = True) -> PipelineRunResult:
        """Execute one ingestion run.

        Parameters
        ----------
        download:
            If ``False``, metadata is persisted but assets are not
            downloaded.  Useful for a metadata-only dry-run.

        Returns
        -------
        PipelineRunResult
            Summary statistics for this run.
        """
        result = PipelineRunResult()
        start, end = self._search_window()

        logger.info("Pipeline run started  start=%s  end=%s", start.isoformat(), end.isoformat())

        new_items: list[pystac.Item] = []

        for item in self._catalog.search(start_datetime=start, end_datetime=end):
            result.items_discovered += 1

            if self._state.is_ingested(item.id):
                logger.debug("Skipping already-ingested item %s", item.id)
                result.items_skipped_duplicate += 1
                continue

            # Persist metadata
            try:
                self._storage.save_metadata(item)
            except Exception as exc:  # noqa: BLE001
                msg = f"Failed to save metadata for {item.id}: {exc}"
                logger.error(msg)
                result.errors.append(msg)
                continue

            new_items.append(item)
            result.items_processed += 1

        logger.info(
            "Discovery complete – %d discovered, %d new, %d duplicates",
            result.items_discovered,
            result.items_processed,
            result.items_skipped_duplicate,
        )

        if download and new_items:
            result.download_results = self._downloader.download_items(new_items)

        # Update state for successfully processed items
        for item in new_items:
            item_dt = self._item_datetime(item)
            try:
                self._state.mark_processed(item.id, item_dt)
            except Exception as exc:  # noqa: BLE001
                msg = f"Failed to update state for {item.id}: {exc}"
                logger.error(msg)
                result.errors.append(msg)

        logger.info(
            "Pipeline run finished – processed=%d  downloads_ok=%d  failed=%d",
            result.items_processed,
            result.downloads_ok,
            result.downloads_failed,
        )
        return result
