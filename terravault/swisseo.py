"""Vegetation indices from one published swissEO S2-SR orbit mosaic.

swisstopo already merges Sentinel-2 granules from the same orbital track into
each STAC item.  This module consumes exactly one such published item.  It is
intentionally not described as a monthly composite and it does not imply that
one orbit mosaic covers every pixel of Switzerland.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import requests

from .monthly import calendar_month_window
from .vegetation import (
    DEFAULT_VEGETATION_NODATA,
    REFLECTANCE_BAND_NAMES,
    VEGETATION_INDEX_DEFINITIONS,
    VEGETATION_INDEX_NAMES,
    inspect_raster,
    locate_gdal,
    raster_extent,
    run_gdal,
    write_described_cog,
    write_vegetation_indices,
)

logger = logging.getLogger(__name__)

SWISSEO_STAC_URL = "https://data.geo.admin.ch/api/stac/v1"
SWISSEO_COLLECTION = "ch.swisstopo.swisseo_s2-sr_v100"
SWISSEO_REQUIRED_ASSET_TITLES: tuple[str, ...] = (
    "BANDS-10M",
    "BANDS-20M",
    "MASKS-10M",
    "METADATA",
)
SWISSEO_REFLECTANCE_SCALE = 0.001
SWISSEO_REFLECTANCE_NODATA = 9999
SWISSEO_MASK_NODATA = 255
SWISSEO_CLOUD_OR_SHADOW = 1
SWISSEO_TERRAIN_SHADOW = 100
SWISSEO_ATTRIBUTION_TEMPLATE = (
    "© swisstopo, Contains modified Copernicus Sentinel data {year}"
)
SWISSEO_V200_COLLECTION = "ch.swisstopo.swisseo_s2-sr_v200"


@dataclass(frozen=True)
class SwissEOProductVersion:
    """Everything that differs between swissEO S2-SR product versions.

    The monthly route stacks each version's inputs into the same local layout
    (a 10 m band stack, a 20 m band stack, and a terrain/cloud mask pair), so
    only asset selection, reflectance scaling and the quality rule vary here.
    """

    name: str
    collection: str
    reflectance_scale: float
    reflectance_offset: float
    reflectance_nodata: int
    mask_nodata: int
    # Band names in the order they occupy the stacked 10 m and 20 m inputs.
    bands_10m: tuple[str, ...]
    bands_20m: tuple[str, ...]
    # gdal_calc terms over T (terrain) and Q (cloud) that a valid pixel meets.
    valid_mask_terms: tuple[str, ...]
    quality_rule: str
    # Inserted into product filenames; empty keeps the original v100 names.
    stem_tag: str
    # v200 publishes one file per band; v100 bundles bands in titled assets.
    per_band_assets: bool

    def physical_reflectance(self, variable: str) -> str:
        """gdal_calc expression converting stored values to reflectance."""

        if self.reflectance_offset == 0:
            return f"{variable}*{self.reflectance_scale!r}"
        sign = "-" if self.reflectance_offset < 0 else "+"
        return f"({variable}*{self.reflectance_scale!r}{sign}{abs(self.reflectance_offset)!r})"


SWISSEO_V100 = SwissEOProductVersion(
    name="v100",
    collection=SWISSEO_COLLECTION,
    reflectance_scale=SWISSEO_REFLECTANCE_SCALE,
    reflectance_offset=0.0,
    reflectance_nodata=SWISSEO_REFLECTANCE_NODATA,
    mask_nodata=SWISSEO_MASK_NODATA,
    bands_10m=(),
    bands_20m=(),
    valid_mask_terms=(
        f"T!={SWISSEO_MASK_NODATA}",
        f"Q!={SWISSEO_MASK_NODATA}",
        f"Q!={SWISSEO_CLOUD_OR_SHADOW}",
        f"T!={SWISSEO_TERRAIN_SHADOW}",
    ),
    quality_rule=(
        "valid when the cloud mask is not 1 (cloud or cloud shadow), the terrain "
        "mask is not 100 (terrain shadow), and neither mask is nodata (255)"
    ),
    stem_tag="",
    per_band_assets=False,
)

# v200 facts were checked against the published data, not only the collection
# text: bands are UInt16 with nodata 0 and carry no GDAL scale/offset, so the
# documented scale 0.0001 and offset -0.1 must be applied here (an offset does
# not cancel in a normalised difference).  The cloud mask holds OmniCloudMask
# classes 0 clear, 1 thick cloud, 2 thin cloud, 3 cloud shadow; class 0 also
# fills the area outside the orbit, so "clear" only counts where a band has
# data.  The terrain mask holds the solar incidence angle (0-180), 200 for cast
# shadow and 255 nodata; angles of 90 or more face away from the sun.
# The terrain mask is 255 over lakes and rivers, so rejecting nodata also drops
# open water (~3.5% of Switzerland, measured on July 2026).  That is kept on
# purpose: v200 is a vegetation product and water is excluded, unlike v100.
SWISSEO_V200 = SwissEOProductVersion(
    name="v200",
    collection=SWISSEO_V200_COLLECTION,
    reflectance_scale=0.0001,
    reflectance_offset=-0.1,
    reflectance_nodata=0,
    mask_nodata=255,
    bands_10m=("B03", "B04", "B08"),
    bands_20m=("B05", "B8A", "B11"),
    valid_mask_terms=("T<90", "Q==0"),
    quality_rule=(
        "valid when the OmniCloudMask class is 0 (clear; thick cloud 1, thin cloud 2 "
        "and cloud shadow 3 are rejected) and the solar incidence angle is below 90 "
        "degrees (self-shadowed slopes 90-180, cast shadow 200 and nodata 255 are "
        "rejected), and the reflectance band is not nodata (0). The terrain mask has "
        "no value over lakes and rivers, so v200 composites exclude open water"
    ),
    stem_tag="_v200",
    per_band_assets=True,
)

SWISSEO_PRODUCT_VERSIONS: dict[str, SwissEOProductVersion] = {
    version.name: version for version in (SWISSEO_V100, SWISSEO_V200)
}


def swisseo_product_version(name: str) -> SwissEOProductVersion:
    try:
        return SWISSEO_PRODUCT_VERSIONS[name]
    except KeyError:
        raise ValueError(
            f"Unknown swissEO product version {name!r}; "
            f"choose one of {', '.join(SWISSEO_PRODUCT_VERSIONS)}"
        ) from None


def swisseo_v200_asset(item: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Select one v200 per-band asset, e.g. ``b8a_20m`` or ``cloudmask_10m``.

    v200 asset titles are prose ("NIR 2 (band 8A) - 20m"), so match the stable
    ``_<name>.tif`` key suffix instead.
    """

    suffix = f"_{name.casefold()}.tif"
    matches = [
        dict(asset)
        for key, asset in (item.get("assets") or {}).items()
        if str(key).casefold().endswith(suffix)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"swissEO item {item.get('id')} has {len(matches)} assets ending in "
            f"{suffix!r}; exactly one is required"
        )
    return matches[0]
