"""Helpers for fetching Sentinel-2 patches through the CDSE Process API."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .auth import CDSEAccessTokenProvider, SentinelHubAuthConfig

PROCESS_API_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
PROCESS_API_MAX_DIMENSION = 2500

_BAND_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_SAMPLE_TYPES = {"AUTO", "UINT8", "UINT16", "FLOAT32"}


@dataclass
class ProcessPatchConfig:
    """Configuration for a Sentinel Hub Process API patch request."""

    bbox: list[float]
    time_from: str
    time_to: str
    bands: list[str] = field(default_factory=lambda: ["B04", "B08"])
    width: int = 512
    height: int = 512
    collection: str = "sentinel-2-l2a"
    output_format: str = "image/tiff"
    units: str | list[str] = "DN"
    sample_type: str = "UINT16"
    gain: float = 1.0
    harmonize_values: bool = False
    max_cloud_cover: float | None = None
    mosaicking_order: str = "mostRecent"
    process_url: str = PROCESS_API_URL

    def __post_init__(self) -> None:
        if len(self.bbox) != 4:
            raise ValueError("bbox must contain [west, south, east, north]")
        west, south, east, north = self.bbox
        if west >= east or south >= north:
            raise ValueError("bbox must have west < east and south < north")
        if not self.bands:
            raise ValueError("at least one band is required")
        invalid_bands = [band for band in self.bands if not _BAND_NAME_RE.fullmatch(band)]
        if invalid_bands:
            raise ValueError(f"invalid band names: {', '.join(invalid_bands)}")
        if not 1 <= self.width <= PROCESS_API_MAX_DIMENSION:
            raise ValueError(
                f"width must be between 1 and {PROCESS_API_MAX_DIMENSION} pixels"
            )
        if not 1 <= self.height <= PROCESS_API_MAX_DIMENSION:
            raise ValueError(
                f"height must be between 1 and {PROCESS_API_MAX_DIMENSION} pixels"
            )
        self.sample_type = self.sample_type.upper()
        if self.sample_type not in _SAMPLE_TYPES:
            raise ValueError(
                f"sample_type must be one of {', '.join(sorted(_SAMPLE_TYPES))}"
            )
        if isinstance(self.units, list) and len(self.units) != len(self.bands):
            raise ValueError("units must have exactly one entry per band")
        if not math.isfinite(self.gain):
            raise ValueError("gain must be a finite number")


def bbox_from_center(
    center_lon: float,
    center_lat: float,
    width: int,
    height: int,
    resolution_m: float,
) -> list[float]:
    """Approximate a WGS-84 bbox around a center point for a pixel grid."""

    half_width_m = width * resolution_m / 2
    half_height_m = height * resolution_m / 2
    meters_per_degree_lat = 111_320.0
    meters_per_degree_lon = meters_per_degree_lat * math.cos(math.radians(center_lat))
    if abs(meters_per_degree_lon) < 1e-9:
        raise ValueError("Longitude conversion is unstable at the poles")

    lon_delta = half_width_m / meters_per_degree_lon
    lat_delta = half_height_m / meters_per_degree_lat
    return [
        center_lon - lon_delta,
        center_lat - lat_delta,
        center_lon + lon_delta,
        center_lat + lat_delta,
    ]


def output_size_for_bbox(
    bbox: list[float],
    max_dimension: int = 2000,
) -> tuple[int, int]:
    """Return an approximately square-pixel output size for a WGS-84 bbox.

    The calculation is intentionally lightweight and is suitable for overview
    products. Production/native-resolution exports should use a projected CRS.
    """

    if len(bbox) != 4:
        raise ValueError("bbox must contain [west, south, east, north]")
    west, south, east, north = bbox
    if west >= east or south >= north:
        raise ValueError("bbox must have west < east and south < north")
    if not 1 <= max_dimension <= PROCESS_API_MAX_DIMENSION:
        raise ValueError(
            f"max_dimension must be between 1 and {PROCESS_API_MAX_DIMENSION}"
        )

    center_lat = (south + north) / 2
    width_m = (east - west) * 111_320.0 * math.cos(math.radians(center_lat))
    height_m = (north - south) * 111_320.0
    if width_m >= height_m:
        width = max_dimension
        height = max(1, round(max_dimension * height_m / width_m))
    else:
        height = max_dimension
        width = max(1, round(max_dimension * width_m / height_m))
    return width, height


def _build_evalscript(config: ProcessPatchConfig) -> str:
    bands_js = ", ".join(f'"{band}"' for band in config.bands)
    units_js = json.dumps(config.units)
    if config.gain == 1.0:
        values_js = ", ".join(f"sample.{band}" for band in config.bands)
    else:
        values_js = ", ".join(f"sample.{band} * {config.gain}" for band in config.bands)
    return f"""//VERSION=3
function setup() {{
  return {{
    input: [
      {{
        bands: [{bands_js}],
        units: {units_js},
      }},
    ],
    output: {{
      id: "default",
      bands: {len(config.bands)},
      sampleType: SampleType.{config.sample_type},
    }},
  }}
}}

function evaluatePixel(sample) {{
  return [{values_js}]
}}
"""


def _build_process_request(config: ProcessPatchConfig) -> dict:
    data_request = {
        "type": config.collection,
        "dataFilter": {
            "timeRange": {
                "from": config.time_from,
                "to": config.time_to,
            },
            "mosaickingOrder": config.mosaicking_order,
        },
        "processing": {"harmonizeValues": config.harmonize_values},
    }
    if config.max_cloud_cover is not None:
        data_request["dataFilter"]["maxCloudCoverage"] = config.max_cloud_cover

    return {
        "input": {
            "bounds": {
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
                "bbox": config.bbox,
            },
            "data": [data_request],
        },
        "output": {
            "width": config.width,
            "height": config.height,
            "responses": [{"identifier": "default", "format": {"type": config.output_format}}],
        },
        "evalscript": _build_evalscript(config),
    }


def build_process_request(config: ProcessPatchConfig) -> dict:
    """Build the JSON request body without making a network request."""

    return _build_process_request(config)


def fetch_sentinel2_patch(
    config: ProcessPatchConfig,
    output_path: str | Path,
    auth: SentinelHubAuthConfig,
    session_factory: type[requests.Session] | None = None,
) -> Path:
    """Fetch a Sentinel-2 patch and write it to *output_path*."""

    provider = CDSEAccessTokenProvider(auth, session_factory=session_factory)
    session = (session_factory or requests.Session)()
    try:
        response = session.post(
            config.process_url,
            json=build_process_request(config),
            headers={"Authorization": f"Bearer {provider.get_token()}"},
            timeout=120,
        )
        response.raise_for_status()
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return path
