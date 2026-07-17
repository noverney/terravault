"""STAC catalog discovery client.

Wraps ``pystac_client`` to query the Copernicus Data Space STAC API for
Sentinel imagery covering a configurable area of interest.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator

import pystac
import pystac_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

COPERNICUS_STAC_URL = "https://stac.dataspace.copernicus.eu/v1"

# Switzerland bounding box [west, south, east, north] (WGS-84)
SWITZERLAND_BBOX: list[float] = [5.96, 45.82, 10.49, 47.81]

DEFAULT_COLLECTIONS: list[str] = ["sentinel-2-l2a", "sentinel-2-l1c"]

DEFAULT_MAX_CLOUD_COVER: float = 20.0


class CatalogClient:
    """Discover STAC items from a remote catalog.

    Parameters
    ----------
    catalog_url:
        Root URL of the STAC API.  Defaults to the Copernicus Data Space endpoint.
    collections:
        List of STAC collection IDs to query.
    bbox:
        Spatial filter as ``[west, south, east, north]`` in WGS-84 decimal
        degrees.  Defaults to a bounding box covering Switzerland.
    intersects:
        GeoJSON geometry used as an exact STAC spatial filter.  When supplied,
        it takes precedence over ``bbox``.
    max_cloud_cover:
        Maximum allowed ``eo:cloud_cover`` percentage (0–100).  Items above
        this threshold are excluded.  Pass ``None`` to disable the filter.
    """

    def __init__(
        self,
        catalog_url: str = COPERNICUS_STAC_URL,
        collections: list[str] | None = None,
        bbox: list[float] | None = None,
        intersects: dict[str, Any] | None = None,
        max_cloud_cover: float | None = DEFAULT_MAX_CLOUD_COVER,
    ) -> None:
        self.catalog_url = catalog_url
        self.collections = collections or DEFAULT_COLLECTIONS
        self.intersects = intersects
        self.bbox = None if intersects is not None else (bbox or SWITZERLAND_BBOX)
        self.max_cloud_cover = max_cloud_cover

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_client(self) -> pystac_client.Client:
        """Open a ``pystac_client.Client`` session."""
        return pystac_client.Client.open(self.catalog_url)

    @staticmethod
    def _format_datetime_interval(start: datetime, end: datetime) -> str:
        """Return an RFC-3339 datetime interval string accepted by STAC APIs."""

        def _fmt(dt: datetime) -> str:
            # Ensure UTC then strip microseconds for a clean representation.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        return f"{_fmt(start)}/{_fmt(end)}"

    @staticmethod
    def _cloud_cover(item: pystac.Item) -> float | None:
        """Return the ``eo:cloud_cover`` property of a STAC item, or ``None``."""
        return item.properties.get("eo:cloud_cover")

    @staticmethod
    def item_datetime(item: pystac.Item) -> datetime:
        """Return the item's datetime, falling back to STAC properties."""
        dt = item.datetime
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        raw = item.properties.get("datetime") or item.properties.get("start_datetime")
        if not raw:
            raise ValueError(f"Item {item.id} has no datetime information")

        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        start_datetime: datetime,
        end_datetime: datetime | None = None,
        max_items: int | None = None,
    ) -> Iterator[pystac.Item]:
        """Search for items matching the configured filters.

        Parameters
        ----------
        start_datetime:
            Start of the temporal search window (inclusive).
        end_datetime:
            End of the temporal search window (inclusive).  Defaults to *now*.
        max_items:
            Upper limit on the number of items returned.  ``None`` means no
            limit (the STAC API may still paginate).

        Yields
        ------
        pystac.Item
            Items that pass the cloud-cover filter.
        """
        if end_datetime is None:
            end_datetime = datetime.now(tz=timezone.utc)

        datetime_str = self._format_datetime_interval(start_datetime, end_datetime)

        client = self._open_client()

        logger.info(
            "Searching %s – collections=%s bbox=%s intersects=%s datetime=%s",
            self.catalog_url,
            self.collections,
            self.bbox,
            "yes" if self.intersects is not None else "no",
            datetime_str,
        )

        search_kwargs: dict[str, Any] = {
            "collections": self.collections,
            "datetime": datetime_str,
            "max_items": max_items,
        }
        if self.intersects is not None:
            search_kwargs["intersects"] = self.intersects
        else:
            search_kwargs["bbox"] = self.bbox

        search = client.search(
            **search_kwargs,
        )

        count = 0
        skipped = 0
        for item in search.items():
            cloud = self._cloud_cover(item)
            if self.max_cloud_cover is not None and cloud is not None:
                if cloud > self.max_cloud_cover:
                    logger.debug(
                        "Skipping %s – cloud_cover=%.1f > %.1f",
                        item.id,
                        cloud,
                        self.max_cloud_cover,
                    )
                    skipped += 1
                    continue
            count += 1
            yield item

        logger.info("Search complete – %d items returned, %d skipped (cloud cover)", count, skipped)

    def list_collections(self) -> list[str]:
        """Return the IDs of all collections exposed by the catalog."""
        client = self._open_client()
        return [c.id for c in client.get_collections()]

    def latest_item(
        self,
        start_datetime: datetime,
        end_datetime: datetime | None = None,
        max_items: int | None = None,
    ) -> pystac.Item | None:
        """Return the newest item matching the configured filters."""
        latest: pystac.Item | None = None
        latest_dt: datetime | None = None

        for item in self.search(
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            max_items=max_items,
        ):
            item_dt = self.item_datetime(item)
            if latest_dt is None or item_dt > latest_dt:
                latest = item
                latest_dt = item_dt

        return latest