_FLOAT32_BYTES = 4
_VARIABLES = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _asset_by_title(item: Mapping[str, Any], title: str) -> tuple[str, dict[str, Any]]:
    matches = [
        (str(key), dict(asset))
        for key, asset in (item.get("assets") or {}).items()
        if str(asset.get("title") or "").casefold() == title.casefold()
    ]
    if len(matches) != 1:
        raise ValueError(
            f"swissEO item {item.get('id')} has {len(matches)} assets titled {title!r}; "
            "exactly one is required"
        )
    return matches[0]


def swisseo_band_map(metadata: Mapping[str, Any]) -> dict[str, tuple[str, int]]:
    """Map Sentinel band names to (asset title, one-based band number)."""

    available: dict[str, tuple[str, int]] = {}
    for asset_title in ("BANDS-10M", "BANDS-20M"):
        section = metadata.get(asset_title) or {}
        for number, band in enumerate(section.get("BANDS") or [], start=1):
            band_id = str(band.get("id") or "").upper()
            if band_id:
                available[band_id] = (asset_title, number)
    sentinel_names = {
        "B03": "B3",
        "B04": "B4",
        "B05": "B5",
        "B08": "B8",
        "B8A": "B8A",
        "B11": "B11",
    }
    missing = [band_id for band_id in sentinel_names.values() if band_id not in available]
    if missing:
        raise ValueError("swissEO metadata lacks required bands: " + ", ".join(missing))
    return {name: available[band_id] for name, band_id in sentinel_names.items()}


