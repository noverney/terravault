"""Memory-bounded extraction and mosaicking of indexed raster pieces.

Pixel data never passes through Python.  DuckDB selects intersecting local
pieces, GDAL creates lightweight warped VRTs, and the COG driver writes the
result block by block.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .dataset_catalog import DatasetCatalog
from .spatial import geometry_area

logger = logging.getLogger(__name__)

_GDAL_TO_BYTES = {
    "Byte": 1,
    "Int8": 1,
    "UInt16": 2,
    "Int16": 2,
    "UInt32": 4,
    "Int32": 4,
    "Float32": 4,
    "UInt64": 8,
    "Int64": 8,
    "Float64": 8,
}
_OUTPUT_DTYPES = frozenset(_GDAL_TO_BYTES)
_INTEGER_RANGES = {
    "Byte": (0, 255),
    "Int8": (-128, 127),
    "UInt16": (0, 65535),
    "Int16": (-32768, 32767),
    "UInt32": (0, 4294967295),
    "Int32": (-2147483648, 2147483647),
    "UInt64": (0, 18446744073709551615),
    "Int64": (-9223372036854775808, 9223372036854775807),
}
_STAC_TO_GDAL_DTYPE = {
    "uint8": "Byte",
    "int8": "Int8",
    "uint16": "UInt16",
    "int16": "Int16",
    "uint32": "UInt32",
    "int32": "Int32",
    "uint64": "UInt64",
    "int64": "Int64",
    "float32": "Float32",
    "float64": "Float64",
}
_RESAMPLING = frozenset(
    {
        "near",
        "bilinear",
        "cubic",
        "cubicspline",
        "lanczos",
        "average",
        "rms",
        "mode",
        "min",
        "max",
        "med",
        "q1",
        "q3",
        "sum",
    }
)
_MIN_LATEST_FOOTPRINT_RATIO = 0.9


def _validate_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    west, south, east, north = (float(value) for value in bbox)
    if not (-180 <= west < east <= 180):
        raise ValueError("bbox west/east must satisfy -180 <= west < east <= 180")
    if not (-90 <= south < north <= 90):
        raise ValueError("bbox south/north must satisfy -90 <= south < north <= 90")
    return west, south, east, north


def _promote_dtype(dtypes: Iterable[str]) -> str:
    """Return one GDAL scalar type that safely represents the input types."""

    unique = set(dtypes)
    unsupported = unique.difference(_GDAL_TO_BYTES)
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise ValueError(f"Unsupported GDAL raster data type(s): {names}")
    if not unique:
        raise ValueError("No raster bands were found in the generated VRT")
    if "Float64" in unique or unique.intersection({"UInt64", "Int64"}):
        return "Float64"
    if "Float32" in unique:
        return "Float32"
    if "UInt32" in unique and unique.intersection({"Int32", "Int16", "Int8"}):
        return "Float64"
    if "UInt32" in unique:
        return "UInt32"
    if "Int32" in unique:
        return "Int32"
    if "UInt16" in unique and unique.intersection({"Int16", "Int8"}):
        return "Int32"
    if "UInt16" in unique:
        return "UInt16"
    if "Int16" in unique:
        return "Int16"
    if "Int8" in unique:
        return "Int16" if "Byte" in unique else "Int8"
    return "Byte"


def _json_number(value: Any) -> int | float | None:
    """Normalize catalogue numeric strings for durable JSON provenance."""

    if value is None:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def _dtype_represents(dtype: str, value: str) -> bool:
    number = float(value)
    if dtype in {"Float32", "Float64"}:
        return True
    minimum, maximum = _INTEGER_RANGES[dtype]
    return number.is_integer() and minimum <= number <= maximum


def _source_gdal_dtype(piece: dict[str, Any]) -> str:
    """Resolve an indexed source type to the GDAL spelling used by warps."""

    raster_dtype = piece.get("raster_dtype")
    if raster_dtype in _GDAL_TO_BYTES:
        return str(raster_dtype)
    data_type = str(piece.get("data_type") or "").casefold()
    try:
        return _STAC_TO_GDAL_DTYPE[data_type]
    except KeyError as exc:
        raise ValueError(
            "Cannot choose a nodata-safe working type because an indexed source "
            f"has no supported raster datatype: {raster_dtype or data_type or 'missing'}"
        ) from exc


def _smallest_scalar_for(number: float) -> str:
    """Return a compact GDAL scalar type capable of storing ``number``."""

    if not math.isfinite(number):
        raise ValueError("nodata must be finite")
    if not number.is_integer():
        return "Float64"
    integer = int(number)
    for dtype in ("Byte", "Int8", "UInt16", "Int16", "UInt32", "Int32", "Int64"):
        minimum, maximum = _INTEGER_RANGES[dtype]
        if minimum <= integer <= maximum:
            return dtype
    return "Float64"


@dataclass(frozen=True)
class ExtractionConfig:
    """Configuration for a spatial raster extraction."""

    dataset_db: Path
    bbox: tuple[float, float, float, float]
    asset_keys: tuple[str, ...]
    output_path: Path
    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    selection: str = "latest-per-tile"
    target_crs: str = "auto"
    resolution: float | None = None
    resampling: str = "near"
    output_dtype: str = "auto"
    compression: str = "DEFLATE"
    nodata: str = "0"
    warp_memory_mib: int = 256
    max_output_gib: float = 16.0
    cutline_path: Path | None = None
    overwrite: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_db", Path(self.dataset_db).expanduser())
        object.__setattr__(self, "output_path", Path(self.output_path).expanduser())
        object.__setattr__(self, "bbox", _validate_bbox(self.bbox))
        keys = tuple(dict.fromkeys(key.strip() for key in self.asset_keys if key.strip()))
        if not keys:
            raise ValueError("At least one asset key/feature is required")
        object.__setattr__(self, "asset_keys", keys)
        if self.selection not in {"latest-per-tile", "all"}:
            raise ValueError("selection must be 'latest-per-tile' or 'all'")
        if self.resolution is not None and self.resolution <= 0:
            raise ValueError("resolution must be positive")
        if self.resampling not in _RESAMPLING:
            raise ValueError(f"Unsupported resampling method: {self.resampling}")
        if self.output_dtype != "auto" and self.output_dtype not in _OUTPUT_DTYPES:
            raise ValueError(f"Unsupported output data type: {self.output_dtype}")
        if self.warp_memory_mib < 16:
            raise ValueError("warp_memory_mib must be at least 16")
        if self.max_output_gib <= 0:
            raise ValueError("max_output_gib must be positive")
        if self.cutline_path is not None:
            object.__setattr__(
                self,
                "cutline_path",
                Path(self.cutline_path).expanduser().resolve(),
            )


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome and sizing information for an extraction."""

    output_path: Path
    manifest_path: Path
    width: int
    height: int
    band_count: int
    output_dtype: str
    estimated_uncompressed_bytes: int
    written_bytes: int | None
    source_count: int
    dry_run: bool


