"""Monthly vegetation composites made from genuine native FORCE BOA/QAI.

The input to this module is the output of :mod:`terravault.force_level2`, not
CDSE L2A and not swissEO.  Every completed FORCE acquisition in a calendar
month is quality-screened with its matching QAI raster, converted from scaled
BOA integers to physical reflectance, and combined with a per-pixel median.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .catalog import SWITZERLAND_BBOX
from .force_pipeline import ForcePipelineDatabase
from .vegetation import (
    DEFAULT_VEGETATION_NODATA,
    REFLECTANCE_BAND_NAMES,
    VEGETATION_INDEX_DEFINITIONS,
    VEGETATION_INDEX_NAMES,
    bbox_envelope,
    inspect_raster,
    locate_gdal,
    pixel_window,
    run_gdal,
    set_vrt_band_descriptions,
    write_described_cog,
    write_vegetation_indices,
)

logger = logging.getLogger(__name__)

MONTHLY_BAND_NAMES = REFLECTANCE_BAND_NAMES
MONTHLY_INDEX_NAMES = VEGETATION_INDEX_NAMES
FORCE_BOA_SCALE = 0.0001
FORCE_BOA_NODATA = -9999
DEFAULT_FORCE_QAI_MASK = 0x031F
_FLOAT32_BYTES = 4
_VARIABLES = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
# Source pixels kept around the bbox so the final bilinear, target-aligned
# warp has neighbours at the patch edge.
_WINDOW_BUFFER_PIXELS = 4
# Each scene is screened by its own gdal_calc process, and one scene used to
# wait for the previous one: 12 windowed July 2024 scenes took 128 s serially.
_SCENE_WORKERS = max(1, int(os.environ.get("TERRAVAULT_FORCE_MONTHLY_SCENE_WORKERS", "6")))

# Native FORCE Level-2 BOA order.  The selected Sentinel-2 meanings are kept
# explicit so BROADNIR (B08) can never be confused with NIR (B8A).
FORCE_BOA_DOMAINS: tuple[str, ...] = (
    "BLUE",
    "GREEN",
    "RED",
    "REDEDGE1",
    "REDEDGE2",
    "REDEDGE3",
    "BROADNIR",
    "NIR",
    "SWIR1",
    "SWIR2",
)
FORCE_REFLECTANCE_BAND_MAP: dict[str, tuple[str, int]] = {
    "B03": ("GREEN", 2),
    "B04": ("RED", 3),
    "B05": ("REDEDGE1", 4),
    "B08": ("BROADNIR", 7),
    "B8A": ("NIR", 8),
    "B11": ("SWIR1", 9),
}


def calendar_month_window(year: int, month: int) -> tuple[datetime, datetime]:
    """Return the UTC half-open interval for one calendar month."""

    if not 1 <= int(month) <= 12:
        raise ValueError("month must be between 1 and 12")
    if not 2015 <= int(year) <= 2100:
        raise ValueError("year must be between 2015 and 2100")
    start = datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(int(year) + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(int(year), int(month) + 1, 1, tzinfo=timezone.utc)
    return start, end


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


def _validate_bbox(bbox: Sequence[float]) -> tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("bbox must contain west, south, east and north")
    west, south, east, north = (float(value) for value in bbox)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox must be a valid WGS84 extent")
    return west, south, east, north


def _approximate_dimensions(
    bbox: Sequence[float], resolution: float
) -> tuple[int, int]:
    west, south, east, north = bbox
    centre_latitude = (south + north) / 2
    width_metres = (east - west) * 111_320.0 * math.cos(math.radians(centre_latitude))
    height_metres = (north - south) * 111_320.0
    return math.ceil(width_metres / resolution), math.ceil(height_metres / resolution)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class ForceMonthlyScene:
    """One verified, completed FORCE acquisition and its matching mosaics."""

    item_id: str
    acquisition_time: datetime
    platform: str
    cloud_cover: float | None
    boa_path: Path
    qai_path: Path


@dataclass(frozen=True)
class ForceMonthlyDiscoveryResult:
    """Completed native FORCE acquisitions found for one month."""

    year: int
    month: int
    database_path: Path
    scenes: tuple[ForceMonthlyScene, ...]
    skipped_items: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, Any]:
        start, end = calendar_month_window(self.year, self.month)
        return {
            "schema_version": 1,
            "source": "native-force-level2-boa-qai",
            "year": self.year,
            "month": self.month,
            "start_datetime": _iso_z(start),
            "end_datetime_exclusive": _iso_z(end),
            "force_database": str(self.database_path),
            "scene_count": len(self.scenes),
            "scenes": [
                {
                    "item_id": scene.item_id,
                    "acquisition_time": _iso_z(scene.acquisition_time),
                    "platform": scene.platform,
                    "cloud_cover": scene.cloud_cover,
                    "boa_mosaic": str(scene.boa_path),
                    "qai_mosaic": str(scene.qai_path),
                }
                for scene in self.scenes
            ],
            "skipped_items": {
                item_id: list(reasons) for item_id, reasons in self.skipped_items.items()
            },
        }


def discover_force_month(
    *, year: int, month: int, force_db: str | Path
) -> ForceMonthlyDiscoveryResult:
    """Read one month of paired BOA/QAI mosaics from the FORCE pipeline DB."""

    start, end = calendar_month_window(year, month)
    database_path = Path(force_db).expanduser().resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"FORCE pipeline database does not exist: {database_path}")
    with ForcePipelineDatabase(database_path) as database:
        scene_rows = database.scenes()
        image_rows = database.images()

    paths_by_item: dict[str, dict[str, list[Path]]] = {}
    for row in image_rows:
        image_type = str(row["image_type"])
        if image_type not in {"BOA_MOSAIC", "QAI_MOSAIC"}:
            continue
        paths_by_item.setdefault(str(row["item_id"]), {}).setdefault(image_type, []).append(
            Path(str(row["local_path"])).expanduser().resolve()
        )

    selected: list[ForceMonthlyScene] = []
    skipped: dict[str, tuple[str, ...]] = {}
    for row in scene_rows:
        item_id = str(row["item_id"])
        acquired = _parse_datetime(row["acquisition_time"])
        if not start <= acquired < end:
            continue
        reasons: list[str] = []
        if str(row.get("force_status") or "") != "complete":
            reasons.append(f"force-status:{row.get('force_status')}")
        item_paths = paths_by_item.get(item_id, {})
        boa_paths = item_paths.get("BOA_MOSAIC", [])
        qai_paths = item_paths.get("QAI_MOSAIC", [])
        if len(boa_paths) != 1:
            reasons.append(f"boa-mosaic-count:{len(boa_paths)}")
        if len(qai_paths) != 1:
            reasons.append(f"qai-mosaic-count:{len(qai_paths)}")
        if len(boa_paths) == 1 and not boa_paths[0].is_file():
            reasons.append("boa-mosaic-missing")
        if len(qai_paths) == 1 and not qai_paths[0].is_file():
            reasons.append("qai-mosaic-missing")
        if reasons:
            skipped[item_id] = tuple(reasons)
            continue
        selected.append(
            ForceMonthlyScene(
                item_id=item_id,
                acquisition_time=acquired,
                platform=str(row.get("platform") or "unknown"),
                cloud_cover=(
                    None if row.get("cloud_cover") is None else float(row["cloud_cover"])
                ),
                boa_path=boa_paths[0],
                qai_path=qai_paths[0],
            )
        )
    selected.sort(key=lambda scene: (scene.acquisition_time, scene.item_id))
    return ForceMonthlyDiscoveryResult(
        year=year,
        month=month,
        database_path=database_path,
        scenes=tuple(selected),
        skipped_items=skipped,
    )


@dataclass(frozen=True)
class MonthlyCompositeConfig:
    """Configuration for a post-FORCE monthly national composite."""

    force_db: Path
    output_root: Path
    year: int
    month: int
    bbox: tuple[float, float, float, float] = field(
        default_factory=lambda: tuple(SWITZERLAND_BBOX)
    )
    cutline_path: Path | None = None
    target_crs: str = "EPSG:2056"
    resolution: float = 10.0
    qai_mask: int = DEFAULT_FORCE_QAI_MASK
    compression: str = "DEFLATE"
    nodata: float = DEFAULT_VEGETATION_NODATA
    warp_memory_mib: int = 256
    max_output_gib: float = 64.0
    overwrite: bool = False
    dry_run: bool = False
    allow_incomplete_month: bool = False

    def __post_init__(self) -> None:
        calendar_month_window(self.year, self.month)
        object.__setattr__(self, "force_db", Path(self.force_db).expanduser())
        object.__setattr__(self, "output_root", Path(self.output_root).expanduser())
        object.__setattr__(self, "bbox", _validate_bbox(self.bbox))
        if self.cutline_path is not None:
            object.__setattr__(
                self, "cutline_path", Path(self.cutline_path).expanduser().resolve()
            )
        if self.resolution <= 0:
            raise ValueError("resolution must be positive")
        if not 0 <= self.qai_mask <= 0xFFFF:
            raise ValueError("qai_mask must fit an unsigned 16-bit integer")
        if self.warp_memory_mib < 16:
            raise ValueError("warp_memory_mib must be at least 16")
        if self.max_output_gib <= 0:
            raise ValueError("max_output_gib must be positive")
        if not math.isfinite(self.nodata):
            raise ValueError("nodata must be finite")

    @property
    def month_directory(self) -> Path:
        return self.output_root / f"year={self.year:04d}" / f"month={self.month:02d}"

    @property
    def stem(self) -> str:
        resolution = f"{self.resolution:g}m".replace(".", "p")
        return f"switzerland_force_boa_{self.year:04d}_{self.month:02d}_{resolution}"


@dataclass(frozen=True)
class MonthlyCompositeResult:
    """Post-FORCE monthly outputs and measured processing time."""

    reflectance_path: Path
    indices_path: Path
    manifest_path: Path
    stac_item_path: Path
    width: int
    height: int
    scene_count: int
    skipped_scene_count: int
    estimated_uncompressed_bytes: int
    timings_seconds: dict[str, float]
    dry_run: bool

    @property
    def elapsed_seconds(self) -> float:
        return self.timings_seconds.get("total", 0.0)


class MonthlyCompositeProcessor:
    """Create a QAI-screened monthly median from native FORCE BOA mosaics."""

    def __init__(self, config: MonthlyCompositeConfig) -> None:
        self.config = config
        self._gdal = (
            {}
            if config.dry_run
            else locate_gdal(
                "gdal_calc.py",
                "gdal_translate",
                "gdalbuildvrt",
                "gdalwarp",
                "gdalinfo",
                "gdaltransform",
            )
        )
        self._envelopes: dict[str, tuple[float, float, float, float]] = {}
        self._outside_bbox: list[str] = []

    @staticmethod
    def _band_domain(band: Mapping[str, Any]) -> str:
        metadata = band.get("metadata") or {}
        force = metadata.get("FORCE") or metadata.get("force") or {}
        return str(force.get("Domain") or force.get("DOMAIN") or "").upper()

    def _validate_scene(self, scene: ForceMonthlyScene) -> dict[str, Any]:
        boa = inspect_raster(self._gdal["gdalinfo"], scene.boa_path)
        qai = inspect_raster(self._gdal["gdalinfo"], scene.qai_path)
        boa_bands = boa.get("bands") or []
        qai_bands = qai.get("bands") or []
        if len(boa_bands) != len(FORCE_BOA_DOMAINS):
            raise ValueError(f"FORCE BOA must have ten bands: {scene.boa_path}")
        descriptions = tuple(
            str(band.get("description") or "").upper() for band in boa_bands
        )
        domains = tuple(self._band_domain(band) for band in boa_bands)
        if descriptions != FORCE_BOA_DOMAINS and domains != FORCE_BOA_DOMAINS:
            raise ValueError(f"FORCE BOA band order cannot be verified: {scene.boa_path}")
        if len(qai_bands) != 1:
            raise ValueError(f"FORCE QAI must have one band: {scene.qai_path}")
        if boa.get("size") != qai.get("size"):
            raise ValueError(f"FORCE BOA/QAI sizes differ for {scene.item_id}")
        if boa.get("geoTransform") != qai.get("geoTransform"):
            raise ValueError(f"FORCE BOA/QAI grids differ for {scene.item_id}")
        return boa

    def _windowed_sources(
        self, scene: ForceMonthlyScene, boa: Mapping[str, Any], directory: Path
    ) -> tuple[Path, Path] | None:
        """Cut the BOA/QAI pair down to the bbox before any pixel is read.

        A FORCE acquisition covers a whole Sentinel-2 granule, so screening it
        unclipped writes and medians about 110 x 110 km per scene even for a
        patch a few kilometres wide.  The window is taken on the native FORCE
        grid, so values are unchanged.  Returns None when the scene misses the
        bbox entirely.
        """

        wkt = str((boa.get("coordinateSystem") or {}).get("wkt") or "")
        if not wkt:
            raise ValueError(f"FORCE BOA has no coordinate system: {scene.boa_path}")
        if wkt not in self._envelopes:
            self._envelopes[wkt] = bbox_envelope(
                self._gdal["gdaltransform"], self.config.bbox, wkt
            )
        window = pixel_window(
            boa, self._envelopes[wkt], buffer_pixels=_WINDOW_BUFFER_PIXELS
        )
        if window is None:
            return None
        width, height = (int(value) for value in boa["size"])
        if window == (0, 0, width, height):
            return scene.boa_path, scene.qai_path
        directory.mkdir(parents=True, exist_ok=True)
        windowed: list[Path] = []
        for source, name in ((scene.boa_path, "boa.vrt"), (scene.qai_path, "qai.vrt")):
            output = directory / name
            run_gdal(
                (
                    self._gdal["gdal_translate"],
                    "-of",
                    "VRT",
                    "-srcwin",
                    *(str(value) for value in window),
                    str(source),
                    str(output),
                )
            )
            windowed.append(output)
        return windowed[0], windowed[1]

    def _scene_reflectance(self, scene: ForceMonthlyScene, output: Path) -> bool:
        """Write one QAI-screened scene; False when it does not reach the bbox."""

        boa = self._validate_scene(scene)
        sources = self._windowed_sources(scene, boa, output.with_suffix(""))
        if sources is None:
            return False
        boa_path, qai_path = sources
        command = [self._gdal["gdal_calc.py"]]
        variables: dict[str, str] = {}
        for band_name, variable in zip(
            MONTHLY_BAND_NAMES, _VARIABLES[: len(MONTHLY_BAND_NAMES)], strict=True
        ):
            variables[band_name] = variable
            band_number = FORCE_REFLECTANCE_BAND_MAP[band_name][1]
            command.extend(
                (f"-{variable}", str(boa_path), f"--{variable}_band", str(band_number))
            )
        command.extend(("-Q", str(qai_path), "--Q_band", "1"))
        valid_qai = f"bitwise_and(Q,{self.config.qai_mask})==0"
        for band_name in MONTHLY_BAND_NAMES:
            variable = variables[band_name]
            valid = f"logical_and({valid_qai},{variable}!={FORCE_BOA_NODATA})"
            command.extend(
                (
                    "--calc",
                    f"where({valid},{variable}*{FORCE_BOA_SCALE!r},{self.config.nodata:g})",
                )
            )
        command.extend(
            (
                "--outfile",
                str(output),
                "--NoDataValue",
                str(self.config.nodata),
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
        return True

    def _monthly_median(self, inputs: Sequence[Path], output: Path) -> None:
        command = [self._gdal["gdal_calc.py"]]
        multiple = len(inputs) > 1
        for band, variable in enumerate(_VARIABLES[: len(MONTHLY_BAND_NAMES)], start=1):
            command.extend((f"-{variable}", *(str(path) for path in inputs)))
            command.extend((f"--{variable}_band", str(band)))
            if multiple:
                values = f"where({variable}=={self.config.nodata:g},nan,{variable})"
                expression = (
                    f"nan_to_num(nanmedian({values},axis=0),nan={self.config.nodata:g})"
                )
            else:
                expression = (
                    f"where({variable}=={self.config.nodata:g},"
                    f"{self.config.nodata:g},{variable})"
                )
            command.extend(("--calc", expression))
        command.extend(
            (
                "--outfile",
                str(output),
                "--NoDataValue",
                str(self.config.nodata),
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
                "--extent",
                "union",
                "--projectionCheck",
                "--overwrite",
                "--quiet",
            )
        )
        run_gdal(command)

    def _country_vrt(self, source: Path, output: Path) -> None:
        west, south, east, north = self.config.bbox
        command = [
            self._gdal["gdalwarp"],
            "-overwrite",
            "-of",
            "VRT",
            "-t_srs",
            self.config.target_crs,
            "-te_srs",
            "EPSG:4326",
            "-te",
            str(west),
            str(south),
            str(east),
            str(north),
            "-tr",
            str(self.config.resolution),
            str(self.config.resolution),
            "-tap",
            "-r",
            "bilinear",
            "-srcnodata",
            str(self.config.nodata),
            "-dstnodata",
            str(self.config.nodata),
            "-wm",
            str(self.config.warp_memory_mib),
            "-multi",
        ]
        if self.config.cutline_path is not None:
            command.extend(("-cutline", str(self.config.cutline_path), "-crop_to_cutline"))
        command.extend((str(source), str(output)))
        run_gdal(command)
        set_vrt_band_descriptions(output, MONTHLY_BAND_NAMES)

    def _estimated_bytes(self, width: int, height: int) -> int:
        return (
            width
            * height
            * (len(MONTHLY_BAND_NAMES) + len(MONTHLY_INDEX_NAMES))
            * _FLOAT32_BYTES
        )

    def _manifest(
        self,
        *,
        discovery: ForceMonthlyDiscoveryResult,
        reflectance_path: Path,
        indices_path: Path,
        stac_item_path: Path,
        width: int,
        height: int,
        estimated_bytes: int,
        timings: Mapping[str, float],
    ) -> dict[str, Any]:
        start, end = calendar_month_window(self.config.year, self.config.month)
        cloud_values = [
            scene.cloud_cover for scene in discovery.scenes if scene.cloud_cover is not None
        ]
        return {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "processing_option": "post-force-monthly-composite",
            "source": "native-force-level2-boa-qai",
            "source_is_swisseo": False,
            "source_is_treeai": False,
            "formula_reference": "https://www.treeai.earth/swiss-vegetation-condition",
            "year": self.config.year,
            "month": self.config.month,
            "start_datetime": _iso_z(start),
            "end_datetime_exclusive": _iso_z(end),
            "month_complete_at_processing": datetime.now(timezone.utc) >= end,
            "allow_incomplete_month": self.config.allow_incomplete_month,
            "force_database": str(Path(self.config.force_db).resolve()),
            "bbox_wgs84": list(self.config.bbox),
            "cutline_path": (
                None if self.config.cutline_path is None else str(self.config.cutline_path)
            ),
            "target_crs": self.config.target_crs,
            "resolution": self.config.resolution,
            "composite_method": "QAI-screened per-pixel median of native FORCE BOA",
            "input_location": "local native FORCE BOA/QAI products",
            "timing_scope": {
                "monthly_mosaic_generation": (
                    "local QAI screening, temporal median, national clip and reflectance COG "
                    "writing; L1C retrieval and FORCE L2PS excluded"
                ),
                "index_calculation": "combined NDVI/GNDVI/NDRE/NDWI calculation and COG writing",
                "total": (
                    "FORCE product selection plus both measured downstream stages; L1C retrieval "
                    "and FORCE L2PS excluded"
                ),
            },
            "force_boa_scale": FORCE_BOA_SCALE,
            "force_boa_band_domains": list(FORCE_BOA_DOMAINS),
            "reflectance_band_mapping": {
                name: {"force_domain": domain, "force_band": band}
                for name, (domain, band) in FORCE_REFLECTANCE_BAND_MAP.items()
            },
            "reflectance_band_order": list(MONTHLY_BAND_NAMES),
            "index_band_order": list(MONTHLY_INDEX_NAMES),
            "index_definitions": VEGETATION_INDEX_DEFINITIONS,
            "quality_policy": {
                "force_qai_invalid_bit_mask_decimal": self.config.qai_mask,
                "force_qai_invalid_bit_mask_hex": f"0x{self.config.qai_mask:04X}",
                "default_0x031F_screens": [
                    "nodata",
                    "non-clear cloud state",
                    "cloud shadow",
                    "snow",
                    "subzero reflectance",
                    "saturated reflectance",
                ],
            },
            "nodata": self.config.nodata,
            "width": width,
            "height": height,
            "estimated_uncompressed_bytes": estimated_bytes,
            "dry_run": self.config.dry_run,
            "scene_count": len(discovery.scenes),
            "scenes_outside_bbox": list(self._outside_bbox),
            "average_catalog_cloud_cover_pct": (
                None if not cloud_values else sum(cloud_values) / len(cloud_values)
            ),
            "skipped_items": {
                key: list(value) for key, value in discovery.skipped_items.items()
            },
            "sources": discovery.to_dict()["scenes"],
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
        path: Path,
        reflectance_path: Path,
        indices_path: Path,
        manifest_path: Path,
        scene_count: int,
        elapsed_seconds: float,
    ) -> None:
        start, end = calendar_month_window(self.config.year, self.config.month)
        west, south, east, north = self.config.bbox
        _write_json_atomic(
            path,
            {
                "stac_version": "1.0.0",
                "type": "Feature",
                "id": self.config.stem,
                "bbox": [west, south, east, north],
                "geometry": {
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
                },
                "properties": {
                    "datetime": None,
                    "start_datetime": _iso_z(start),
                    "end_datetime": _iso_z(end - timedelta(microseconds=1)),
                    "gsd": self.config.resolution,
                    "terravault:source": "native-force-level2-boa-qai",
                    "terravault:composite_method": "qai-screened-per-pixel-median",
                    "terravault:source_scene_count": scene_count,
                    "terravault:elapsed_seconds": round(elapsed_seconds, 3),
                    "terravault:provisional": datetime.now(timezone.utc) < end,
                },
                "links": [],
                "assets": {
                    "reflectance": {
                        "href": reflectance_path.name,
                        "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                        "roles": ["data"],
                        "terravault:band_order": list(MONTHLY_BAND_NAMES),
                    },
                    "vegetation_indices": {
                        "href": indices_path.name,
                        "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                        "roles": ["data"],
                        "terravault:band_order": list(MONTHLY_INDEX_NAMES),
                    },
                    "manifest": {
                        "href": manifest_path.name,
                        "type": "application/json",
                        "roles": ["metadata"],
                    },
                },
            },
        )

    def run(self) -> MonthlyCompositeResult:
        """Create or plan one post-FORCE monthly vegetation product."""

        started = time.perf_counter()
        timings: dict[str, float] = {}
        _, month_end = calendar_month_window(self.config.year, self.config.month)
        if month_end > datetime.now(timezone.utc) and not self.config.allow_incomplete_month:
            raise ValueError(
                f"{self.config.year:04d}-{self.config.month:02d} is not complete yet; "
                "wait for the next month or pass --allow-incomplete-month"
            )
        if self.config.cutline_path is not None and not self.config.cutline_path.is_file():
            raise FileNotFoundError(f"Cutline GeoJSON does not exist: {self.config.cutline_path}")

        stage = time.perf_counter()
        discovery = discover_force_month(
            year=self.config.year,
            month=self.config.month,
            force_db=self.config.force_db,
        )
        timings["source_selection"] = time.perf_counter() - stage
        if not discovery.scenes:
            raise FileNotFoundError(
                "No complete paired native FORCE BOA/QAI mosaics were found for "
                f"{self.config.year:04d}-{self.config.month:02d}"
            )

        output_directory = self.config.month_directory.resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        reflectance_path = output_directory / f"{self.config.stem}_reflectance.tif"
        indices_path = output_directory / f"{self.config.stem}_indices.tif"
        manifest_path = output_directory / f"{self.config.stem}.manifest.json"
        stac_item_path = output_directory / f"{self.config.stem}.stac-item.json"
        existing = [path for path in (reflectance_path, indices_path) if path.exists()]
        if existing and not self.config.overwrite and not self.config.dry_run:
            raise FileExistsError(
                "Monthly output already exists; pass --overwrite: "
                + ", ".join(str(path) for path in existing)
            )

        width, height = _approximate_dimensions(self.config.bbox, self.config.resolution)
        estimated_bytes = self._estimated_bytes(width, height)
        if estimated_bytes / (1024**3) > self.config.max_output_gib:
            raise ValueError(
                f"Estimated uncompressed outputs are {estimated_bytes / (1024**3):.2f} GiB, "
                f"above the {self.config.max_output_gib:.2f} GiB safety limit"
            )

        if self.config.dry_run:
            timings["monthly_mosaic_generation"] = 0.0
            timings["index_calculation"] = 0.0
            timings["total"] = time.perf_counter() - started
            _write_json_atomic(
                manifest_path,
                self._manifest(
                    discovery=discovery,
                    reflectance_path=reflectance_path,
                    indices_path=indices_path,
                    stac_item_path=stac_item_path,
                    width=width,
                    height=height,
                    estimated_bytes=estimated_bytes,
                    timings=timings,
                ),
            )
            return MonthlyCompositeResult(
                reflectance_path=reflectance_path,
                indices_path=indices_path,
                manifest_path=manifest_path,
                stac_item_path=stac_item_path,
                width=width,
                height=height,
                scene_count=len(discovery.scenes),
                skipped_scene_count=len(discovery.skipped_items),
                estimated_uncompressed_bytes=estimated_bytes,
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
                prefix=f"terravault-force-monthly-{self.config.year:04d}-{self.config.month:02d}-",
                dir=output_directory,
            ) as temporary:
                temporary_path = Path(temporary)
                stage = time.perf_counter()

                def screen(numbered: tuple[int, ForceMonthlyScene]) -> Path | None:
                    number, scene = numbered
                    output = temporary_path / f"scene-{number:04d}.tif"
                    logger.info(
                        "Screening native FORCE BOA – item=%s acquisition=%s",
                        scene.item_id,
                        _iso_z(scene.acquisition_time),
                    )
                    return output if self._scene_reflectance(scene, output) else None

                with ThreadPoolExecutor(max_workers=_SCENE_WORKERS) as pool:
                    screened = list(pool.map(screen, enumerate(discovery.scenes, start=1)))
                scene_outputs = [output for output in screened if output is not None]
                for scene, output in zip(discovery.scenes, screened, strict=True):
                    if output is None:
                        logger.info("Skipping %s: it does not overlap the bbox", scene.item_id)
                        self._outside_bbox.append(scene.item_id)
                timings["force_qai_screening"] = time.perf_counter() - stage
                if not scene_outputs:
                    raise FileNotFoundError(
                        "No completed native FORCE acquisition overlaps the bbox for "
                        f"{self.config.year:04d}-{self.config.month:02d}"
                    )

                stage = time.perf_counter()
                median = temporary_path / "force-monthly-median.tif"
                self._monthly_median(scene_outputs, median)
                timings["monthly_compositing"] = time.perf_counter() - stage

                stage = time.perf_counter()
                country = temporary_path / "switzerland.vrt"
                self._country_vrt(median, country)
                country_info = inspect_raster(self._gdal["gdalinfo"], country)
                width, height = (int(value) for value in country_info["size"])
                estimated_bytes = self._estimated_bytes(width, height)
                if estimated_bytes / (1024**3) > self.config.max_output_gib:
                    raise ValueError(
                        f"Exact uncompressed outputs are {estimated_bytes / (1024**3):.2f} GiB, "
                        f"above the {self.config.max_output_gib:.2f} GiB safety limit"
                    )
                write_described_cog(
                    source_path=country,
                    output_path=partial_reflectance,
                    band_names=MONTHLY_BAND_NAMES,
                    gdal=self._gdal,
                    compression=self.config.compression,
                    metadata={
                        "TERRAVAULT_SOURCE": "native-force-level2-boa-qai",
                        "TERRAVAULT_MONTH": f"{self.config.year:04d}-{self.config.month:02d}",
                        "TERRAVAULT_COMPOSITE": "qai-screened-per-pixel-median",
                    },
                )
                timings["national_clipping_and_cog"] = time.perf_counter() - stage

                stage = time.perf_counter()
                write_vegetation_indices(
                    reflectance_path=partial_reflectance,
                    output_path=partial_indices,
                    gdal=self._gdal,
                    nodata=self.config.nodata,
                    compression=self.config.compression,
                    metadata={
                        "TERRAVAULT_SOURCE": "native-force-level2-boa-qai",
                        "TERRAVAULT_INDICES": ",".join(MONTHLY_INDEX_NAMES),
                    },
                )
                timings["index_calculation"] = time.perf_counter() - stage
            os.replace(partial_reflectance, reflectance_path)
            os.replace(partial_indices, indices_path)
        finally:
            partial_reflectance.unlink(missing_ok=True)
            partial_indices.unlink(missing_ok=True)

        timings["monthly_mosaic_generation"] = sum(
            timings.get(key, 0.0)
            for key in (
                "force_qai_screening",
                "monthly_compositing",
                "national_clipping_and_cog",
            )
        )
        timings["total"] = time.perf_counter() - started
        _write_json_atomic(
            manifest_path,
            self._manifest(
                discovery=discovery,
                reflectance_path=reflectance_path,
                indices_path=indices_path,
                stac_item_path=stac_item_path,
                width=width,
                height=height,
                estimated_bytes=estimated_bytes,
                timings=timings,
            ),
        )
        self._write_stac_item(
            path=stac_item_path,
            reflectance_path=reflectance_path,
            indices_path=indices_path,
            manifest_path=manifest_path,
            scene_count=len(discovery.scenes),
            elapsed_seconds=timings["total"],
        )
        return MonthlyCompositeResult(
            reflectance_path=reflectance_path,
            indices_path=indices_path,
            manifest_path=manifest_path,
            stac_item_path=stac_item_path,
            width=width,
            height=height,
            scene_count=len(discovery.scenes),
            skipped_scene_count=len(discovery.skipped_items),
            estimated_uncompressed_bytes=estimated_bytes,
            timings_seconds=timings,
            dry_run=False,
        )
