"""Source-neutral vegetation-index definitions and GDAL helpers.

TreeAI is a formula reference only.  Nothing in this module retrieves data
from TreeAI or assumes that a raster was produced by its processing chain.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

REFLECTANCE_BAND_NAMES: tuple[str, ...] = ("B03", "B04", "B05", "B08", "B8A", "B11")
VEGETATION_INDEX_NAMES: tuple[str, ...] = ("NDVI", "GNDVI", "NDRE", "NDWI")

# Formula reference: https://www.treeai.earth/swiss-vegetation-condition
# The requested NDWI convention is (NIR - SWIR) / (NIR + SWIR).
VEGETATION_INDEX_DEFINITIONS: dict[str, dict[str, str]] = {
    "NDVI": {
        "formula": "(B08 - B04) / (B08 + B04)",
        "numerator_positive": "B08",
        "numerator_negative": "B04",
    },
    "GNDVI": {
        "formula": "(B08 - B03) / (B08 + B03)",
        "numerator_positive": "B08",
        "numerator_negative": "B03",
    },
    "NDRE": {
        "formula": "(B8A - B05) / (B8A + B05)",
        "numerator_positive": "B8A",
        "numerator_negative": "B05",
    },
    "NDWI": {
        "formula": "(B08 - B11) / (B08 + B11)",
        "numerator_positive": "B08",
        "numerator_negative": "B11",
    },
}

DEFAULT_VEGETATION_NODATA = -9999.0
_VARIABLES = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_MAX_GDAL_DIAGNOSTIC_CHARS = 12_000


def _bounded_gdal_diagnostic(value: str) -> str:
    """Keep repeated GDAL warnings from overwhelming logs and CLI errors."""

    stripped = value.strip()
    if len(stripped) <= _MAX_GDAL_DIAGNOSTIC_CHARS:
        return stripped
    head_size = 2_000
    tail_size = _MAX_GDAL_DIAGNOSTIC_CHARS - head_size
    omitted = len(stripped) - head_size - tail_size
    return (
        stripped[:head_size]
        + f"\n... {omitted} diagnostic characters omitted ...\n"
        + stripped[-tail_size:]
    )


def locate_gdal(*names: str) -> dict[str, str]:
    """Locate required GDAL commands or fail with one actionable error."""

    commands: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        path = shutil.which(name)
        if path is None:
            missing.append(name)
        else:
            commands[name] = path
    if missing:
        raise RuntimeError("GDAL command-line tools are required; missing: " + ", ".join(missing))
    return commands


# Per-process GDAL tuning. Several of these commands run concurrently, so
# these are deliberately per-process budgets rather than whole-machine ones.
GDAL_CACHE_MIB = os.environ.get("TERRAVAULT_GDAL_CACHE_MIB", "512")
GDAL_WRITE_THREADS = os.environ.get("TERRAVAULT_GDAL_THREADS", "2")


def run_gdal(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one GDAL command without leaking credentials into the log."""

    logger.debug("Running GDAL command: %s", " ".join(arguments))
    environment = os.environ.copy()
    # Block cache and compression threads are both left at GDAL's small
    # defaults otherwise, which shows up as jobs using well under one core
    # while waiting on I/O. Keep them modest: these commands run several at a
    # time, so the budget is per process, not per machine.
    environment.setdefault("GDAL_CACHEMAX", GDAL_CACHE_MIB)
    environment.setdefault("GDAL_NUM_THREADS", GDAL_WRITE_THREADS)
    environment.setdefault("GDAL_HTTP_MULTIRANGE", "YES")
    environment.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
    environment.setdefault("GDAL_HTTP_TIMEOUT", "120")
    environment.setdefault("GDAL_HTTP_MAX_RETRY", "6")
    environment.setdefault("GDAL_HTTP_RETRY_DELAY", "2")
    completed = subprocess.run(
        list(arguments),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    stdout = _bounded_gdal_diagnostic(completed.stdout)
    stderr = _bounded_gdal_diagnostic(completed.stderr)
    if stdout:
        logger.debug("GDAL stdout: %s", stdout)
    if stderr:
        logger.log(
            logging.ERROR if completed.returncode else logging.DEBUG,
            "GDAL stderr: %s",
            stderr,
        )
    if completed.returncode:
        detail = stderr or stdout
        raise RuntimeError(
            f"GDAL command failed with exit code {completed.returncode}: {detail}"
        )
    return completed


def inspect_raster(gdalinfo: str, path: str | Path) -> dict[str, Any]:
    """Return the JSON metadata for a local or remote GDAL raster."""

    completed = run_gdal((gdalinfo, "-json", "-mdd", "all", str(path)))
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GDAL returned invalid raster metadata for {path}") from exc


def raster_extent(metadata: Mapping[str, Any]) -> tuple[float, float, float, float]:
    """Calculate a north-up raster extent from gdalinfo JSON."""

    width, height = (int(value) for value in metadata["size"])
    transform = metadata["geoTransform"]
    x0, pixel_width, x_skew, y0, y_skew, pixel_height = (
        float(value) for value in transform
    )
    if abs(x_skew) > 1e-9 or abs(y_skew) > 1e-9:
        raise ValueError("Rotated rasters are not supported by this workflow")
    x1 = x0 + width * pixel_width
    y1 = y0 + height * pixel_height
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def set_vrt_band_descriptions(vrt_path: Path, names: Sequence[str]) -> None:
    """Set deterministic VRT band descriptions used by derived COGs."""

    tree = ET.parse(vrt_path)
    bands = tree.getroot().findall("VRTRasterBand")
    if len(bands) != len(names):
        raise RuntimeError(f"VRT has {len(bands)} bands; expected {len(names)}")
    for band, name in zip(bands, names, strict=True):
        description = band.find("Description")
        if description is None:
            description = ET.SubElement(band, "Description")
        description.text = name
    tree.write(vrt_path, encoding="UTF-8", xml_declaration=True)


def write_described_cog(
    *,
    source_path: Path,
    output_path: Path,
    band_names: Sequence[str],
    gdal: Mapping[str, str],
    compression: str,
    metadata: Mapping[str, str] | None = None,
) -> None:
    """Translate a raster to a Float32 COG with named bands."""

    described_vrt = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.vrt")
    try:
        run_gdal((gdal["gdal_translate"], "-of", "VRT", str(source_path), str(described_vrt)))
        set_vrt_band_descriptions(described_vrt, band_names)
        command = [
            gdal["gdal_translate"],
            "-of",
            "COG",
            "-ot",
            "Float32",
            "-co",
            f"COMPRESS={compression}",
            "-co",
            "BIGTIFF=YES",
            "-co",
            "BLOCKSIZE=512",
            "-co",
            "INTERLEAVE=BAND",
        ]
        for key, value in (metadata or {}).items():
            command.extend(("-mo", f"{key}={value}"))
        command.extend((str(described_vrt), str(output_path)))
        run_gdal(command)
    finally:
        described_vrt.unlink(missing_ok=True)


def write_vegetation_indices(
    *,
    reflectance_path: Path,
    output_path: Path,
    gdal: Mapping[str, str],
    nodata: float = DEFAULT_VEGETATION_NODATA,
    compression: str = "DEFLATE",
    metadata: Mapping[str, str] | None = None,
) -> None:
    """Calculate four two-band indices and stack them in canonical order.

    ``gdal_calc`` evaluates every supplied input for every ``--calc`` expression.
    Keeping one index per invocation therefore prevents each expression from
    rereading all six reflectance bands, which matters especially for remote COGs.
    """

    temporary_id = uuid.uuid4().hex
    stack_vrt = output_path.with_name(f".{output_path.name}.{temporary_id}.stack.vrt")
    calculated: list[Path] = []
    band_number = {
        name: number for number, name in enumerate(REFLECTANCE_BAND_NAMES, start=1)
    }
    try:
        for index_number, name in enumerate(VEGETATION_INDEX_NAMES, start=1):
            definition = VEGETATION_INDEX_DEFINITIONS[name]
            positive_band = definition["numerator_positive"]
            negative_band = definition["numerator_negative"]
            calculated_path = output_path.with_name(
                f".{output_path.name}.{temporary_id}."
                f"index-{index_number:02d}-{name.casefold()}.tif"
            )
            calculated.append(calculated_path)
            denominator = "(A+B)"
            valid = (
                f"logical_and.reduce((A!={nodata:g},B!={nodata:g},"
                f"abs({denominator})>1e-12))"
            )
            run_gdal(
                (
                    gdal["gdal_calc.py"],
                    "-A",
                    str(reflectance_path),
                    "--A_band",
                    str(band_number[positive_band]),
                    "-B",
                    str(reflectance_path),
                    "--B_band",
                    str(band_number[negative_band]),
                    "--calc",
                    f"where({valid},(A-B)/{denominator},{nodata:g})",
                    "--outfile",
                    str(calculated_path),
                    "--NoDataValue",
                    str(nodata),
                    "--type",
                    "Float32",
                    "--format",
                    "GTiff",
                    "--co",
                    f"COMPRESS={compression}",
                    "--co",
                    "BIGTIFF=YES",
                    "--co",
                    "TILED=YES",
                    "--co",
                    "INTERLEAVE=BAND",
                    "--hideNoData",
                    "--projectionCheck",
                    "--overwrite",
                    "--quiet",
                )
            )
        run_gdal(
            (
                gdal["gdalbuildvrt"],
                "-overwrite",
                "-separate",
                "-srcnodata",
                str(nodata),
                "-vrtnodata",
                str(nodata),
                str(stack_vrt),
                *(str(path) for path in calculated),
            )
        )
        write_described_cog(
            source_path=stack_vrt,
            output_path=output_path,
            band_names=VEGETATION_INDEX_NAMES,
            gdal=gdal,
            compression=compression,
            metadata=metadata,
        )
    finally:
        stack_vrt.unlink(missing_ok=True)
        for calculated_path in calculated:
            calculated_path.unlink(missing_ok=True)


def write_multiband_median(
    *,
    input_paths: Sequence[Path],
    output_path: Path,
    band_names: Sequence[str],
    gdal: Mapping[str, str],
    nodata: float = DEFAULT_VEGETATION_NODATA,
    compression: str = "DEFLATE",
) -> None:
    """Write a block-wise per-pixel median across aligned multiband rasters."""

    if not input_paths:
        raise ValueError("At least one input raster is required for a median")
    if len(input_paths) > len(_VARIABLES) * 1000:
        raise ValueError("Input count is unreasonably large for one GDAL calculation")
    command = [gdal["gdal_calc.py"]]
    multiple = len(input_paths) > 1
    for band_number, variable in enumerate(_VARIABLES[: len(band_names)], start=1):
        command.extend((f"-{variable}", *(str(path) for path in input_paths)))
        command.extend((f"--{variable}_band", str(band_number)))
        if multiple:
            values = f"where({variable}=={nodata:g},nan,{variable})"
            expression = f"nan_to_num(nanmedian({values},axis=0),nan={nodata:g})"
        else:
            expression = f"where({variable}=={nodata:g},{nodata:g},{variable})"
        command.extend(("--calc", expression))
    command.extend(
        (
            "--outfile",
            str(output_path),
            "--NoDataValue",
            str(nodata),
            "--type",
            "Float32",
            "--format",
            "GTiff",
            "--co",
            f"COMPRESS={compression}",
            "--co",
            "BIGTIFF=YES",
            "--co",
            "TILED=YES",
            "--co",
            f"NUM_THREADS={GDAL_WRITE_THREADS}",
            "--hideNoData",
            "--extent",
            "union",
            "--projectionCheck",
            "--overwrite",
            "--quiet",
        )
    )
    run_gdal(command)


def warp_to_roi_vrt(
    *,
    source_path: Path,
    output_path: Path,
    bbox_wgs84: Sequence[float],
    target_crs: str,
    resolution: float,
    band_names: Sequence[str],
    gdal: Mapping[str, str],
    nodata: float = DEFAULT_VEGETATION_NODATA,
    cutline_path: Path | None = None,
    warp_memory_mib: int = 256,
) -> None:
    """Align, clip and describe a multiband raster on a national ROI grid."""

    if len(bbox_wgs84) != 4:
        raise ValueError("bbox_wgs84 must have four coordinates")
    west, south, east, north = (float(value) for value in bbox_wgs84)
    command = [
        gdal["gdalwarp"],
        "-overwrite",
        "-of",
        "VRT",
        "-t_srs",
        target_crs,
        "-te_srs",
        "EPSG:4326",
        "-te",
        str(west),
        str(south),
        str(east),
        str(north),
        "-tr",
        str(resolution),
        str(resolution),
        "-tap",
        "-r",
        "bilinear",
        "-srcnodata",
        str(nodata),
        "-dstnodata",
        str(nodata),
        "-wm",
        str(warp_memory_mib),
        "-multi",
    ]
    if cutline_path is not None:
        command.extend(("-cutline", str(cutline_path), "-crop_to_cutline"))
    command.extend((str(source_path), str(output_path)))
    run_gdal(command)
    set_vrt_band_descriptions(output_path, band_names)


def calculate_index_values(bands: Mapping[str, float]) -> dict[str, float]:
    """Small scalar implementation used for validation and unit tests."""

    result: dict[str, float] = {}
    for name, definition in VEGETATION_INDEX_DEFINITIONS.items():
        positive = float(bands[definition["numerator_positive"]])
        negative = float(bands[definition["numerator_negative"]])
        denominator = positive + negative
        result[name] = float("nan") if abs(denominator) <= 1e-12 else (
            positive - negative
        ) / denominator
    return result