class RasterExtractor:
    """Query local pieces and stream a spatial mosaic to a tiled COG."""

    def __init__(self, config: ExtractionConfig) -> None:
        self.config = config
        self.catalog = DatasetCatalog(config.dataset_db)
        self._gdal = self._locate_gdal()

    @staticmethod
    def _locate_gdal() -> dict[str, str]:
        commands: dict[str, str] = {}
        missing: list[str] = []
        for name in ("gdalwarp", "gdalbuildvrt", "gdal_translate", "gdalinfo"):
            path = shutil.which(name)
            if path is None:
                missing.append(name)
            else:
                commands[name] = path
        if missing:
            raise RuntimeError(
                "GDAL command-line tools are required for extraction; missing: "
                + ", ".join(missing)
            )
        return commands

    @staticmethod
    def _run_command(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        logger.debug("Running GDAL command: %s", " ".join(arguments))
        environment = os.environ.copy()
        completed = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if completed.stdout.strip():
            logger.debug("GDAL stdout: %s", completed.stdout.strip())
        if completed.stderr.strip():
            level = logging.ERROR if completed.returncode else logging.DEBUG
            logger.log(level, "GDAL stderr: %s", completed.stderr.strip())
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"GDAL command failed with exit code {completed.returncode}: {detail}"
            )
        return completed

    def _query_sources(self) -> list[dict[str, Any]]:
        pieces = self.catalog.query_raster_pieces(
            bbox=self.config.bbox,
            start_datetime=self.config.start_datetime,
            end_datetime=self.config.end_datetime,
            asset_keys=self.config.asset_keys,
        )
        existing: list[dict[str, Any]] = []
        for piece in pieces:
            path = Path(piece["local_path"])
            if not path.is_file():
                logger.warning(
                    "Skipping indexed raster whose local file is missing – item=%s "
                    "asset=%s path=%s",
                    piece["item_id"],
                    piece["asset_key"],
                    path,
                )
                continue
            existing.append(piece)

        if self.config.selection == "latest-per-tile":
            required = set(self.config.asset_keys)
            item_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for piece in existing:
                tile_key = piece["tile_id"] or piece["item_id"]
                item_groups.setdefault((tile_key, piece["item_id"]), []).append(piece)
            complete_items: dict[str, list[list[dict[str, Any]]]] = {}
            for (tile_key, _item_id), group in item_groups.items():
                if {piece["asset_key"] for piece in group} != required:
                    continue
                complete_items.setdefault(tile_key, []).append(group)
            newest_items: dict[str, list[dict[str, Any]]] = {}
            for tile_key, groups in complete_items.items():
                areas: list[float] = []
                for group in groups:
                    geometry_json = group[0].get("geometry_json")
                    geometry = json.loads(geometry_json) if geometry_json else None
                    areas.append(geometry_area(geometry))
                largest_footprint = max(areas)
                candidates = [
                    group
                    for group, area in zip(groups, areas, strict=True)
                    if largest_footprint <= 0
                    or area >= largest_footprint * _MIN_LATEST_FOOTPRINT_RATIO
                ]
                newest_items[tile_key] = max(
                    candidates,
                    key=lambda group: (
                        group[0]["acquisition_time"],
                        group[0]["item_id"],
                    ),
                )
            incomplete_tiles = {tile_key for tile_key, _item_id in item_groups}.difference(
                newest_items
            )
            if incomplete_tiles:
                logger.warning(
                    "No single completed item has every requested feature for tile(s): %s",
                    ", ".join(sorted(incomplete_tiles)),
                )
            existing = [
                piece for tile_key in sorted(newest_items) for piece in newest_items[tile_key]
            ]

        order = {asset_key: index for index, asset_key in enumerate(self.config.asset_keys)}
        existing.sort(
            key=lambda piece: (
                order[piece["asset_key"]],
                piece["tile_id"] or "",
                piece["acquisition_time"],
                piece["item_id"],
            )
        )
        available = {piece["asset_key"] for piece in existing}
        missing = [key for key in self.config.asset_keys if key not in available]
        if missing:
            raise FileNotFoundError(
                "No completed local raster pieces intersect the request for feature(s): "
                + ", ".join(missing)
            )
        return existing

    def query_sources(self) -> list[dict[str, Any]]:
        """Return the exact local pieces that a subsequent extraction will use."""

        return self._query_sources()

    def _target_crs(self, pieces: Sequence[dict[str, Any]]) -> str:
        if self.config.target_crs.lower() != "auto":
            return self.config.target_crs
        epsg_codes = {
            int(piece["proj_epsg"]) for piece in pieces if piece.get("proj_epsg") is not None
        }
        if len(epsg_codes) == 1:
            return f"EPSG:{epsg_codes.pop()}"
        logger.info(
            "Source pieces span multiple or unknown CRSs; using EPSG:3857. "
            "Specify --target-crs for a national/project CRS."
        )
        return "EPSG:3857"

    def _resolution(self, pieces: Sequence[dict[str, Any]], target_crs: str) -> float:
        if self.config.resolution is not None:
            return self.config.resolution
        if target_crs.upper() in {"EPSG:4326", "OGC:CRS84", "CRS84"}:
            raise ValueError(
                "Automatic resolution is measured in metres and cannot be used with a "
                "geographic target CRS; specify --resolution in target-CRS units"
            )
        resolutions = [
            float(piece["resolution_m"])
            for piece in pieces
            if piece.get("resolution_m") is not None
        ]
        if not resolutions:
            raise ValueError(
                "No native resolution is indexed; specify --resolution in target-CRS units"
            )
        return min(resolutions)

    def _destination_nodata(
        self,
        pieces: Sequence[dict[str, Any]],
    ) -> tuple[str, str | None]:
        """Choose a common sentinel without consuming valid zero-probability data."""

        destination = self.config.nodata
        zero_is_valid = any(
            str(piece["asset_key"]).casefold().startswith("cld_") and piece.get("nodata") is None
            for piece in pieces
        )
        try:
            destination_is_zero = float(destination) == 0
        except (TypeError, ValueError) as exc:
            raise ValueError(f"nodata must be numeric, got {destination!r}") from exc
        if destination_is_zero and zero_is_valid:
            destination = "-9999"
            logger.warning(
                "Destination nodata 0 collides with valid CLD probability 0; "
                "using the signed sentinel -9999 for this mixed-feature extraction"
            )

        destination_number = float(destination)
        source_dtype = _promote_dtype(_source_gdal_dtype(piece) for piece in pieces)
        # A VRT inherits its source scalar type. Promote only when that type cannot
        # represent the destination sentinel; floating sources must not be truncated.
        working_dtype = None
        if not _dtype_represents(source_dtype, destination):
            working_dtype = _promote_dtype((source_dtype, _smallest_scalar_for(destination_number)))
        return destination, working_dtype

    def _band_contract(
        self,
        pieces: Sequence[dict[str, Any]],
        *,
        destination_nodata: str,
    ) -> dict[str, Any]:
        """Describe raw storage and physical-value transforms in band order."""

        bands: list[dict[str, Any]] = []
        for number, asset_key in enumerate(self.config.asset_keys, start=1):
            sources = [piece for piece in pieces if piece["asset_key"] == asset_key]
            transforms = {
                (
                    _json_number(piece.get("raster_scale")),
                    _json_number(piece.get("raster_offset")),
                )
                for piece in sources
            }
            if len(transforms) != 1:
                raise ValueError(
                    f"Sources for {asset_key} have inconsistent scale/offset metadata; "
                    "extract acquisitions with one physical-value transform at a time"
                )
            scale, offset = transforms.pop()
            source_nodata = sorted(
                {_json_number(piece.get("nodata")) for piece in sources},
                key=lambda value: (value is not None, str(value)),
            )
            bands.append(
                {
                    "band": number,
                    "asset_key": asset_key,
                    "source_data_types": sorted(
                        {
                            str(piece.get("data_type") or piece.get("raster_dtype") or "")
                            for piece in sources
                        }
                    ),
                    "source_nodata_values": source_nodata,
                    "output_nodata": _json_number(destination_nodata),
                    "scale": scale,
                    "offset": offset,
                    "transform_applied": False,
                    "zero_is_valid": None in source_nodata,
                }
            )
        return {
            "schema_version": 1,
            "storage": "raw",
            "transform": "physical = raw * scale + offset",
            "bands": bands,
        }

    def _warp_feature(
        self,
        *,
        asset_key: str,
        sources: Sequence[dict[str, Any]],
        target_crs: str,
        resolution: float,
        destination_nodata: str,
        working_dtype: str | None,
        output_vrt: Path,
    ) -> None:
        west, south, east, north = self.config.bbox
        logger.info(
            "Building virtual feature mosaic – feature=%s sources=%d target_crs=%s resolution=%s",
            asset_key,
            len(sources),
            target_crs,
            resolution,
        )
        source_vrt_dir = output_vrt.parent / f"{output_vrt.stem}-sources"
        source_vrt_dir.mkdir()
        warped_sources: list[Path] = []
        for index, piece in enumerate(sources):
            warped_source = source_vrt_dir / f"{index:05d}.vrt"
            command = [
                self._gdal["gdalwarp"],
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
                self.config.resampling,
                "-dstnodata",
                destination_nodata,
                "-wm",
                str(self.config.warp_memory_mib),
                "-multi",
            ]
            if working_dtype is not None:
                command.extend(("-ot", working_dtype))
            source_nodata = piece.get("nodata")
            if source_nodata is not None:
                command.extend(("-srcnodata", str(source_nodata)))
            if self.config.cutline_path is not None:
                command.extend(["-cutline", str(self.config.cutline_path)])
            command.extend([str(piece["local_path"]), str(warped_source)])
            self._run_command(command)
            warped_sources.append(warped_source)
        self._run_command(
            [
                self._gdal["gdalbuildvrt"],
                "-overwrite",
                "-srcnodata",
                destination_nodata,
                "-vrtnodata",
                destination_nodata,
                str(output_vrt),
                *(str(path) for path in warped_sources),
            ]
        )

    @staticmethod
    def _set_band_metadata(stack_vrt: Path, band_contract: dict[str, Any]) -> None:
        tree = ET.parse(stack_vrt)
        root = tree.getroot()
        bands = root.findall("VRTRasterBand")
        contract_bands = band_contract["bands"]
        if len(bands) != len(contract_bands):
            raise RuntimeError(
                f"Stacked VRT has {len(bands)} bands; expected {len(contract_bands)}"
            )
        for band, contract in zip(bands, contract_bands, strict=True):
            description = band.find("Description")
            if description is None:
                description = ET.SubElement(band, "Description")
            description.text = contract["asset_key"]
            for element_name, field in (("Scale", "scale"), ("Offset", "offset")):
                value = contract[field]
                if value is None:
                    continue
                element = band.find(element_name)
                if element is None:
                    element = ET.SubElement(band, element_name)
                element.text = str(value)
        tree.write(stack_vrt, encoding="UTF-8", xml_declaration=True)

    def _inspect_vrt(self, stack_vrt: Path) -> tuple[int, int, list[str]]:
        completed = self._run_command([self._gdal["gdalinfo"], "-json", str(stack_vrt)])
        metadata = json.loads(completed.stdout)
        width, height = (int(value) for value in metadata["size"])
        dtypes = [str(band["type"]) for band in metadata.get("bands", [])]
        return width, height, dtypes

    def _write_cog(
        self,
        *,
        stack_vrt: Path,
        partial_path: Path,
        output_dtype: str,
        asset_keys: Sequence[str],
        band_contract: dict[str, Any],
    ) -> None:
        command = [
            self._gdal["gdal_translate"],
            "--config",
            "GDAL_CACHEMAX",
            str(self.config.warp_memory_mib),
            "-of",
            "COG",
            "-ot",
            output_dtype,
            "-co",
            f"COMPRESS={self.config.compression}",
            "-co",
            "BIGTIFF=YES",
            "-co",
            "BLOCKSIZE=512",
            "-co",
            "NUM_THREADS=1",
            "-mo",
            f"TERRAVAULT_ASSET_KEYS={','.join(asset_keys)}",
            "-mo",
            "TERRAVAULT_BAND_CONTRACT="
            + json.dumps(band_contract, sort_keys=True, separators=(",", ":")),
            str(stack_vrt),
            str(partial_path),
        ]
        logger.info(
            "Streaming stitched COG – output=%s dtype=%s bands=%d memory_limit=%dMiB",
            self.config.output_path.resolve(),
            output_dtype,
            len(asset_keys),
            self.config.warp_memory_mib,
        )
        self._run_command(command)

    def extract(self) -> ExtractionResult:
        """Build a query result without materialising a country raster in RAM."""

        output_path = self.config.output_path.resolve()
        manifest_path = output_path.with_suffix(f"{output_path.suffix}.manifest.json")
        if output_path.exists() and not self.config.overwrite and not self.config.dry_run:
            raise FileExistsError(
                f"Output already exists: {output_path}; pass --overwrite to replace it"
            )
        if self.config.cutline_path is not None and not self.config.cutline_path.is_file():
            raise FileNotFoundError(f"Cutline GeoJSON does not exist: {self.config.cutline_path}")

        pieces = self.query_sources()
        target_crs = self._target_crs(pieces)
        resolution = self._resolution(pieces, target_crs)
        grouped = {
            key: [piece for piece in pieces if piece["asset_key"] == key]
            for key in self.config.asset_keys
        }
        destination_nodata, working_dtype = self._destination_nodata(pieces)
        band_contract = self._band_contract(
            pieces,
            destination_nodata=destination_nodata,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Extraction selected local pieces – database=%s bbox=%s features=%s "
            "pieces=%d selection=%s",
            self.config.dataset_db.resolve(),
            self.config.bbox,
            ",".join(self.config.asset_keys),
            len(pieces),
            self.config.selection,
        )

        with tempfile.TemporaryDirectory(
            prefix="terravault-extract-",
            dir=output_path.parent,
        ) as temporary:
            temporary_path = Path(temporary)
            feature_vrts: list[Path] = []
            for index, asset_key in enumerate(self.config.asset_keys):
                feature_vrt = temporary_path / f"feature-{index:03d}.vrt"
                self._warp_feature(
                    asset_key=asset_key,
                    sources=grouped[asset_key],
                    target_crs=target_crs,
                    resolution=resolution,
                    destination_nodata=destination_nodata,
                    working_dtype=working_dtype,
                    output_vrt=feature_vrt,
                )
                feature_vrts.append(feature_vrt)

            stack_vrt = temporary_path / "stack.vrt"
            self._run_command(
                [
                    self._gdal["gdalbuildvrt"],
                    "-overwrite",
                    "-separate",
                    str(stack_vrt),
                    *(str(path) for path in feature_vrts),
                ]
            )
            self._set_band_metadata(stack_vrt, band_contract)
            width, height, source_dtypes = self._inspect_vrt(stack_vrt)
            output_dtype = (
                _promote_dtype(source_dtypes)
                if self.config.output_dtype == "auto"
                else self.config.output_dtype
            )
            if not _dtype_represents(output_dtype, destination_nodata):
                raise ValueError(
                    f"Destination nodata {destination_nodata} is not representable as "
                    f"{output_dtype}; choose a signed or floating --output-dtype"
                )
            band_contract["output_dtype"] = output_dtype
            estimated_bytes = (
                width * height * len(self.config.asset_keys) * _GDAL_TO_BYTES[output_dtype]
            )
            estimated_gib = estimated_bytes / (1024**3)
            logger.info(
                "Extraction size estimate – width=%d height=%d bands=%d dtype=%s "
                "uncompressed=%.2fGiB limit=%.2fGiB",
                width,
                height,
                len(self.config.asset_keys),
                output_dtype,
                estimated_gib,
                self.config.max_output_gib,
            )
            if estimated_gib > self.config.max_output_gib:
                raise ValueError(
                    f"Estimated uncompressed output is {estimated_gib:.2f} GiB, above "
                    f"the {self.config.max_output_gib:.2f} GiB safety limit. Narrow the "
                    "bbox/features, use a coarser --resolution, or explicitly raise "
                    "--max-output-gib."
                )

            partial_path = output_path.with_name(
                f".{output_path.name}.{uuid.uuid4().hex}.partial.tif"
            )
            written_bytes: int | None = None
            try:
                if not self.config.dry_run:
                    self._write_cog(
                        stack_vrt=stack_vrt,
                        partial_path=partial_path,
                        output_dtype=output_dtype,
                        asset_keys=self.config.asset_keys,
                        band_contract=band_contract,
                    )
                    os.replace(partial_path, output_path)
                    written_bytes = output_path.stat().st_size
            finally:
                partial_path.unlink(missing_ok=True)

        manifest = {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": self.config.dry_run,
            "dataset_db": str(self.config.dataset_db.resolve()),
            "output_path": str(output_path),
            "bbox_wgs84": list(self.config.bbox),
            "cutline_path": (
                None if self.config.cutline_path is None else str(self.config.cutline_path)
            ),
            "start_datetime": (
                None
                if self.config.start_datetime is None
                else self.config.start_datetime.isoformat()
            ),
            "end_datetime": (
                None if self.config.end_datetime is None else self.config.end_datetime.isoformat()
            ),
            "selection": self.config.selection,
            "target_crs": target_crs,
            "resolution": resolution,
            "resampling": self.config.resampling,
            "requested_nodata": self.config.nodata,
            "nodata": destination_nodata,
            "asset_keys_in_band_order": list(self.config.asset_keys),
            "band_contract": band_contract,
            "width": width,
            "height": height,
            "band_count": len(self.config.asset_keys),
            "output_dtype": output_dtype,
            "estimated_uncompressed_bytes": estimated_bytes,
            "written_bytes": written_bytes,
            "warp_memory_mib": self.config.warp_memory_mib,
            "sources": [
                {
                    "item_id": piece["item_id"],
                    "tile_id": piece["tile_id"],
                    "acquisition_time": piece["acquisition_time"].isoformat(),
                    "asset_key": piece["asset_key"],
                    "local_path": piece["local_path"],
                    "nodata": _json_number(piece.get("nodata")),
                    "data_type": piece.get("data_type"),
                    "raster_scale": _json_number(piece.get("raster_scale")),
                    "raster_offset": _json_number(piece.get("raster_offset")),
                }
                for piece in pieces
            ],
        }
        temporary_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{uuid.uuid4().hex}.partial"
        )
        temporary_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary_manifest, manifest_path)
        logger.info(
            "Extraction complete – output=%s manifest=%s bytes=%s",
            output_path if not self.config.dry_run else "(dry run)",
            manifest_path.resolve(),
            written_bytes,
        )
        return ExtractionResult(
            output_path=output_path,
            manifest_path=manifest_path,
            width=width,
            height=height,
            band_count=len(self.config.asset_keys),
            output_dtype=output_dtype,
            estimated_uncompressed_bytes=estimated_bytes,
            written_bytes=written_bytes,
            source_count=len(pieces),
            dry_run=self.config.dry_run,
        )
