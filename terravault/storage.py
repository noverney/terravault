"""Local storage management for downloaded satellite data.

Creates and manages the following directory structure::

    satellite_data/
        sentinel2/
            YYYY/
                MM/
                    DD/
                        TILE_ID/
                            scene_metadata.json
                            *.tif
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pystac

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path("satellite_data")


def _tile_id_from_item(item: pystac.Item) -> str:
    """Best-effort extraction of a human-readable tile ID from a STAC item.

    Tries common property names in order:
    * ``s2:mgrs_tile``
    * ``mgrs:utm_zone`` + ``mgrs:latitude_band`` + ``mgrs:grid_square``
    * Falls back to the item ID itself.
    """
    props = item.properties

    # Sentinel-2 MGRS tile (e.g. "32TNT")
    mgrs = props.get("s2:mgrs_tile") or props.get("mgrs_tile")
    if mgrs:
        return str(mgrs)

    # Build from individual MGRS components
    utm = props.get("mgrs:utm_zone")
    lat = props.get("mgrs:latitude_band")
    grid = props.get("mgrs:grid_square")
    if utm and lat and grid:
        return f"{utm}{lat}{grid}"

    # Use the plain item ID as a safe fallback
    return item.id


def _item_date(item: pystac.Item) -> datetime:
    """Return the acquisition date of a STAC item."""
    dt = item.datetime
    if dt is None:
        # Some items store start/end instead of a single datetime
        raw = item.properties.get("datetime") or item.properties.get("start_datetime")
        if raw:
            from datetime import timezone
            from dateutil.parser import parse as _parse  # type: ignore[import-untyped]
            dt = _parse(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    if dt is None:
        raise ValueError(f"Item {item.id!r} has no datetime information")
    return dt


class StorageManager:
    """Manages local storage for satellite scene metadata and assets.

    Parameters
    ----------
    root:
        Root directory for all stored data.
    mission:
        Sub-directory name used as the *mission* path component
        (e.g. ``"sentinel2"``).
    """

    def __init__(
        self,
        root: str | Path = DEFAULT_ROOT,
        mission: str = "sentinel2",
    ) -> None:
        self.root = Path(root)
        self.mission = mission

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def scene_dir(self, item: pystac.Item) -> Path:
        """Return (and create) the directory for a particular scene."""
        dt = _item_date(item)
        tile = _tile_id_from_item(item)
        path = (
            self.root
            / self.mission
            / f"{dt.year:04d}"
            / f"{dt.month:02d}"
            / f"{dt.day:02d}"
            / tile
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def metadata_path(self, item: pystac.Item) -> Path:
        """Return the path to the JSON metadata file for *item*."""
        return self.scene_dir(item) / "scene_metadata.json"

    def asset_path(self, item: pystac.Item, asset_key: str, suffix: str = ".tif") -> Path:
        """Return the local path for an asset file.

        Parameters
        ----------
        item:
            The STAC item that owns the asset.
        asset_key:
            The asset key as defined in the STAC item (e.g. ``"B04"``).
        suffix:
            File extension override.  Defaults to ``.tif``.
        """
        filename = f"{asset_key}{suffix}"
        return self.scene_dir(item) / filename

    # ------------------------------------------------------------------
    # Metadata persistence
    # ------------------------------------------------------------------

    def save_metadata(self, item: pystac.Item) -> Path:
        """Persist STAC item metadata as JSON and return the written path.

        If the metadata file already exists it is **overwritten** so that
        any property updates from the catalog are captured.
        """
        path = self.metadata_path(item)
        metadata: dict[str, Any] = item.to_dict()
        path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.debug("Saved metadata → %s", path)
        return path

    def load_metadata(self, item: pystac.Item) -> dict[str, Any]:
        """Load and return the persisted metadata for *item*."""
        path = self.metadata_path(item)
        return json.loads(path.read_text(encoding="utf-8"))

    def metadata_exists(self, item: pystac.Item) -> bool:
        """Return ``True`` if metadata for *item* has already been persisted."""
        return self.metadata_path(item).exists()
