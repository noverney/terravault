"""STAC catalog discovery client.

Wraps ``pystac_client`` to query the Copernicus Data Space STAC API for
Sentinel imagery covering a configurable area of interest.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterator

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
    max_cloud_cover:
        Maximum allowed ``eo:cloud_cover`` percentage (0–100).  Items above
        this threshold are excluded.  Pass ``None`` to disable the filter.
    """

    def __init__(
        self,
        catalog_url: str = COPERNICUS_STAC_URL,
        collections: list[str] | None = None,
        bbox: list[float] | None = None,
        max_cloud_cover: float | None = DEFAULT_MAX_CLOUD_COVER,
    ) -> None:
        self.catalog_url = catalog_url
        self.collections = collections or DEFAULT_COLLECTIONS
        self.bbox = bbox or SWITZERLAND_BBOX
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
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        return f"{_fmt(start)}/{_fmt(end)}"

    @staticmethod
    def _cloud_cover(item: pystac.Item) -> float | None:
        """Return the ``eo:cloud_cover`` property of a STAC item, or ``None``."""
        return item.properties.get("eo:cloud_cover")

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
            "Searching %s – collections=%s bbox=%s datetime=%s",
            self.catalog_url,
            self.collections,
            self.bbox,
            datetime_str,
        )

        search = client.search(
            collections=self.collections,
            bbox=self.bbox,
            datetime=datetime_str,
            max_items=max_items,
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
