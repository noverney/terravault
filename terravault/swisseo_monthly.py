"""Monthly composites from all published swissEO S2-SR orbit mosaics."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .catalog import SWITZERLAND_BBOX
from .monthly import calendar_month_window
from .swisseo import (
    SWISSEO_COLLECTION,
    SWISSEO_STAC_URL,
    SwissEOClient,
    SwissEOImageConfig,
    SwissEOImageProcessor,
    SwissEOProductVersion,
    discover_swisseo_month,
    swisseo_band_map,
    swisseo_product_version,
    swisseo_v200_asset,
)
from .vegetation import (
    DEFAULT_VEGETATION_NODATA,
    REFLECTANCE_BAND_NAMES,
    VEGETATION_INDEX_DEFINITIONS,
    VEGETATION_INDEX_NAMES,
    inspect_raster,
    locate_gdal,
    raster_extent,
    run_gdal,
    set_vrt_band_descriptions,
    write_described_cog,
    write_vegetation_indices,
    warp_to_roi_vrt,
)

_FLOAT32_BYTES = 4
_COPY_BUFFER_BYTES = 16 * 1024 * 1024
logger = logging.getLogger(__name__)

# This route ran entirely serially: one orbit mosaic aligned at a time, then six
# band medians one after another, measured at 86% of a twelve-core machine.
# Both loops are over independent work, so both are pooled. Item alignment is
# download-heavy and tolerates more concurrency than the medians, which each
# read every aligned item.
_ITEM_WORKERS = max(1, int(os.environ.get("TERRAVAULT_SWISSEO_ITEM_WORKERS", "5")))
_BAND_WORKERS = max(1, int(os.environ.get("TERRAVAULT_SWISSEO_BAND_WORKERS", "3")))

# Concurrent downloads make a transient read timeout from the publication host
# much more likely, and one unhandled timeout used to abort the whole month.
# Retry the transfer instead; the partial file is discarded first so a resumed
# attempt never appends to a truncated body.
_DOWNLOAD_ATTEMPTS = max(1, int(os.environ.get("TERRAVAULT_SWISSEO_DOWNLOAD_ATTEMPTS", "5")))
_DOWNLOAD_BACKOFF_SECONDS = 5.0
_DOWNLOAD_TIMEOUT = (30, 180)


def _validate_bbox(bbox: Sequence[float]) -> tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("bbox must contain four coordinates")
    west, south, east, north = (float(value) for value in bbox)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox must be valid WGS84 coordinates")
    return west, south, east, north


def _approximate_dimensions(bbox: Sequence[float], resolution: float) -> tuple[int, int]:
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


def _wait_for_output_root(
    output_root: Path,
    *,
    deadline: float,
    poll_seconds: float,
) -> None:
    warned = False
    while not output_root.is_dir():
        if time.monotonic() >= deadline:
            raise OSError(f"Output volume did not reconnect before timeout: {output_root}")
        if not warned:
            logger.warning(
                "Output volume is unavailable; waiting for reconnect: %s",
                output_root,
            )
            warned = True
        time.sleep(poll_seconds)


def _copy_file_sequential(
    source: Path,
    destination: Path,
    *,
    output_root: Path,
    wait_seconds: float,
    poll_seconds: float,
) -> None:
    """Publish locally staged bytes while tolerating an SMB reconnect."""

    deadline = time.monotonic() + wait_seconds
    while True:
        _wait_for_output_root(
            output_root,
            deadline=deadline,
            poll_seconds=poll_seconds,
        )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.unlink(missing_ok=True)
            with source.open("rb") as input_stream, destination.open("xb") as output_stream:
                shutil.copyfileobj(
                    input_stream,
                    output_stream,
                    length=_COPY_BUFFER_BYTES,
                )
                output_stream.flush()
                os.fsync(output_stream.fileno())
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise OSError(f"Could not publish staged raster to {destination}: {exc}") from exc
            logger.warning(
                "Output-volume write failed; retaining local stage and retrying: %s",
                exc,
            )
            time.sleep(poll_seconds)


def _replace_after_reconnect(
    source: Path,
    destination: Path,
    *,
    output_root: Path,
    wait_seconds: float,
    poll_seconds: float,
) -> None:
    deadline = time.monotonic() + wait_seconds
    while True:
        _wait_for_output_root(
            output_root,
            deadline=deadline,
            poll_seconds=poll_seconds,
        )
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise OSError(f"Could not finalize {destination}: {exc}") from exc
            logger.warning("Output-volume rename failed; waiting to retry: %s", exc)
            time.sleep(poll_seconds)


@dataclass(frozen=True)
class SwissEOMonthlyConfig:
    output_root: Path
    year: int
    month: int
    bbox: tuple[float, float, float, float] = field(default_factory=lambda: tuple(SWITZERLAND_BBOX))
    cutline_path: Path | None = None
    stac_url: str = SWISSEO_STAC_URL
    collection: str = SWISSEO_COLLECTION
    target_crs: str = "EPSG:2056"
    resolution: float = 20.0
    compression: str = "DEFLATE"
    nodata: float = DEFAULT_VEGETATION_NODATA
    warp_memory_mib: int = 256
    max_output_gib: float = 16.0
    max_temporary_gib: float = 128.0
    temporary_root: Path | None = None
    publication_wait_seconds: float = 21_600.0
    publication_poll_seconds: float = 30.0
    overwrite: bool = False
    dry_run: bool = False
    allow_incomplete_month: bool = False
    # v100 stays the default so existing runs, filenames and outputs are unchanged.
    product_version: str = "v100"

    def __post_init__(self) -> None:
        calendar_month_window(self.year, self.month)
        version = swisseo_product_version(self.product_version)
        if version.name != "v100":
            # The quality rule and scaling belong to one collection; never let a
            # v200 rule silently run against v100 data or the reverse.
            if self.collection == SWISSEO_COLLECTION:
                object.__setattr__(self, "collection", version.collection)
            elif self.collection != version.collection:
                raise ValueError(
                    f"swissEO {version.name} reads {version.collection}, "
                    f"not {self.collection}"
                )
        object.__setattr__(self, "output_root", Path(self.output_root).expanduser())
        object.__setattr__(self, "bbox", _validate_bbox(self.bbox))
        if self.cutline_path is not None:
            object.__setattr__(self, "cutline_path", Path(self.cutline_path).expanduser().resolve())
        if self.temporary_root is not None:
            object.__setattr__(
                self,
                "temporary_root",
                Path(self.temporary_root).expanduser().resolve(),
            )
        if self.resolution <= 0:
            raise ValueError("resolution must be positive")
        if self.warp_memory_mib < 16:
            raise ValueError("warp_memory_mib must be at least 16")
        if self.max_output_gib <= 0:
            raise ValueError("max_output_gib must be positive")
        if self.max_temporary_gib <= 0:
            raise ValueError("max_temporary_gib must be positive")
        if self.publication_wait_seconds < 0:
            raise ValueError("publication_wait_seconds must be non-negative")
        if self.publication_poll_seconds <= 0:
            raise ValueError("publication_poll_seconds must be positive")

    @property
    def month_directory(self) -> Path:
        return self.output_root / f"year={self.year:04d}" / f"month={self.month:02d}"

    @property
    def version(self) -> SwissEOProductVersion:
        return swisseo_product_version(self.product_version)

    @property
    def stem(self) -> str:
        resolution = f"{self.resolution:g}m".replace(".", "p")
        return (
            f"switzerland_swisseo_s2_sr{self.version.stem_tag}_"
            f"{self.year:04d}_{self.month:02d}_{resolution}"
        )


@dataclass(frozen=True)
class SwissEOMonthlyResult:
    reflectance_path: Path
    indices_path: Path
    manifest_path: Path
    width: int
    height: int
    item_count: int
    estimated_peak_temporary_bytes: int
    timings_seconds: dict[str, float]
    dry_run: bool

    @property
    def elapsed_seconds(self) -> float:
        return self.timings_seconds.get("total", 0.0)


class SwissEOMonthlyProcessor:
    """Combine every swissEO orbit mosaic in a calendar month by median."""

    def __init__(
        self,
        config: SwissEOMonthlyConfig,
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

    def _metadata_summary(self, metadata: Mapping[str, Any]) -> dict[str, Any]:
        if self.config.version.per_band_assets:
            # v200 reports the OmniCloudMask cloud share, not v100's masked share;
            # keep them under different names so they are never averaged together.
            properties = metadata.get("PROPERTIES") or {}
            granules = (metadata.get("SOURCE") or {}).get("GRANULES") or {}
            cloud = properties.get("CLOUDPERCENTAGE")
            return {
                "percent_cloud": None if cloud is None else float(cloud),
                "source_scene_count": len(granules) or None,
            }
        section = metadata.get("BANDS-10M") or {}
        properties = section.get("SOURCE_COLLECTION_PROPERTIES") or {}
        return {
            "percent_data": properties.get("percent_data"),
            "percent_masked": properties.get("percent_masked"),
            "orbit_number": properties.get("SENSING_ORBIT_NUMBER"),
            "source_scene_count": properties.get("scene_count"),
        }

    def _download_asset(self, *, href: str, output: Path) -> Path:
        """Stage one swissEO asset into the local temporary volume once.

        Streaming remote COGs through ``/vsicurl/`` for every warp and median
        pass is slow and can return truncated tiles.  A single sequential copy
        to the temporary volume makes all later reads local and crash-proof.
        Local paths (used by tests and offline runs) are copied as-is.
        """

        import requests

        if output.exists() and output.stat().st_size > 0:
            logger.info("reusing downloaded swissEO asset %s", output.name)
            return output
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial")
        started = time.perf_counter()
        if href.startswith(("https://", "http://")):
            logger.info("downloading swissEO asset %s -> %s", href, output.name)
            for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
                try:
                    with requests.get(href, stream=True, timeout=_DOWNLOAD_TIMEOUT) as response:
                        response.raise_for_status()
                        with temporary.open("wb") as handle:
                            for chunk in response.iter_content(chunk_size=_COPY_BUFFER_BYTES):
                                if chunk:
                                    handle.write(chunk)
                    break
                except (requests.RequestException, OSError) as error:
                    temporary.unlink(missing_ok=True)
                    if attempt == _DOWNLOAD_ATTEMPTS:
                        raise
                    delay = _DOWNLOAD_BACKOFF_SECONDS * attempt
                    logger.warning(
                        "download of %s failed (attempt %d/%d: %s); retrying in %.0fs",
                        output.name, attempt, _DOWNLOAD_ATTEMPTS, error, delay,
                    )
                    time.sleep(delay)
        else:
            logger.info("staging local swissEO asset %s -> %s", href, output.name)
            shutil.copyfile(href, temporary)
        os.replace(temporary, output)
        logger.info(
            "staged %s (%.1f MiB in %.1fs)",
            output.name,
            output.stat().st_size / 1024**2,
            time.perf_counter() - started,
        )
        return output

    def _readable_raster(self, path: Path, *, bands: int | None = None) -> bool:
        """Report whether a checkpointed raster survived an interrupted run.

        A crash can leave a half-written GeoTIFF behind.  Ask GDAL to open it
        rather than trusting the file's existence, and delete it when it does
        not open so the caller simply rebuilds it.
        """

        if not path.is_file() or path.stat().st_size == 0:
            return False
        try:
            info = inspect_raster(self._gdal["gdalinfo"], path)
        except Exception:
            path.unlink(missing_ok=True)
            return False
        if bands is not None and len(info.get("bands", ())) != bands:
            path.unlink(missing_ok=True)
            return False
        return True

    def _reuse_aligned_item(self, directory: Path) -> dict[str, Path] | None:
        """Return an earlier run's aligned band map when it is still complete."""

        marker = directory / "aligned.json"
        if not marker.is_file():
            return None
        try:
            names = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(names, dict) or not names:
            return None
        aligned = {name: directory / relative for name, relative in names.items()}
        if all(path.is_file() and path.stat().st_size > 0 for path in aligned.values()):
            return aligned
        return None

    def _aligned_item_bands(
        self,
        *,
        item: Mapping[str, Any],
        metadata: Mapping[str, Any],
        directory: Path,
    ) -> dict[str, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        child = SwissEOImageProcessor(
            SwissEOImageConfig(
                item_id=str(item["id"]),
                output_root=directory,
                stac_url=self.config.stac_url,
                collection=self.config.collection,
                resolution=self.config.resolution,
                target_crs=self.config.target_crs,
                reflectance_scale=self.config.version.reflectance_scale,
                source_nodata=self.config.version.reflectance_nodata,
                output_nodata=self.config.nodata,
                compression=self.config.compression,
                warp_memory_mib=self.config.warp_memory_mib,
            ),
            client=self.client,
        )
        version = self.config.version
        local_assets: dict[str, Path] = {}
        downloads: list[Path] = []
        if version.per_band_assets:
            # v200 ships one file per band.  Stack them locally into the v100
            # layout (10 m bands, 20 m bands, terrain+cloud masks) so the warps,
            # median and index code below stay shared between versions.  The
            # per-band grids within one orbit are pixel-aligned, so a
            # -separate VRT involves no resampling.
            def fetch(name: str) -> Path:
                local = directory / f"source-{name}.tif"
                self._download_asset(
                    href=str(swisseo_v200_asset(item, name)["href"]), output=local
                )
                downloads.append(local)
                return local

            stacks = {
                "BANDS-20M": [fetch(f"{band.casefold()}_20m") for band in version.bands_20m],
                "BANDS-10M": [fetch(f"{band.casefold()}_10m") for band in version.bands_10m],
                "MASKS-10M": [fetch("terrainmask_10m"), fetch("cloudmask_10m")],
            }
            for title, sources in stacks.items():
                stack = directory / f"source-{title.casefold()}.vrt"
                run_gdal(
                    (
                        self._gdal["gdalbuildvrt"],
                        "-overwrite",
                        "-separate",
                        str(stack),
                        *(str(source) for source in sources),
                    )
                )
                local_assets[title] = stack
            band_map = {
                **{name: ("BANDS-10M", number) for number, name in enumerate(version.bands_10m, 1)},
                **{name: ("BANDS-20M", number) for number, name in enumerate(version.bands_20m, 1)},
            }
        else:
            assets = child._source_assets(item)
            for title in ("BANDS-20M", "BANDS-10M", "MASKS-10M"):
                local = directory / f"source-{title.casefold()}.tif"
                self._download_asset(href=str(assets[title]["href"]), output=local)
                local_assets[title] = local
                downloads.append(local)
            band_map = swisseo_band_map(metadata)
        warped: dict[str, Path] = {}
        # Compress the aligned subsets aggressively; uncompressed Float32/UInt16
        # national subsets for ten orbits would exceed a typical laptop disk.
        subset_creation = (f"COMPRESS={self.config.compression}", "BIGTIFF=YES", "TILED=YES")
        bands20 = directory / "bands-20m.tif"
        child._warp_asset(
            href=str(local_assets["BANDS-20M"]),
            output=bands20,
            resampling="bilinear",
            source_nodata=version.reflectance_nodata,
            bbox_wgs84=self.config.bbox,
            cutline_path=self.config.cutline_path,
            output_format="GTiff",
            creation_options=subset_creation,
        )
        warped["BANDS-20M"] = bands20
        extent = raster_extent(inspect_raster(self._gdal["gdalinfo"], bands20))
        for title, resampling, nodata in (
            ("BANDS-10M", "bilinear", version.reflectance_nodata),
            ("MASKS-10M", "max", version.mask_nodata),
        ):
            output = directory / f"{title.casefold()}.tif"
            child._warp_asset(
                href=str(local_assets[title]),
                output=output,
                resampling=resampling,
                source_nodata=nodata,
                extent=extent,
                cutline_path=self.config.cutline_path,
                output_format="GTiff",
                creation_options=subset_creation,
            )
            warped[title] = output
        # The aligned GTiff subsets are self-contained; the much larger raw
        # orbit downloads are no longer needed once every warp has read them.
        # Removing them here bounds peak temporary storage to roughly one
        # raw orbit plus the accumulated aligned subsets.
        for source in (*downloads, *local_assets.values()):
            source.unlink(missing_ok=True)
        single_bands: dict[str, Path] = {}
        for band_name, (asset_title, band_number) in band_map.items():
            output = directory / f"{band_name.casefold()}.vrt"
            run_gdal(
                (
                    self._gdal["gdal_translate"],
                    "-of",
                    "VRT",
                    "-b",
                    str(band_number),
                    str(warped[asset_title]),
                    str(output),
                )
            )
            single_bands[band_name] = output
        for name, band_number in (("TERRAIN_MASK", 1), ("CLOUD_MASK", 2)):
            output = directory / f"{name.casefold()}.vrt"
            run_gdal(
                (
                    self._gdal["gdal_translate"],
                    "-of",
                    "VRT",
                    "-b",
                    str(band_number),
                    str(warped["MASKS-10M"]),
                    str(output),
                )
            )
            single_bands[name] = output
        # The VRTs reference the warped GTiffs beside them, so the whole item
        # directory is self-contained and reusable by a later run.  Record the
        # band map only after every file exists.
        _write_json_atomic(
            directory / "aligned.json",
            {name: path.name for name, path in single_bands.items()},
        )
        return single_bands

    def _monthly_reflectance_median(
        self,
        *,
        aligned_items: Sequence[Mapping[str, Path]],
        output: Path,
    ) -> None:
        """Calculate six independent masked medians and stack them in band order.

        A multiband ``gdal_calc`` command rereads every supplied input once per
        output expression.  Running one calculation per spectral band limits
        each pass to that band and the two shared masks.  Inputs are local
        aligned subsets, so no remote COG range reads occur in this stage.
        """

        if not aligned_items:
            raise ValueError("At least one aligned swissEO item is required")
        multiple = len(aligned_items) > 1
        version = self.config.version
        mask_parts = list(version.valid_mask_terms)
        band_outputs: list[Path] = []
        band_commands: list[list[str]] = []
        for band_number, band_name in enumerate(REFLECTANCE_BAND_NAMES, start=1):
            band_output = output.with_name(
                f"{output.stem}.band-{band_number:02d}-{band_name.casefold()}.tif"
            )
            band_outputs.append(band_output)
            command = [self._gdal["gdal_calc.py"]]
            command.extend(
                (
                    "-A",
                    *(str(item[band_name]) for item in aligned_items),
                    "--A_band",
                    "1",
                )
            )
            command.extend(
                (
                    "-T",
                    *(str(item["TERRAIN_MASK"]) for item in aligned_items),
                    "--T_band",
                    "1",
                    "-Q",
                    *(str(item["CLOUD_MASK"]) for item in aligned_items),
                    "--Q_band",
                    "1",
                )
            )
            valid = (
                "logical_and.reduce(("
                + ",".join([f"A!={version.reflectance_nodata}", *mask_parts])
                + "))"
            )
            physical = version.physical_reflectance("A")
            if multiple:
                expression = (
                    f"nan_to_num(nanmedian(where({valid},{physical},nan),axis=0),"
                    f"nan={self.config.nodata:g})"
                )
            else:
                expression = f"where({valid},{physical},{self.config.nodata:g})"
            command.extend(
                (
                    "--calc",
                    expression,
                    "--outfile",
                    str(band_output),
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
                    "--co",
                    "INTERLEAVE=BAND",
                    "--hideNoData",
                    "--extent",
                    "union",
                    "--projectionCheck",
                    "--overwrite",
                    "--quiet",
                )
            )
            band_commands.append(command)

        # Each band median is independent; they only share the aligned inputs,
        # which are read-only by this point.
        logger.info(
            "computing %d band medians with %d workers", len(band_commands), _BAND_WORKERS
        )
        with ThreadPoolExecutor(max_workers=_BAND_WORKERS) as pool:
            for _ in pool.map(run_gdal, band_commands):
                pass

        stack_vrt = output.with_name(f"{output.stem}.stack.vrt")
        try:
            run_gdal(
                (
                    self._gdal["gdalbuildvrt"],
                    "-overwrite",
                    "-separate",
                    "-srcnodata",
                    str(self.config.nodata),
                    "-vrtnodata",
                    str(self.config.nodata),
                    str(stack_vrt),
                    *(str(path) for path in band_outputs),
                )
            )
            set_vrt_band_descriptions(stack_vrt, REFLECTANCE_BAND_NAMES)
            run_gdal(
                (
                    self._gdal["gdal_translate"],
                    "-of",
                    "GTiff",
                    "-ot",
                    "Float32",
                    "-a_nodata",
                    str(self.config.nodata),
                    "-co",
                    f"COMPRESS={self.config.compression}",
                    "-co",
                    "BIGTIFF=YES",
                    "-co",
                    "TILED=YES",
                    "-co",
                    "INTERLEAVE=BAND",
                    str(stack_vrt),
                    str(output),
                )
            )
        finally:
            stack_vrt.unlink(missing_ok=True)
            for band_output in band_outputs:
                band_output.unlink(missing_ok=True)

    def _manifest(
        self,
        *,
        items: Sequence[Mapping[str, Any]],
        summaries: Mapping[str, Mapping[str, Any]],
        reflectance: Path,
        indices: Path,
        width: int,
        height: int,
        estimated_peak_temporary_bytes: int,
        timings: Mapping[str, float],
    ) -> dict[str, Any]:
        masked = [
            float(summary["percent_masked"])
            for summary in summaries.values()
            if summary.get("percent_masked") is not None
        ]
        cloud = [
            float(summary["percent_cloud"])
            for summary in summaries.values()
            if summary.get("percent_cloud") is not None
        ]
        version = self.config.version
        return {
            "schema_version": 1,
            "processing_option": "swisseo-monthly-composite",
            "source": "swissEO-S2-SR-published-orbit-mosaics",
            "source_is_force": False,
            "source_is_treeai": False,
            "formula_reference": "https://www.treeai.earth/swiss-vegetation-condition",
            "year": self.config.year,
            "month": self.config.month,
            "stac_url": self.client.stac_url,
            "collection": self.client.collection,
            "stac_authentication_required": False,
            "bbox_wgs84": list(self.config.bbox),
            "cutline_path": None
            if self.config.cutline_path is None
            else str(self.config.cutline_path),
            "target_crs": self.config.target_crs,
            "resolution": self.config.resolution,
            "composite_method": "per-pixel median of all quality-masked swissEO orbit mosaics",
            "input_location": (
                "public swissEO COG assets downloaded once to the local temporary "
                "volume, warped to aligned national subsets, then removed"
            ),
            "timing_scope": {
                "monthly_mosaic_generation": (
                    "remote COG reads, alignment, six band-specific masked orbit-mosaic "
                    "medians, band stacking, national clip and reflectance COG writing"
                ),
                "index_calculation": "combined NDVI/GNDVI/NDRE/NDWI calculation and COG writing",
                "total": "public STAC/metadata retrieval plus both measured processing stages",
            },
            "item_count": len(items),
            "average_source_percent_masked": None if not masked else sum(masked) / len(masked),
            "average_source_percent_cloud": None if not cloud else sum(cloud) / len(cloud),
            "product_version": version.name,
            "reflectance_scaling": {
                "scale": version.reflectance_scale,
                "offset": version.reflectance_offset,
                "nodata": version.reflectance_nodata,
            },
            "quality_rule": version.quality_rule,
            "reflectance_band_order": list(REFLECTANCE_BAND_NAMES),
            "index_band_order": list(VEGETATION_INDEX_NAMES),
            "index_definitions": VEGETATION_INDEX_DEFINITIONS,
            "sources": [
                {"item_id": item["id"], **dict(summaries[str(item["id"])])} for item in items
            ],
            "width": width,
            "height": height,
            "estimated_peak_temporary_bytes": estimated_peak_temporary_bytes,
            "temporary_root": (
                str(Path(tempfile.gettempdir()).resolve())
                if self.config.temporary_root is None
                else str(self.config.temporary_root)
            ),
            "temporary_strategy": (
                "each orbit mosaic is downloaded once, warped to compressed aligned "
                "national subset GTiffs on the temporary volume, then removed before the "
                "next orbit is fetched; six band-specific blockwise masked medians read "
                "only the local aligned subsets, and completed TIFFs are copied "
                "sequentially to the output volume so SMB is never used as a random-seek "
                "GeoTIFF work file; a disconnected output volume is retried while the "
                "completed local stage is retained"
            ),
            "publication_wait_seconds": self.config.publication_wait_seconds,
            "publication_poll_seconds": self.config.publication_poll_seconds,
            "dry_run": self.config.dry_run,
            "outputs": {"reflectance": str(reflectance), "indices": str(indices)},
            "timings_seconds": {key: round(float(value), 3) for key, value in timings.items()},
        }

    def run(self) -> SwissEOMonthlyResult:
        started = time.perf_counter()
        _, end = calendar_month_window(self.config.year, self.config.month)
        if end > datetime.now(timezone.utc) and not self.config.allow_incomplete_month:
            raise ValueError("Requested calendar month is incomplete")
        if self.config.cutline_path is not None and not self.config.cutline_path.is_file():
            raise FileNotFoundError(f"Cutline does not exist: {self.config.cutline_path}")
        stage = time.perf_counter()
        discovery = discover_swisseo_month(
            year=self.config.year,
            month=self.config.month,
            client=self.client,
        )
        if not discovery.items:
            raise FileNotFoundError("No swissEO orbit mosaics were published for this month")
        metadata = {str(item["id"]): self.client.get_metadata(item) for item in discovery.items}
        summaries = {
            item_id: self._metadata_summary(payload) for item_id, payload in metadata.items()
        }
        timings: dict[str, float] = {"stac_and_metadata": time.perf_counter() - stage}
        output_dir = self.config.month_directory.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        reflectance = output_dir / f"{self.config.stem}_reflectance.tif"
        indices = output_dir / f"{self.config.stem}_indices.tif"
        manifest = output_dir / f"{self.config.stem}.manifest.json"
        width, height = _approximate_dimensions(self.config.bbox, self.config.resolution)
        estimated = (
            width
            * height
            * (len(REFLECTANCE_BAND_NAMES) + len(VEGETATION_INDEX_NAMES))
            * _FLOAT32_BYTES
        )
        if estimated / (1024**3) > self.config.max_output_gib:
            raise ValueError("Estimated uncompressed output exceeds max_output_gib")
        # Peak temporary storage is bounded by the aligned per-orbit subsets
        # (six reflectance bands + two masks per item, Float32/UInt8 on the
        # target grid), the six band medians plus the four-index raster, and a
        # single in-flight raw orbit download that is removed after its warps.
        # Aligned subsets are written with DEFLATE; a 0.3 ratio is a
        # conservative planning factor for masked UInt16/UInt8 national subsets.
        _DEFLATE_RATIO = 0.3
        aligned_subset_bytes = int(
            width
            * height
            * (len(REFLECTANCE_BAND_NAMES) * _FLOAT32_BYTES + 2)
            * len(discovery.items)
            * _DEFLATE_RATIO
        )
        median_and_indices_bytes = (
            width
            * height
            * (len(REFLECTANCE_BAND_NAMES) + len(VEGETATION_INDEX_NAMES))
            * _FLOAT32_BYTES
        )
        # One raw orbit is held only while its warps run.  It scales with the
        # target grid (UInt16 reflectance + UInt8 masks over the same region),
        # so use the per-item aligned size as the in-flight download buffer.
        one_raw_orbit_bytes = width * height * (
            len(REFLECTANCE_BAND_NAMES) * 2 + 2
        )
        estimated_peak_temporary_bytes = (
            aligned_subset_bytes + median_and_indices_bytes + one_raw_orbit_bytes
        )
        if self.config.dry_run:
            timings["monthly_mosaic_generation"] = 0.0
            timings["index_calculation"] = 0.0
            timings["total"] = time.perf_counter() - started
            _write_json_atomic(
                manifest,
                self._manifest(
                    items=discovery.items,
                    summaries=summaries,
                    reflectance=reflectance,
                    indices=indices,
                    width=width,
                    height=height,
                    estimated_peak_temporary_bytes=estimated_peak_temporary_bytes,
                    timings=timings,
                ),
            )
            return SwissEOMonthlyResult(
                reflectance,
                indices,
                manifest,
                width,
                height,
                len(discovery.items),
                estimated_peak_temporary_bytes,
                timings,
                True,
            )
        if estimated_peak_temporary_bytes / (1024**3) > self.config.max_temporary_gib:
            raise ValueError(
                "Estimated peak temporary storage exceeds max_temporary_gib; "
                "choose a coarser resolution or a larger processing volume"
            )
        temporary_root = (
            Path(tempfile.gettempdir()).resolve()
            if self.config.temporary_root is None
            else self.config.temporary_root
        )
        temporary_root.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(temporary_root).free
        if estimated_peak_temporary_bytes > free_bytes:
            raise OSError(
                "Insufficient free space for conservative swissEO monthly intermediates: "
                f"need about {estimated_peak_temporary_bytes / (1024**3):.1f} GiB, "
                f"have {free_bytes / (1024**3):.1f} GiB on {temporary_root}"
            )
        output_free_bytes = shutil.disk_usage(output_dir).free
        if estimated > output_free_bytes:
            raise OSError(
                "Insufficient output-volume space for swissEO monthly products: "
                f"need about {estimated / (1024**3):.1f} GiB uncompressed, "
                f"have {output_free_bytes / (1024**3):.1f} GiB on {output_dir}"
            )
        existing = [path for path in (reflectance, indices) if path.exists()]
        if existing and not self.config.overwrite:
            raise FileExistsError("Outputs exist; pass --overwrite")
        partial_reflectance = reflectance.with_name(
            f".{reflectance.name}.{uuid.uuid4().hex}.partial.tif"
        )
        partial_indices = indices.with_name(f".{indices.name}.{uuid.uuid4().hex}.partial.tif")
        # Aligning one orbit mosaic costs a multi-gigabyte download plus three
        # warps, and every alignment used to live inside the TemporaryDirectory
        # below -- so a single transient read timeout from the publication host
        # discarded the whole month's staging.  Keep the aligned items and the
        # band median beside it instead, so a rerun resumes from what finished.
        checkpoint_root = (
            temporary_root / "checkpoints" / self.config.stem
            if self.config.temporary_root is not None
            else None
        )
        if checkpoint_root is not None and self.config.overwrite:
            shutil.rmtree(checkpoint_root, ignore_errors=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix="terravault-swisseo-monthly-",
                dir=temporary_root,
            ) as temporary:
                temp = Path(temporary)
                checkpoint = checkpoint_root if checkpoint_root is not None else temp
                checkpoint.mkdir(parents=True, exist_ok=True)
                local_reflectance = checkpoint / "monthly-reflectance.tif"
                local_indices = checkpoint / "monthly-indices.tif"
                mosaic_started = time.perf_counter()
                median = checkpoint / "monthly-median.tif"

                def align(numbered):
                    number, item = numbered
                    directory = checkpoint / f"item-{number:03d}"
                    reused = self._reuse_aligned_item(directory)
                    if reused is not None:
                        logger.info(
                            "reusing aligned item %d/%d from an earlier run",
                            number, len(discovery.items),
                        )
                        return reused
                    aligned = self._aligned_item_bands(
                        item=item,
                        metadata=metadata[str(item["id"])],
                        directory=directory,
                    )
                    logger.info("aligned item %d/%d", number, len(discovery.items))
                    return aligned

                if self._readable_raster(
                    local_reflectance, bands=len(REFLECTANCE_BAND_NAMES)
                ):
                    # The band median and the country COG both survived an
                    # earlier attempt; only the index pass is still missing.
                    logger.info(
                        "reusing monthly reflectance %s from an earlier run",
                        local_reflectance.name,
                    )
                    info = inspect_raster(self._gdal["gdalinfo"], local_reflectance)
                    width, height = (int(value) for value in info["size"])
                else:
                    if self._readable_raster(median, bands=len(REFLECTANCE_BAND_NAMES)):
                        logger.info("reusing band median %s from an earlier run", median.name)
                    else:
                        logger.info(
                            "aligning %d orbit mosaics with %d workers",
                            len(discovery.items), _ITEM_WORKERS,
                        )
                        with ThreadPoolExecutor(max_workers=_ITEM_WORKERS) as pool:
                            # map preserves input order, which the median inputs rely on.
                            aligned_items = list(
                                pool.map(align, enumerate(discovery.items, start=1))
                            )
                        self._monthly_reflectance_median(
                            aligned_items=aligned_items,
                            output=median,
                        )
                        for number in range(1, len(aligned_items) + 1):
                            shutil.rmtree(checkpoint / f"item-{number:03d}", ignore_errors=True)
                    country = temp / "country.vrt"
                    warp_to_roi_vrt(
                        source_path=median,
                        output_path=country,
                        bbox_wgs84=self.config.bbox,
                        target_crs=self.config.target_crs,
                        resolution=self.config.resolution,
                        band_names=REFLECTANCE_BAND_NAMES,
                        gdal=self._gdal,
                        nodata=self.config.nodata,
                        cutline_path=self.config.cutline_path,
                        warp_memory_mib=self.config.warp_memory_mib,
                    )
                    info = inspect_raster(self._gdal["gdalinfo"], country)
                    width, height = (int(value) for value in info["size"])
                    exact_estimated = (
                        width
                        * height
                        * (len(REFLECTANCE_BAND_NAMES) + len(VEGETATION_INDEX_NAMES))
                        * _FLOAT32_BYTES
                    )
                    if exact_estimated / (1024**3) > self.config.max_output_gib:
                        raise ValueError("Exact uncompressed output exceeds max_output_gib")
                    write_described_cog(
                        source_path=country,
                        output_path=local_reflectance,
                        band_names=REFLECTANCE_BAND_NAMES,
                        gdal=self._gdal,
                        compression=self.config.compression,
                        metadata={"TERRAVAULT_SOURCE": "swissEO-S2-SR-monthly"},
                    )
                    median.unlink(missing_ok=True)
                mosaic_processing_seconds = time.perf_counter() - mosaic_started
                stage = time.perf_counter()
                if self._readable_raster(
                    local_indices, bands=len(VEGETATION_INDEX_NAMES)
                ):
                    logger.info(
                        "reusing monthly indices %s from an earlier run",
                        local_indices.name,
                    )
                else:
                    write_vegetation_indices(
                        reflectance_path=local_reflectance,
                        output_path=local_indices,
                        gdal=self._gdal,
                        nodata=self.config.nodata,
                        compression=self.config.compression,
                        metadata={"TERRAVAULT_SOURCE": "swissEO-S2-SR-monthly"},
                    )
                index_processing_seconds = time.perf_counter() - stage
                stage = time.perf_counter()
                _copy_file_sequential(
                    local_reflectance,
                    partial_reflectance,
                    output_root=self.config.output_root.resolve(),
                    wait_seconds=self.config.publication_wait_seconds,
                    poll_seconds=self.config.publication_poll_seconds,
                )
                reflectance_publish_seconds = time.perf_counter() - stage
                stage = time.perf_counter()
                _copy_file_sequential(
                    local_indices,
                    partial_indices,
                    output_root=self.config.output_root.resolve(),
                    wait_seconds=self.config.publication_wait_seconds,
                    poll_seconds=self.config.publication_poll_seconds,
                )
                indices_publish_seconds = time.perf_counter() - stage
                timings["monthly_mosaic_generation"] = (
                    mosaic_processing_seconds + reflectance_publish_seconds
                )
                timings["index_calculation"] = index_processing_seconds + indices_publish_seconds
            _replace_after_reconnect(
                partial_reflectance,
                reflectance,
                output_root=self.config.output_root.resolve(),
                wait_seconds=self.config.publication_wait_seconds,
                poll_seconds=self.config.publication_poll_seconds,
            )
            _replace_after_reconnect(
                partial_indices,
                indices,
                output_root=self.config.output_root.resolve(),
                wait_seconds=self.config.publication_wait_seconds,
                poll_seconds=self.config.publication_poll_seconds,
            )
            # Both products are on the output volume now, so the staged copies
            # they were resumed from must not keep tens of GiB of scratch alive.
            if checkpoint_root is not None:
                shutil.rmtree(checkpoint_root, ignore_errors=True)
        finally:
            partial_reflectance.unlink(missing_ok=True)
            partial_indices.unlink(missing_ok=True)
        timings["total"] = time.perf_counter() - started
        _write_json_atomic(
            manifest,
            self._manifest(
                items=discovery.items,
                summaries=summaries,
                reflectance=reflectance,
                indices=indices,
                width=width,
                height=height,
                estimated_peak_temporary_bytes=estimated_peak_temporary_bytes,
                timings=timings,
            ),
        )
        return SwissEOMonthlyResult(
            reflectance,
            indices,
            manifest,
            width,
            height,
            len(discovery.items),
            estimated_peak_temporary_bytes,
            timings,
            False,
        )