class SwissEOClient:
    """Minimal public client for swisstopo's STAC and metadata JSON."""

    def __init__(
        self,
        *,
        stac_url: str = SWISSEO_STAC_URL,
        collection: str = SWISSEO_COLLECTION,
        timeout_seconds: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.stac_url = stac_url.rstrip("/")
        self.collection = collection
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def _json(self, url: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(url, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a JSON object from {url}")
        return payload

    def get_item(self, item_id: str) -> dict[str, Any]:
        encoded_collection = quote(self.collection, safe="")
        encoded_item = quote(item_id, safe="")
        return self._json(
            f"{self.stac_url}/collections/{encoded_collection}/items/{encoded_item}"
        )

    def get_metadata(self, item: Mapping[str, Any]) -> dict[str, Any]:
        _key, asset = _asset_by_title(item, "METADATA")
        href = str(asset.get("href") or "")
        if not href:
            raise ValueError(f"swissEO item {item.get('id')} metadata asset has no href")
        return self._json(href)

    def search_month(self, *, year: int, month: int) -> tuple[dict[str, Any], ...]:
        start, end = calendar_month_window(year, month)
        encoded_collection = quote(self.collection, safe="")
        url = f"{self.stac_url}/collections/{encoded_collection}/items"
        params: Mapping[str, Any] | None = {
            "datetime": f"{_iso_z(start)}/{_iso_z(end - timedelta(seconds=1))}",
            "limit": 100,
        }
        items: list[dict[str, Any]] = []
        while url:
            payload = self._json(url, params=params)
            items.extend(dict(feature) for feature in payload.get("features") or [])
            next_links = [
                link
                for link in payload.get("links") or []
                if str(link.get("rel") or "").casefold() == "next"
            ]
            url = str(next_links[0].get("href")) if next_links else ""
            params = None
        items.sort(key=lambda item: (str(item.get("properties", {}).get("datetime")), str(item["id"])))
        return tuple(items)


@dataclass(frozen=True)
class SwissEODiscoveryResult:
    """Published swissEO orbit mosaics available in one calendar month."""

    year: int
    month: int
    items: tuple[dict[str, Any], ...]
    stac_url: str
    collection: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source": "swissEO-S2-SR-published-orbit-mosaics",
            "year": self.year,
            "month": self.month,
            "stac_url": self.stac_url,
            "collection": self.collection,
            "stac_authentication_required": False,
            "item_count": len(self.items),
            "coverage_note": (
                "Each item is a same-orbital-track mosaic. One item is not guaranteed "
                "to cover all Switzerland; do not choose one item as a national monthly composite."
            ),
            "items": [
                {
                    "id": item.get("id"),
                    "datetime": (item.get("properties") or {}).get("datetime"),
                    "bbox": item.get("bbox"),
                    "required_assets_present": all(
                        any(
                            str(asset.get("title") or "").casefold() == title.casefold()
                            for asset in (item.get("assets") or {}).values()
                        )
                        for title in SWISSEO_REQUIRED_ASSET_TITLES
                    ),
                }
                for item in self.items
            ],
        }


def discover_swisseo_month(
    *,
    year: int,
    month: int,
    client: SwissEOClient | None = None,
) -> SwissEODiscoveryResult:
    """List public swissEO orbit mosaics; no token is required."""

    active_client = client or SwissEOClient()
    return SwissEODiscoveryResult(
        year=year,
        month=month,
        items=active_client.search_month(year=year, month=month),
        stac_url=active_client.stac_url,
        collection=active_client.collection,
    )


@dataclass(frozen=True)
class SwissEOImageConfig:
    """Configuration for indices from one exact swissEO STAC item."""

    item_id: str
    output_root: Path
    stac_url: str = SWISSEO_STAC_URL
    collection: str = SWISSEO_COLLECTION
    resolution: float = 20.0
    target_crs: str = "EPSG:2056"
    reflectance_scale: float = SWISSEO_REFLECTANCE_SCALE
    source_nodata: int = SWISSEO_REFLECTANCE_NODATA
    output_nodata: float = DEFAULT_VEGETATION_NODATA
    mask_cloud_and_shadow: bool = True
    mask_terrain_shadow: bool = True
    compression: str = "DEFLATE"
    warp_memory_mib: int = 256
    max_output_gib: float = 16.0
    overwrite: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not self.item_id.strip():
            raise ValueError("item_id is required")
        object.__setattr__(self, "output_root", Path(self.output_root).expanduser())
        if self.resolution <= 0:
            raise ValueError("resolution must be positive")
        if self.reflectance_scale <= 0:
            raise ValueError("reflectance_scale must be positive")
        if not math.isfinite(self.output_nodata):
            raise ValueError("output_nodata must be finite")
        if self.warp_memory_mib < 16:
            raise ValueError("warp_memory_mib must be at least 16")
        if self.max_output_gib <= 0:
            raise ValueError("max_output_gib must be positive")

    @property
    def item_directory(self) -> Path:
        return self.output_root / f"item={self.item_id.casefold()}"

    @property
    def stem(self) -> str:
        safe_id = "".join(character if character.isalnum() or character in "-." else "-" for character in self.item_id)
        resolution = f"{self.resolution:g}m".replace(".", "p")
        return f"swisseo_s2_sr_{safe_id}_{resolution}"


@dataclass(frozen=True)
class SwissEOImageResult:
    """Derived swissEO reflectance/indices and measured elapsed time."""

    reflectance_path: Path
    indices_path: Path
    manifest_path: Path
    stac_item_path: Path
    width: int
    height: int
    timings_seconds: dict[str, float]
    dry_run: bool

    @property
    def elapsed_seconds(self) -> float:
        return self.timings_seconds.get("total", 0.0)


class SwissEOImageProcessor:
    """Calculate indices from one already-merged swissEO orbit mosaic."""

    def __init__(
        self,
        config: SwissEOImageConfig,
        *,
        client: SwissEOClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or SwissEOClient(
            stac_url=config.stac_url,
            collection=config.collection,
        )
        self._gdal = (
            {}
            if config.dry_run
            else locate_gdal(
                "gdal_calc.py",
                "gdal_translate",
                "gdalbuildvrt",
                "gdalwarp",
                "gdalinfo",
            )
        )

    @staticmethod
    def _gdal_href(href: str) -> str:
        return f"/vsicurl/{href}" if href.startswith(("https://", "http://")) else href

    def _source_assets(self, item: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            title: _asset_by_title(item, title)[1]
            for title in SWISSEO_REQUIRED_ASSET_TITLES
        }

    def _warp_asset(
        self,
        *,
        href: str,
        output: Path,
        resampling: str,
        source_nodata: int,
        extent: tuple[float, float, float, float] | None = None,
        bbox_wgs84: Sequence[float] | None = None,
        cutline_path: Path | None = None,
        output_format: str = "VRT",
        creation_options: Sequence[str] = (),
    ) -> None:
        if extent is not None and bbox_wgs84 is not None:
            raise ValueError("extent and bbox_wgs84 are mutually exclusive")
        command = [
            self._gdal["gdalwarp"],
            "-overwrite",
            "-of",
            output_format,
            "-t_srs",
            self.config.target_crs,
            "-tr",
            str(self.config.resolution),
            str(self.config.resolution),
            "-tap",
            "-r",
            resampling,
            "-srcnodata",
            str(source_nodata),
            "-dstnodata",
            str(source_nodata),
            "-wm",
            str(self.config.warp_memory_mib),
            "-multi",
        ]
        for option in creation_options:
            command.extend(("-co", option))
        if extent is not None:
            command.extend(("-te", *(str(value) for value in extent)))
        if bbox_wgs84 is not None:
            if len(bbox_wgs84) != 4:
                raise ValueError("bbox_wgs84 must contain four coordinates")
            command.extend(
                (
                    "-te_srs",
                    "EPSG:4326",
                    "-te",
                    *(str(float(value)) for value in bbox_wgs84),
                )
            )
        if cutline_path is not None:
            command.extend(("-cutline", str(cutline_path), "-crop_to_cutline"))
        command.extend((self._gdal_href(href), str(output)))
        run_gdal(command)

    def _standardized_reflectance(
        self,
        *,
        warped: Mapping[str, Path],
        band_map: Mapping[str, tuple[str, int]],
        output: Path,
    ) -> None:
        command = [self._gdal["gdal_calc.py"]]
        variables: dict[str, str] = {}
        for band_name, variable in zip(
            REFLECTANCE_BAND_NAMES,
            _VARIABLES[: len(REFLECTANCE_BAND_NAMES)],
            strict=True,
        ):
            variables[band_name] = variable
            asset_title, band_number = band_map[band_name]
            command.extend(
                (
                    f"-{variable}",
                    str(warped[asset_title]),
                    f"--{variable}_band",
                    str(band_number),
                )
            )
        command.extend(
            (
                "-T",
                str(warped["MASKS-10M"]),
                "--T_band",
                "1",
                "-Q",
                str(warped["MASKS-10M"]),
                "--Q_band",
                "2",
            )
        )
        mask_parts = [f"T!={SWISSEO_MASK_NODATA}", f"Q!={SWISSEO_MASK_NODATA}"]
        if self.config.mask_cloud_and_shadow:
            mask_parts.append(f"Q!={SWISSEO_CLOUD_OR_SHADOW}")
        if self.config.mask_terrain_shadow:
            mask_parts.append(f"T!={SWISSEO_TERRAIN_SHADOW}")
        for band_name in REFLECTANCE_BAND_NAMES:
            variable = variables[band_name]
            valid = "logical_and.reduce((" + ",".join(
                [f"{variable}!={self.config.source_nodata}", *mask_parts]
            ) + "))"
            command.extend(
                (
                    "--calc",
                    f"where({valid},{variable}*{self.config.reflectance_scale!r},"
                    f"{self.config.output_nodata:g})",
                )
            )
        command.extend(
            (
                "--outfile",
                str(output),
                "--NoDataValue",
                str(self.config.output_nodata),
                "--type",
                "Float32",
                "--format",
                "GTiff",
                "--co",
                f"COMPRESS={self.config.compression}",
                "--co",
                "BIGTIFF=YES",
                "--co",
                "TILED=YES",
                "--hideNoData",
                "--projectionCheck",
                "--overwrite",
                "--quiet",
            )
        )
        run_gdal(command)

    def _estimated_bytes(self, width: int, height: int) -> int:
        return (
            width
            * height
            * (len(REFLECTANCE_BAND_NAMES) + len(VEGETATION_INDEX_NAMES))
            * _FLOAT32_BYTES
        )

    def _dimensions_from_metadata(
        self, metadata: Mapping[str, Any]
    ) -> tuple[int, int]:
        section = metadata.get("BANDS-20M") or {}
        bands = section.get("BANDS") or []
        if not bands or len(bands[0].get("dimensions") or []) != 2:
            raise ValueError("swissEO metadata does not provide BANDS-20M dimensions")
        native_width, native_height = (int(value) for value in bands[0]["dimensions"])
        factor = 20.0 / self.config.resolution
        return math.ceil(native_width * factor), math.ceil(native_height * factor)

    def _manifest(
        self,
        *,
        item: Mapping[str, Any],
        assets: Mapping[str, Mapping[str, Any]],
        metadata: Mapping[str, Any],
        band_map: Mapping[str, tuple[str, int]],
        reflectance_path: Path,
        indices_path: Path,
        stac_item_path: Path,
        width: int,
        height: int,
        timings: Mapping[str, float],
    ) -> dict[str, Any]:
        acquired = _parse_datetime((item.get("properties") or {}).get("datetime"))
        source_section = metadata.get("BANDS-10M") or {}
        properties = (
            source_section.get("SOURCE_COLLECTION_PROPERTIES")
            or source_section.get("PROPERTIES")
            or {}
        )
        return {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "processing_option": "swisseo-published-image",
            "source": "swissEO-S2-SR-published-orbit-mosaic",
            "source_is_monthly_composite": False,
            "source_is_force": False,
            "source_is_treeai": False,
            "formula_reference": "https://www.treeai.earth/swiss-vegetation-condition",
            "stac_url": self.client.stac_url,
            "collection": self.client.collection,
            "stac_authentication_required": False,
            "item_id": item.get("id"),
            "acquisition_datetime": _iso_z(acquired),
            "coverage_note": (
                "swisstopo merges granules from the same orbital track into this item. "
                "This exact item is not asserted to cover all Switzerland."
            ),
            "source_scene_count": properties.get("scene_count"),
            "source_percent_data": properties.get("percent_data"),
            "source_percent_masked": properties.get("percent_masked"),
            "source_orbit_number": properties.get("SENSING_ORBIT_NUMBER"),
            "attribution": SWISSEO_ATTRIBUTION_TEMPLATE.format(year=acquired.year),
            "target_crs": self.config.target_crs,
            "resolution": self.config.resolution,
            "source_reflectance_scale": self.config.reflectance_scale,
            "source_reflectance_nodata": self.config.source_nodata,
            "reflectance_band_order": list(REFLECTANCE_BAND_NAMES),
            "source_band_mapping": {
                name: {"asset_title": title, "band": number}
                for name, (title, number) in band_map.items()
            },
            "index_band_order": list(VEGETATION_INDEX_NAMES),
            "index_definitions": VEGETATION_INDEX_DEFINITIONS,
            "quality_policy": {
                "mask_asset": "MASKS-10M",
                "cloud_and_cloud_shadow_band": 2,
                "cloud_and_cloud_shadow_invalid_value": SWISSEO_CLOUD_OR_SHADOW,
                "terrain_shadow_band": 1,
                "terrain_shadow_invalid_value": SWISSEO_TERRAIN_SHADOW,
                "mask_cloud_and_shadow": self.config.mask_cloud_and_shadow,
                "mask_terrain_shadow": self.config.mask_terrain_shadow,
                "mask_nodata": SWISSEO_MASK_NODATA,
            },
            "assets": {
                title: {"href": asset.get("href"), "title": asset.get("title")}
                for title, asset in assets.items()
            },
            "width": width,
            "height": height,
            "estimated_uncompressed_bytes": self._estimated_bytes(width, height),
            "dry_run": self.config.dry_run,
            "outputs": {
                "reflectance": str(reflectance_path),
                "indices": str(indices_path),
                "stac_item": str(stac_item_path),
            },
            "timings_seconds": {
                key: round(float(value), 3) for key, value in timings.items()
            },
        }

    def _write_stac_item(
        self,
        *,
        item: Mapping[str, Any],
        path: Path,
        reflectance_path: Path,
        indices_path: Path,
        manifest_path: Path,
        elapsed_seconds: float,
    ) -> None:
        acquired = _parse_datetime((item.get("properties") or {}).get("datetime"))
        _write_json_atomic(
            path,
            {
                "stac_version": "1.0.0",
                "type": "Feature",
                "id": self.config.stem,
                "bbox": item.get("bbox"),
                "geometry": item.get("geometry"),
                "properties": {
                    "datetime": _iso_z(acquired),
                    "gsd": self.config.resolution,
                    "terravault:source": "swissEO-S2-SR-published-orbit-mosaic",
                    "terravault:source_item": item.get("id"),
                    "terravault:elapsed_seconds": round(elapsed_seconds, 3),
                    "attribution": SWISSEO_ATTRIBUTION_TEMPLATE.format(year=acquired.year),
                },
                "links": [
                    {
                        "rel": "derived_from",
                        "href": (
                            f"{self.client.stac_url}/collections/{self.client.collection}/"
                            f"items/{item.get('id')}"
                        ),
                        "type": "application/geo+json",
                    }
                ],
                "assets": {
                    "reflectance": {
                        "href": reflectance_path.name,
                        "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                        "roles": ["data"],
                        "terravault:band_order": list(REFLECTANCE_BAND_NAMES),
                    },
                    "vegetation_indices": {
                        "href": indices_path.name,
                        "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                        "roles": ["data"],
                        "terravault:band_order": list(VEGETATION_INDEX_NAMES),
                    },
                    "manifest": {
                        "href": manifest_path.name,
                        "type": "application/json",
                        "roles": ["metadata"],
                    },
                },
            },
        )

    def run(self) -> SwissEOImageResult:
        """Create or plan vegetation indices for one exact swissEO item."""

        started = time.perf_counter()
        stage = time.perf_counter()
        item = self.client.get_item(self.config.item_id)
        if str(item.get("collection") or self.client.collection) != self.client.collection:
            raise ValueError(f"Item {self.config.item_id} is not in {self.client.collection}")
        assets = self._source_assets(item)
        metadata = self.client.get_metadata(item)
        band_map = swisseo_band_map(metadata)
        timings: dict[str, float] = {"stac_and_metadata": time.perf_counter() - stage}

        output_directory = self.config.item_directory.resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        reflectance_path = output_directory / f"{self.config.stem}_reflectance.tif"
        indices_path = output_directory / f"{self.config.stem}_indices.tif"
        manifest_path = output_directory / f"{self.config.stem}.manifest.json"
        stac_item_path = output_directory / f"{self.config.stem}.stac-item.json"
        existing = [path for path in (reflectance_path, indices_path) if path.exists()]
        if existing and not self.config.overwrite and not self.config.dry_run:
            raise FileExistsError(
                "swissEO derived output already exists; pass --overwrite: "
                + ", ".join(str(path) for path in existing)
            )

        width, height = self._dimensions_from_metadata(metadata)
        estimated_bytes = self._estimated_bytes(width, height)
        if estimated_bytes / (1024**3) > self.config.max_output_gib:
            raise ValueError(
                f"Estimated uncompressed outputs are {estimated_bytes / (1024**3):.2f} GiB, "
                f"above the {self.config.max_output_gib:.2f} GiB safety limit"
            )

        if self.config.dry_run:
            timings["total"] = time.perf_counter() - started
            _write_json_atomic(
                manifest_path,
                self._manifest(
                    item=item,
                    assets=assets,
                    metadata=metadata,
                    band_map=band_map,
                    reflectance_path=reflectance_path,
                    indices_path=indices_path,
                    stac_item_path=stac_item_path,
                    width=width,
                    height=height,
                    timings=timings,
                ),
            )
            return SwissEOImageResult(
                reflectance_path=reflectance_path,
                indices_path=indices_path,
                manifest_path=manifest_path,
                stac_item_path=stac_item_path,
                width=width,
                height=height,
                timings_seconds=timings,
                dry_run=True,
            )

        partial_reflectance = reflectance_path.with_name(
            f".{reflectance_path.name}.{uuid.uuid4().hex}.partial.tif"
        )
        partial_indices = indices_path.with_name(
            f".{indices_path.name}.{uuid.uuid4().hex}.partial.tif"
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix=f"terravault-swisseo-{self.config.item_id}-",
                dir=output_directory,
            ) as temporary:
                temporary_path = Path(temporary)
                warped: dict[str, Path] = {}
                stage = time.perf_counter()
                bands20 = temporary_path / "bands-20m.vrt"
                self._warp_asset(
                    href=str(assets["BANDS-20M"]["href"]),
                    output=bands20,
                    resampling="bilinear",
                    source_nodata=self.config.source_nodata,
                )
                warped["BANDS-20M"] = bands20
                reference_info = inspect_raster(self._gdal["gdalinfo"], bands20)
                extent = raster_extent(reference_info)
                for title, resampling, nodata in (
                    ("BANDS-10M", "bilinear", self.config.source_nodata),
                    # A 20 m output represents four 10 m quality pixels. Max
                    # conservatively retains cloud=1 or direct-shadow=100 if
                    # any contributing native mask pixel is invalid.
                    ("MASKS-10M", "max", SWISSEO_MASK_NODATA),
                ):
                    output = temporary_path / f"{title.casefold()}.vrt"
                    self._warp_asset(
                        href=str(assets[title]["href"]),
                        output=output,
                        resampling=resampling,
                        source_nodata=nodata,
                        extent=extent,
                    )
                    warped[title] = output
                timings["remote_cog_alignment"] = time.perf_counter() - stage

                stage = time.perf_counter()
                standardized = temporary_path / "reflectance.tif"
                self._standardized_reflectance(
                    warped=warped,
                    band_map=band_map,
                    output=standardized,
                )
                write_described_cog(
                    source_path=standardized,
                    output_path=partial_reflectance,
                    band_names=REFLECTANCE_BAND_NAMES,
                    gdal=self._gdal,
                    compression=self.config.compression,
                    metadata={
                        "TERRAVAULT_SOURCE": "swissEO-S2-SR-published-orbit-mosaic",
                        "TERRAVAULT_SOURCE_ITEM": self.config.item_id,
                    },
                )
                timings["quality_mask_and_reflectance_cog"] = time.perf_counter() - stage

                exact_info = inspect_raster(self._gdal["gdalinfo"], partial_reflectance)
                width, height = (int(value) for value in exact_info["size"])
                estimated_bytes = self._estimated_bytes(width, height)
                if estimated_bytes / (1024**3) > self.config.max_output_gib:
                    raise ValueError(
                        f"Exact uncompressed outputs are {estimated_bytes / (1024**3):.2f} GiB, "
                        f"above the {self.config.max_output_gib:.2f} GiB safety limit"
                    )

                stage = time.perf_counter()
                write_vegetation_indices(
                    reflectance_path=partial_reflectance,
                    output_path=partial_indices,
                    gdal=self._gdal,
                    nodata=self.config.output_nodata,
                    compression=self.config.compression,
                    metadata={
                        "TERRAVAULT_SOURCE": "swissEO-S2-SR-published-orbit-mosaic",
                        "TERRAVAULT_SOURCE_ITEM": self.config.item_id,
                        "TERRAVAULT_INDICES": ",".join(VEGETATION_INDEX_NAMES),
                    },
                )
                timings["index_calculation"] = time.perf_counter() - stage
            os.replace(partial_reflectance, reflectance_path)
            os.replace(partial_indices, indices_path)
        finally:
            partial_reflectance.unlink(missing_ok=True)
            partial_indices.unlink(missing_ok=True)

        timings["total"] = time.perf_counter() - started
        _write_json_atomic(
            manifest_path,
            self._manifest(
                item=item,
                assets=assets,
                metadata=metadata,
                band_map=band_map,
                reflectance_path=reflectance_path,
                indices_path=indices_path,
                stac_item_path=stac_item_path,
                width=width,
                height=height,
                timings=timings,
            ),
        )
        self._write_stac_item(
            item=item,
            path=stac_item_path,
            reflectance_path=reflectance_path,
            indices_path=indices_path,
            manifest_path=manifest_path,
            elapsed_seconds=timings["total"],
        )
        return SwissEOImageResult(
            reflectance_path=reflectance_path,
            indices_path=indices_path,
            manifest_path=manifest_path,
            stac_item_path=stac_item_path,
            width=width,
            height=height,
            timings_seconds=timings,
            dry_run=False,
        )
