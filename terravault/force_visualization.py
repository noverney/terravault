"""NDVI products and quicklooks from TerraVault FORCE feature mosaics.

The input is expected to use the standard TerraVault band order:
B04 (red), B08 (near infrared), SCL and CLD. Pixel processing stays in GDAL;
Python only validates metadata, assembles commands and records provenance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

DEFAULT_INVALID_SCL_CLASSES = (0, 1, 3, 8, 9, 10, 11)
NDVI_COLORS = (
    (-1.0, 49, 54, 149, 255, "water / very low"),
    (-0.2, 69, 117, 180, 255, "water / shadow"),
    (0.0, 190, 77, 47, 255, "bare / built"),
    (0.1, 230, 145, 56, 255, "very sparse vegetation"),
    (0.2, 245, 216, 120, 255, "sparse vegetation"),
    (0.35, 166, 217, 106, 255, "moderate vegetation"),
    (0.5, 72, 161, 78, 255, "dense vegetation"),
    (0.7, 0, 104, 55, 255, "very dense vegetation"),
    (1.0, 0, 68, 27, 255, "upper bound"),
)
_SAFE_STEM = re.compile(r"[^A-Za-z0-9_.-]+")
_FORCE_QAI_NAME = re.compile(
    r"(?P<date>\d{8})_LEVEL2_(?P<sensor>SEN2[ABC])_QAI(?:\.vrt|\.tif)$",
    re.IGNORECASE,
)
_SENTINEL_PRODUCT_NAME = re.compile(
    r"^(?P<platform>S2[ABC])_MSIL[12][AC]_"
    r"(?P<sensing>\d{8}T\d{6})_"
    r"N\d{4}_"
    r"(?P<orbit>R\d{3})_"
    r"(?P<tile>T\d{2}[A-Z]{3})_"
)
FORCE_QAI_FIELDS = (
    ("0", 0x0001, "nodata"),
    ("1-2", 0x0006, "cloud_buffer_opaque_or_cirrus"),
    ("3", 0x0008, "cloud_shadow"),
    ("4", 0x0010, "snow"),
    ("8", 0x0100, "subzero_reflectance"),
    ("9", 0x0200, "saturated_reflectance"),
)
VISUALIZATION_ALGORITHM_VERSION = 2
_NATIVE_FORCE_BOA_DOMAINS = (
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
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_WKT_AUTHORITY = re.compile(
    r'(?:ID|AUTHORITY)\["(?P<authority>[^"]+)",\s*"?(?P<code>\d+)"?\]'
)


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _same_crs(left: str | None, right: str | None) -> bool:
    """Compare WKT semantically enough to tolerate GDAL serialization variants."""

    if not left or not right:
        return False
    left_ids = _WKT_AUTHORITY.findall(left)
    right_ids = _WKT_AUTHORITY.findall(right)
    if left_ids and right_ids:
        return tuple(value.casefold() for value in left_ids[-1]) == tuple(
            value.casefold() for value in right_ids[-1]
        )
    def normalize(value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()

    return normalize(left) == normalize(right)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ForceVisualizationConfig:
    """Configuration for a FORCE NDVI COG and colorized quicklook."""

    input_path: Path
    output_dir: Path | None = None
    output_stem: str | None = None
    red_band: int | None = None
    nir_band: int | None = None
    scl_band: int | None = None
    cloud_band: int | None = None
    mask_quality: bool = True
    cloud_threshold: float = 50.0
    invalid_scl_classes: tuple[int, ...] = DEFAULT_INVALID_SCL_CLASSES
    crop_to_force_input: bool = True
    quicklook_width: int = 1400
    force_qai_path: Path | None = None
    force_qai_mask: int = 0x031F
    allow_force_time_mismatch: bool = False
    debug_plot: bool = True
    debug_plot_width: int = 1800
    overwrite: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        input_path = _absolute(self.input_path)
        object.__setattr__(self, "input_path", input_path)
        output_dir = (
            self._default_output_dir(input_path)
            if self.output_dir is None
            else _absolute(self.output_dir)
        )
        object.__setattr__(self, "output_dir", output_dir)
        if self.force_qai_path is not None:
            object.__setattr__(self, "force_qai_path", _absolute(self.force_qai_path))
        stem = self.output_stem or f"{input_path.stem}_ndvi"
        stem = _SAFE_STEM.sub("_", stem).strip("._-")
        if not stem:
            raise ValueError("output_stem must contain a letter or digit")
        object.__setattr__(self, "output_stem", stem)
        for name in ("red_band", "nir_band", "scl_band", "cloud_band"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1")
        if not 0 <= self.cloud_threshold <= 100:
            raise ValueError("cloud_threshold must be between 0 and 100")
        if self.quicklook_width < 64:
            raise ValueError("quicklook_width must be at least 64 pixels")
        if self.debug_plot_width < 640:
            raise ValueError("debug_plot_width must be at least 640 pixels")
        if not 1 <= self.force_qai_mask <= 0xFFFF:
            raise ValueError("force_qai_mask must be between 1 and 65535")
        classes = tuple(dict.fromkeys(int(value) for value in self.invalid_scl_classes))
        if any(value < 0 or value > 11 for value in classes):
            raise ValueError("Sentinel-2 SCL classes must be between 0 and 11")
        object.__setattr__(self, "invalid_scl_classes", classes)

    @staticmethod
    def _default_output_dir(input_path: Path) -> Path:
        if input_path.parent.name == "mosaic" and input_path.parent.parent.name == "datacube":
            return input_path.parent.parent.parent / "visualizations"
        return input_path.parent / "visualizations"


@dataclass(frozen=True)
class ForceVisualizationResult:
    """Outputs of a FORCE visualization run."""

    status: str
    ndvi_path: Path
    quicklook_path: Path
    force_ndvi_path: Path | None
    force_quicklook_path: Path | None
    force_worldfile_path: Path | None
    debug_plot_path: Path | None
    worldfile_path: Path
    manifest_path: Path
    width: int
    height: int
    valid_percent: float | None
    raw_valid_percent: float | None
    force_valid_percent: float | None
    commands: tuple[tuple[str, ...], ...]
    skipped: bool = False


class ForceVisualizer:
    """Generate a georeferenced NDVI COG and a compact PNG quicklook."""

    def __init__(self, config: ForceVisualizationConfig) -> None:
        self.config = config
        assert config.output_dir is not None
        assert config.output_stem is not None
        self.ndvi_path = config.output_dir / f"{config.output_stem}.tif"
        self.quicklook_path = config.output_dir / f"{config.output_stem}.png"
        self.force_ndvi_path = (
            config.output_dir / f"{config.output_stem}_force_qai.tif"
            if config.force_qai_path is not None
            else None
        )
        self.force_quicklook_path = (
            config.output_dir / f"{config.output_stem}_force_qai.png"
            if config.force_qai_path is not None
            else None
        )
        self.force_worldfile_path = (
            config.output_dir / f"{config.output_stem}_force_qai.wld"
            if config.force_qai_path is not None
            else None
        )
        self.debug_plot_path = (
            config.output_dir
            / (
                f"{config.output_stem}_raw_cdse_force.png"
                if config.force_qai_path is not None
                else f"{config.output_stem}_before_after.png"
            )
            if config.debug_plot
            else None
        )
        self.worldfile_path = config.output_dir / f"{config.output_stem}.wld"
        self.manifest_path = config.output_dir / f"{config.output_stem}.visualization.json"
        self._commands: list[tuple[str, ...]] = []
        self._gdal = self._locate_gdal()

    @staticmethod
    def _locate_gdal() -> dict[str, str]:
        commands: dict[str, str] = {}
        missing: list[str] = []
        for name in ("gdal_calc.py", "gdal_translate", "gdaldem", "gdalinfo", "gdalwarp"):
            path = shutil.which(name)
            if path is None:
                missing.append(name)
            else:
                commands[name] = path
        if missing:
            raise RuntimeError(
                "GDAL command-line tools are required for visualization; missing: "
                + ", ".join(missing)
            )
        return commands

    @staticmethod
    def _run(
        arguments: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        logger.debug("Running visualization command: %s", " ".join(arguments))
        completed = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.stdout.strip():
            logger.debug("Visualization stdout: %s", completed.stdout.strip())
        if completed.stderr.strip():
            level = logging.ERROR if completed.returncode else logging.DEBUG
            logger.log(level, "Visualization stderr: %s", completed.stderr.strip())
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"Visualization command failed with exit code {completed.returncode}: {detail}"
            )
        return completed

    def _execute(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
        command = tuple(str(value) for value in arguments)
        self._commands.append(command)
        if self.config.dry_run:
            return None
        return self._run(command)

    def _inspect(self, path: Path, *, statistics: bool = False) -> dict[str, Any]:
        command = [self._gdal["gdalinfo"], "-json"]
        if statistics:
            command.append("-stats")
        command.append(str(path))
        completed = self._run(command)
        return json.loads(completed.stdout)

    @staticmethod
    def _described_band(
        metadata: dict[str, Any],
        description: str,
        fallback: int,
    ) -> int:
        wanted = description.casefold()
        for band in metadata.get("bands") or []:
            if str(band.get("description") or "").casefold() == wanted:
                return int(band["band"])
        return fallback

    @staticmethod
    def _force_domain(band: dict[str, Any]) -> str | None:
        metadata = band.get("metadata") or {}
        force = metadata.get("FORCE") or metadata.get("force") or {}
        value = force.get("Domain") or force.get("DOMAIN")
        return None if value is None else str(value).upper()

    def _reject_native_boa(self, metadata: dict[str, Any]) -> None:
        bands = metadata.get("bands") or []
        if len(bands) != len(_NATIVE_FORCE_BOA_DOMAINS):
            return
        descriptions = tuple(str(band.get("description") or "").upper() for band in bands)
        domains = tuple(self._force_domain(band) for band in bands)
        root_force = (metadata.get("metadata") or {}).get("FORCE") or {}
        product = str(root_force.get("Product") or root_force.get("PRODUCT") or "").upper()
        if (
            descriptions == _NATIVE_FORCE_BOA_DOMAINS
            or domains == _NATIVE_FORCE_BOA_DOMAINS
            or product == "BOA"
        ):
            raise ValueError(
                "Native FORCE BOA is a 10-band atmospheric-correction product, not the "
                "TerraVault external-feature B04/B08/SCL/CLD stack required by this "
                "mask-comparison visualizer. Use the external-feature mosaic as --input; "
                "BOA-versus-L2A reflectance needs a separate comparison mode."
            )

    def _bands(self, metadata: dict[str, Any]) -> dict[str, int]:
        self._reject_native_boa(metadata)
        result = {
            "red": self.config.red_band or self._described_band(metadata, "B04_10m", 1),
            "nir": self.config.nir_band or self._described_band(metadata, "B08_10m", 2),
            "scl": self.config.scl_band or self._described_band(metadata, "SCL_20m", 3),
            "cloud": self.config.cloud_band or self._described_band(metadata, "CLD_20m", 4),
        }
        band_count = len(metadata.get("bands") or [])
        required = ("red", "nir", "scl", "cloud") if self.config.mask_quality else ("red", "nir")
        invalid = [name for name in required if result[name] > band_count]
        if invalid:
            raise ValueError(
                f"Input has {band_count} bands; invalid mappings for: " + ", ".join(invalid)
            )
        mapped = [result[name] for name in required]
        if len(set(mapped)) != len(mapped):
            raise ValueError("Required visualization band mappings must use distinct bands")
        return result

    @staticmethod
    def _band_nodata(metadata: dict[str, Any], band_number: int) -> float | int:
        bands = metadata.get("bands") or []
        value = bands[band_number - 1].get("noDataValue")
        if value is None:
            raise ValueError(f"Input band {band_number} has no nodata value")
        return value

    def _extraction_manifest(self) -> Path | None:
        direct = self.config.input_path.with_suffix(
            f"{self.config.input_path.suffix}.manifest.json"
        )
        if direct.is_file():
            return direct
        force_manifest = self._force_job_manifest()
        if force_manifest is None:
            return None
        job = json.loads(force_manifest.read_text(encoding="utf-8"))
        source_path = Path(str(job.get("input_path") or ""))
        candidate = source_path.with_suffix(f"{source_path.suffix}.manifest.json")
        return candidate if candidate.is_file() else None

    @staticmethod
    def _scene_metadata_path(local_path: str, extraction_manifest: Path) -> Path | None:
        source = Path(local_path).expanduser()
        candidates = [source.parent / "scene_metadata.json"]
        if not source.is_absolute():
            candidates.append(extraction_manifest.parent / source.parent / "scene_metadata.json")
        return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)

    @staticmethod
    def _embedded_band_contract(metadata: dict[str, Any]) -> dict[str, Any] | None:
        root = (metadata.get("metadata") or {}).get("") or {}
        value = root.get("TERRAVAULT_BAND_CONTRACT")
        if value is None:
            return None
        try:
            contract = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise ValueError("TERRAVAULT_BAND_CONTRACT is not valid JSON") from exc
        if not isinstance(contract, dict):
            raise ValueError("TERRAVAULT_BAND_CONTRACT must be a JSON object")
        return contract

    @staticmethod
    def _contract_entries(contract: dict[str, Any]) -> dict[int, dict[str, Any]]:
        if contract.get("schema_version") != 1 or contract.get("storage") != "raw":
            raise ValueError("Unsupported TerraVault extraction band contract")
        entries = contract.get("bands")
        if not isinstance(entries, list):
            raise ValueError("TerraVault extraction band contract has no band list")
        result: dict[int, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("band"), int):
                raise ValueError("TerraVault extraction band contract has an invalid band entry")
            number = int(entry["band"])
            if number in result:
                raise ValueError("TerraVault extraction band contract repeats a band number")
            result[number] = entry
        return result

    @staticmethod
    def _legacy_source_metadata(
        extraction: dict[str, Any],
        extraction_manifest: Path,
    ) -> tuple[dict[str, set[tuple[Any, Any, Any]]], list[Path]]:
        values: dict[str, set[tuple[Any, Any, Any]]] = {}
        provenance_files: set[Path] = set()
        for source in extraction.get("sources") or []:
            if not isinstance(source, dict):
                continue
            asset_key = str(source.get("asset_key") or "")
            scale = source.get("raster_scale")
            offset = source.get("raster_offset")
            nodata = source.get("nodata")
            if any(value is not None for value in (scale, offset, nodata)):
                values.setdefault(asset_key, set()).add((scale, offset, nodata))
                continue
            scene_path = ForceVisualizer._scene_metadata_path(
                str(source.get("local_path") or ""),
                extraction_manifest,
            )
            if scene_path is None:
                continue
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
            asset = (scene.get("assets") or {}).get(asset_key) or {}
            values.setdefault(asset_key, set()).add(
                (
                    asset.get("raster:scale"),
                    asset.get("raster:offset"),
                    asset.get("nodata"),
                )
            )
            provenance_files.add(scene_path)
        return values, sorted(provenance_files)

    def _band_semantics(
        self,
        metadata: dict[str, Any],
        bands: dict[str, int],
    ) -> dict[str, Any]:
        """Resolve an explicit raw-DN to physical-reflectance contract."""

        extraction_manifest = self._extraction_manifest()
        extraction: dict[str, Any] | None = None
        sidecar_contract: dict[str, Any] | None = None
        provenance_files: list[Path] = []
        if extraction_manifest is not None:
            extraction = json.loads(extraction_manifest.read_text(encoding="utf-8"))
            candidate = extraction.get("band_contract")
            if candidate is not None and not isinstance(candidate, dict):
                raise ValueError("Extraction manifest band_contract must be a JSON object")
            sidecar_contract = candidate
            provenance_files.append(extraction_manifest.resolve())

        embedded_contract = self._embedded_band_contract(metadata)
        if (
            embedded_contract is not None
            and sidecar_contract is not None
            and embedded_contract != sidecar_contract
        ):
            raise ValueError("Embedded and sidecar TerraVault band contracts disagree")
        contract = sidecar_contract or embedded_contract
        source = "unverified_identity"
        entries: dict[int, dict[str, Any]] = {}
        legacy_values: dict[str, set[tuple[Any, Any, Any]]] = {}
        if contract is not None:
            entries = self._contract_entries(contract)
            source = (
                "extraction_band_contract"
                if sidecar_contract is not None
                else "embedded_band_contract"
            )
        elif extraction is not None and extraction_manifest is not None:
            legacy_values, scene_files = self._legacy_source_metadata(
                extraction,
                extraction_manifest,
            )
            provenance_files.extend(scene_files)
            source = "legacy_extraction_stac_provenance"

        metadata_bands = metadata.get("bands") or []
        reflectance: dict[str, dict[str, Any]] = {}
        expected_assets = {"red": "B04_10m", "nir": "B08_10m"}
        for logical_name, expected_asset in expected_assets.items():
            number = bands[logical_name]
            raster_band = metadata_bands[number - 1]
            entry = entries.get(number)
            if contract is not None and entry is None:
                raise ValueError(
                    f"TerraVault extraction band contract has no entry for band {number} "
                    f"({expected_asset})"
                )
            transform_applied = False
            if entry is not None:
                asset_key = str(entry.get("asset_key") or "")
                if asset_key.casefold() != expected_asset.casefold():
                    raise ValueError(
                        f"Band {number} is contracted as {asset_key!r}; expected {expected_asset}"
                    )
                scale = entry.get("scale")
                offset = entry.get("offset")
                transform_applied = bool(entry.get("transform_applied"))
            elif legacy_values:
                asset_key = expected_asset
                values = legacy_values.get(expected_asset) or set()
                pairs = {(scale, offset) for scale, offset, _nodata in values}
                if len(pairs) != 1:
                    raise ValueError(
                        f"Cannot resolve one scale/offset pair for {expected_asset} from "
                        "the extraction's exact STAC sources"
                    )
                scale, offset = pairs.pop()
            else:
                asset_key = str(raster_band.get("description") or expected_asset)
                has_gdal_transform = "scale" in raster_band or "offset" in raster_band
                scale = raster_band.get("scale", 1.0)
                offset = raster_band.get("offset", 0.0)
                if not has_gdal_transform and str(
                    raster_band.get("description") or ""
                ).casefold() in {
                    "b04_10m",
                    "b08_10m",
                }:
                    raise ValueError(
                        "CDSE L2A reflectance bands require durable scale/offset provenance; "
                        "provide the TerraVault extraction manifest or embedded band contract"
                    )
                if has_gdal_transform:
                    source = "gdal_band_metadata"
            if not transform_applied and (scale is None or offset is None):
                raise ValueError(f"Incomplete reflectance transform for {expected_asset}")
            calculation_scale = 1.0 if transform_applied else float(scale)
            calculation_offset = 0.0 if transform_applied else float(offset)
            if (
                not math.isfinite(calculation_scale)
                or calculation_scale == 0
                or not math.isfinite(calculation_offset)
            ):
                raise ValueError(
                    f"Invalid reflectance transform for {expected_asset}: "
                    f"scale={calculation_scale!r}, offset={calculation_offset!r}"
                )
            reflectance[logical_name] = {
                "band": number,
                "asset_key": asset_key,
                "scale": None if scale is None else float(scale),
                "offset": None if offset is None else float(offset),
                "transform_applied": transform_applied,
                "calculation_scale": calculation_scale,
                "calculation_offset": calculation_offset,
            }

        cloud: dict[str, Any] | None = None
        if self.config.mask_quality:
            if contract is not None:
                expected_quality = {
                    bands["scl"]: "SCL_20m",
                    bands["cloud"]: "CLD_20m",
                }
                for number, expected_asset in expected_quality.items():
                    entry = entries.get(number)
                    asset_key = "" if entry is None else str(entry.get("asset_key") or "")
                    if asset_key.casefold() != expected_asset.casefold():
                        raise ValueError(
                            f"Band {number} is contracted as {asset_key!r}; "
                            f"expected {expected_asset}"
                        )
            cloud_number = bands["cloud"]
            cloud_entry = entries.get(cloud_number)
            zero_is_valid = False
            if cloud_entry is not None:
                zero_is_valid = bool(cloud_entry.get("zero_is_valid"))
                asset_key = str(cloud_entry.get("asset_key") or "CLD_20m")
            else:
                asset_key = "CLD_20m"
                values = legacy_values.get(asset_key) or set()
                zero_is_valid = bool(values) and all(nodata is None for _s, _o, nodata in values)
            legacy_collision = False
            if extraction is not None and sidecar_contract is None and zero_is_valid:
                try:
                    legacy_collision = float(extraction.get("nodata")) == 0
                except (TypeError, ValueError):
                    legacy_collision = False
            cloud = {
                "band": cloud_number,
                "asset_key": asset_key,
                "zero_is_valid": zero_is_valid,
                "legacy_zero_nodata_recovery": legacy_collision,
                "input_nodata": self._band_nodata(metadata, cloud_number),
            }

        return {
            "schema_version": 1,
            "source": source,
            "extraction_manifest": (
                None if extraction_manifest is None else str(extraction_manifest.resolve())
            ),
            "reflectance": reflectance,
            "cloud": cloud,
            "provenance_files": [str(path) for path in sorted(set(provenance_files))],
        }

    def _force_job_manifest(self) -> Path | None:
        path = self.config.input_path
        if path.parent.name != "mosaic" or path.parent.parent.name != "datacube":
            return None
        candidate = (
            path.parent.parent.parent / "_terravault" / "force" / "jobs" / f"{path.stem}.json"
        )
        return candidate if candidate.is_file() else None

    def _crop_bounds(self) -> tuple[float, float, float, float] | None:
        if not self.config.crop_to_force_input:
            return None
        force_manifest = self._force_job_manifest()
        if force_manifest is None:
            return None
        payload = json.loads(force_manifest.read_text(encoding="utf-8"))
        source_path = Path(payload.get("input_path") or "")
        if not source_path.is_file():
            logger.warning(
                "Original FORCE input is unavailable; retaining complete tile extent – path=%s",
                source_path,
            )
            return None
        metadata = self._inspect(source_path)
        corners = metadata.get("cornerCoordinates") or {}
        upper_left = corners.get("upperLeft")
        lower_right = corners.get("lowerRight")
        if not upper_left or not lower_right:
            return None
        transform = metadata.get("geoTransform") or []
        epsilon_x = abs(float(transform[1])) * 1e-6 if len(transform) == 6 else 0.0
        epsilon_y = abs(float(transform[5])) * 1e-6 if len(transform) == 6 else 0.0
        return (
            float(upper_left[0]),
            float(upper_left[1]),
            float(lower_right[0]) - epsilon_x,
            float(lower_right[1]) + epsilon_y,
        )

    def _native_force_manifest(self) -> Path | None:
        if self.config.force_qai_path is None:
            return None
        path = self.config.force_qai_path
        for directory in (path.parent, *path.parents):
            if directory.parent.name != "products":
                continue
            output_root = directory.parent.parent.parent
            candidate = output_root / "_terravault" / "force-l2" / "jobs" / f"{directory.name}.json"
            if candidate.is_file():
                return candidate
        return None

    def _force_temporal_match(self) -> dict[str, Any]:
        """Validate full L1C/L2A acquisition membership for the QAI."""

        if self.config.force_qai_path is None:
            return {"status": "not_requested"}
        native_manifest = self._native_force_manifest()
        force_identities: set[tuple[str, str, str, str]] = set()
        if native_manifest is not None:
            native = json.loads(native_manifest.read_text(encoding="utf-8"))
            if native.get("status") != "complete":
                raise ValueError(f"Native FORCE manifest is not complete: {native_manifest}")
            declared_paths = {Path(str(path)).resolve() for path in (native.get("qai_paths") or [])}
            declared_mosaic = native.get("qai_mosaic_path")
            if declared_mosaic:
                declared_paths.add(Path(str(declared_mosaic)).resolve())
            if self.config.force_qai_path.resolve() not in declared_paths:
                raise ValueError("FORCE QAI is not declared by its native processing manifest")
            identity = native.get("product_identity") or {}
            fields = (
                "platform",
                "sensing_time",
                "relative_orbit",
                "mgrs_tile",
            )
            if all(identity.get(field) for field in fields):
                force_identities.add(tuple(str(identity[field]) for field in fields))

        force_manifest = self._force_job_manifest()
        source_identities: set[tuple[str, str, str, str]] = set()
        extraction_manifest: Path | None = None
        if force_manifest is not None:
            job = json.loads(force_manifest.read_text(encoding="utf-8"))
            source_path = Path(str(job.get("input_path") or ""))
            candidate = source_path.with_suffix(f"{source_path.suffix}.manifest.json")
            if candidate.is_file():
                extraction_manifest = candidate
                extraction = json.loads(candidate.read_text(encoding="utf-8"))
                for source in extraction.get("sources") or []:
                    item_id = str(source.get("item_id") or "")
                    item_match = _SENTINEL_PRODUCT_NAME.match(item_id)
                    if item_match is not None:
                        source_identities.add(
                            (
                                item_match.group("platform"),
                                item_match.group("sensing"),
                                item_match.group("orbit"),
                                item_match.group("tile"),
                            )
                        )
        result = {
            "status": "not_available",
            "native_force_manifest": (None if native_manifest is None else str(native_manifest)),
            "force_identities": [
                {
                    "platform": platform,
                    "sensing_time": sensing,
                    "relative_orbit": orbit,
                    "mgrs_tile": tile,
                }
                for platform, sensing, orbit, tile in sorted(force_identities)
            ],
            "source_identities": [
                {
                    "platform": platform,
                    "sensing_time": sensing,
                    "relative_orbit": orbit,
                    "mgrs_tile": tile,
                }
                for platform, sensing, orbit, tile in sorted(source_identities)
            ],
            "extraction_manifest": (
                None if extraction_manifest is None else str(extraction_manifest)
            ),
        }
        if not force_identities or not source_identities:
            if not self.config.allow_force_time_mismatch:
                raise ValueError(
                    "Cannot verify full FORCE QAI acquisition provenance; use a QAI "
                    "inside a completed TerraVault force-level2 product and an L2A "
                    "mosaic with extraction provenance, or pass "
                    "--allow-force-time-mismatch for an intentional diagnostic"
                )
            return result
        matched = source_identities == force_identities
        result["status"] = "matched" if matched else "mismatched"
        if not matched and not self.config.allow_force_time_mismatch:
            raise ValueError(
                "FORCE QAI product membership does not match the L2A extraction; "
                "pass --allow-force-time-mismatch only for an intentional diagnostic"
            )
        return result

    def _align_force_qai(
        self,
        *,
        reference_metadata: dict[str, Any],
        output_path: Path,
    ) -> dict[str, Any]:
        assert self.config.force_qai_path is not None
        width, height = (int(value) for value in reference_metadata["size"])
        transform = reference_metadata.get("geoTransform") or []
        if len(transform) != 6:
            raise ValueError("NDVI reference has no affine transform for QAI alignment")
        if not (
            math.isclose(float(transform[2]), 0.0, abs_tol=1e-12)
            and math.isclose(float(transform[4]), 0.0, abs_tol=1e-12)
            and float(transform[1]) > 0
            and float(transform[5]) < 0
        ):
            raise ValueError("NDVI reference must be a north-up grid for QAI alignment")
        left = float(transform[0])
        top = float(transform[3])
        right = left + width * float(transform[1])
        bottom = top + height * float(transform[5])
        wkt = reference_metadata.get("coordinateSystem", {}).get("wkt")
        if not wkt:
            raise ValueError("NDVI reference has no CRS for QAI alignment")
        if (
            _FORCE_QAI_NAME.search(self.config.force_qai_path.name) is None
            and not self.config.allow_force_time_mismatch
        ):
            raise ValueError(
                "FORCE QAI filename is not recognized; pass "
                "--allow-force-time-mismatch only for an intentional diagnostic"
            )
        source_metadata = self._inspect(self.config.force_qai_path)
        source_bands = source_metadata.get("bands") or []
        if len(source_bands) != 1:
            raise ValueError("FORCE QAI input must contain exactly one bit-packed band")
        source_band = source_bands[0]
        if source_band.get("type") not in {"Int16", "UInt16"}:
            raise ValueError("FORCE QAI input must be a signed or unsigned 16-bit integer")
        if source_band.get("noDataValue") != 1:
            raise ValueError("FORCE QAI input must use the native nodata bit value 1")
        command = (
            self._gdal["gdalwarp"],
            "-overwrite",
            "-of",
            "GTiff",
            "-r",
            "near",
            "-srcnodata",
            "1",
            "-t_srs",
            str(wkt),
            "-te",
            str(left),
            str(bottom),
            str(right),
            str(top),
            "-ts",
            str(width),
            str(height),
            "-dstnodata",
            "1",
            "-co",
            "TILED=YES",
            "-co",
            "COMPRESS=DEFLATE",
            str(self.config.force_qai_path),
            str(output_path),
        )
        self._execute(command)
        result = {
            "source_path": str(self.config.force_qai_path),
            "source_size": source_metadata.get("size"),
            "aligned_size": [width, height],
            "resampling": "nearest",
            "destination_nodata": 1,
            "target_bounds": [
                left,
                bottom,
                right,
                top,
            ],
        }
        if self.config.dry_run:
            result["status"] = "planned"
            return result
        aligned = self._inspect(output_path)
        if [int(value) for value in aligned.get("size") or []] != [width, height]:
            raise RuntimeError("Aligned FORCE QAI does not match the NDVI pixel grid")
        aligned_wkt = aligned.get("coordinateSystem", {}).get("wkt")
        if not _same_crs(aligned_wkt, wkt):
            raise RuntimeError("Aligned FORCE QAI does not match the NDVI CRS")
        reference_transform = reference_metadata.get("geoTransform") or []
        aligned_transform = aligned.get("geoTransform") or []
        if (
            len(reference_transform) != 6
            or len(aligned_transform) != 6
            or any(
                abs(float(reference) - float(actual)) > 1e-8
                for reference, actual in zip(reference_transform, aligned_transform)
            )
        ):
            raise RuntimeError("Aligned FORCE QAI does not match the NDVI geotransform")
        aligned_bands = aligned.get("bands") or []
        if (
            len(aligned_bands) != 1
            or aligned_bands[0].get("type") not in {"Int16", "UInt16"}
            or aligned_bands[0].get("noDataValue") != 1
        ):
            raise RuntimeError("Aligned FORCE QAI lost its 16-bit/nodata semantics")
        result["status"] = "aligned"
        return result

    def _input_files(self, band_semantics: dict[str, Any]) -> list[Path]:
        paths = {self.config.input_path}
        if self.config.input_path.suffix.casefold() == ".vrt":
            tree = ET.parse(self.config.input_path)
            for source in tree.findall(".//SourceFilename"):
                if not source.text:
                    continue
                path = Path(source.text)
                if source.get("relativeToVRT") == "1":
                    path = self.config.input_path.parent / path
                paths.add(path.resolve())
        force_manifest = self._force_job_manifest()
        if force_manifest is not None:
            paths.add(force_manifest.resolve())
            job = json.loads(force_manifest.read_text(encoding="utf-8"))
            source_path = Path(str(job.get("input_path") or ""))
            extraction_manifest = source_path.with_suffix(f"{source_path.suffix}.manifest.json")
            if extraction_manifest.is_file():
                paths.add(extraction_manifest.resolve())
        native_force_manifest = self._native_force_manifest()
        if native_force_manifest is not None:
            paths.add(native_force_manifest.resolve())
        if self.config.force_qai_path is not None:
            paths.add(self.config.force_qai_path)
            if self.config.force_qai_path.suffix.casefold() == ".vrt":
                tree = ET.parse(self.config.force_qai_path)
                for source in tree.findall(".//SourceFilename"):
                    if not source.text:
                        continue
                    path = Path(source.text)
                    if source.get("relativeToVRT") == "1":
                        path = self.config.force_qai_path.parent / path
                    paths.add(path.resolve())
        paths.update(Path(path) for path in band_semantics.get("provenance_files") or [])
        return sorted(paths)

    def _fingerprint(
        self,
        band_semantics: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        files: list[dict[str, Any]] = []
        for path in self._input_files(band_semantics):
            if not path.is_file():
                raise FileNotFoundError(f"Visualization source does not exist: {path}")
            stat = path.stat()
            files.append(
                {
                    "path": str(path),
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        payload: dict[str, Any] = {
            "algorithm_version": VISUALIZATION_ALGORITHM_VERSION,
            "files": files,
            "band_semantics": band_semantics,
            "mask_quality": self.config.mask_quality,
            "cloud_threshold": self.config.cloud_threshold,
            "invalid_scl_classes": self.config.invalid_scl_classes,
            "quicklook_width": self.config.quicklook_width,
            "debug_plot": self.config.debug_plot,
            "debug_plot_width": self.config.debug_plot_width,
            "crop_to_force_input": self.config.crop_to_force_input,
            "bands": {
                "red": self.config.red_band,
                "nir": self.config.nir_band,
                "scl": self.config.scl_band,
                "cloud": self.config.cloud_band,
            },
            "palette": NDVI_COLORS,
        }
        if self.config.force_qai_path is not None:
            payload.update(
                {
                    "force_qai_path": str(self.config.force_qai_path),
                    "force_qai_mask": self.config.force_qai_mask,
                    "allow_force_time_mismatch": (self.config.allow_force_time_mismatch),
                }
            )
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return fingerprint, files

    def _existing_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    @contextmanager
    def _processing_state(
        self,
        *,
        fingerprint: str,
        input_files: list[dict[str, Any]],
        band_semantics: dict[str, Any],
    ) -> Iterator[None]:
        payload = {
            "schema_version": 4,
            "algorithm_version": VISUALIZATION_ALGORITHM_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": "processing",
            "error": None,
            "fingerprint": fingerprint,
            "input_path": str(self.config.input_path),
            "input_files": input_files,
            "band_semantics": band_semantics,
            "ndvi_path": str(self.ndvi_path),
            "quicklook_path": str(self.quicklook_path),
            "force_ndvi_path": (
                None if self.force_ndvi_path is None else str(self.force_ndvi_path)
            ),
            "force_quicklook_path": (
                None if self.force_quicklook_path is None else str(self.force_quicklook_path)
            ),
            "debug_plot_path": (
                None if self.debug_plot_path is None else str(self.debug_plot_path)
            ),
            "commands": [list(command) for command in self._commands],
        }
        _write_json_atomic(self.manifest_path, payload)
        try:
            yield
        except BaseException as exc:
            payload.update(
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "status": (
                        "interrupted"
                        if isinstance(exc, (KeyboardInterrupt, SystemExit))
                        else "failed"
                    ),
                    "error": str(exc),
                    "commands": [list(command) for command in self._commands],
                }
            )
            _write_json_atomic(self.manifest_path, payload)
            raise

    @staticmethod
    def _palette_text() -> str:
        lines = [
            f"{value} {red} {green} {blue} {alpha}"
            for value, red, green, blue, alpha, _label in NDVI_COLORS
        ]
        lines.append("nv 0 0 0 0")
        return "\n".join(lines) + "\n"

    def _calculation(
        self,
        *,
        bands: dict[str, int],
        metadata: dict[str, Any],
        band_semantics: dict[str, Any],
        raw_path: Path,
        crop_bounds: tuple[float, float, float, float] | None,
        mask_quality: bool,
        force_qai_path: Path | None = None,
    ) -> list[str]:
        red_nodata = self._band_nodata(metadata, bands["red"])
        nir_nodata = self._band_nodata(metadata, bands["nir"])
        red_contract = band_semantics["reflectance"]["red"]
        nir_contract = band_semantics["reflectance"]["nir"]
        red = (
            f"((B.astype(float32)*{red_contract['calculation_scale']!r})"
            f"+{red_contract['calculation_offset']!r})"
        )
        nir = (
            f"((A.astype(float32)*{nir_contract['calculation_scale']!r})"
            f"+{nir_contract['calculation_offset']!r})"
        )
        denominator = f"({nir}+{red})"
        conditions = [
            f"A!={nir_nodata}",
            f"B!={red_nodata}",
            f"{denominator}!=0",
        ]
        command = [
            self._gdal["gdal_calc.py"],
            "-A",
            str(self.config.input_path),
            f"--A_band={bands['nir']}",
            "-B",
            str(self.config.input_path),
            f"--B_band={bands['red']}",
        ]
        if mask_quality:
            scl_nodata = self._band_nodata(metadata, bands["scl"])
            cloud_nodata = self._band_nodata(metadata, bands["cloud"])
            invalid = ",".join(str(value) for value in self.config.invalid_scl_classes)
            cloud_contract = band_semantics.get("cloud") or {}
            cloud = "D"
            if cloud_contract.get("legacy_zero_nodata_recovery"):
                cloud = f"where(D=={cloud_nodata},0,D)"
            else:
                conditions.append(f"D!={cloud_nodata}")
            conditions.extend(
                (
                    f"C!={scl_nodata}",
                    f"{cloud}<={self.config.cloud_threshold}",
                    f"logical_not(isin(C,[{invalid}]))",
                )
            )
            command.extend(
                (
                    "-C",
                    str(self.config.input_path),
                    f"--C_band={bands['scl']}",
                    "-D",
                    str(self.config.input_path),
                    f"--D_band={bands['cloud']}",
                )
            )
        if force_qai_path is not None:
            conditions.append(f"bitwise_and(E.astype(uint16),{self.config.force_qai_mask})==0")
            command.extend(("-E", str(force_qai_path), "--E_band=1"))
        expression = (
            f"where(logical_and.reduce(({','.join(conditions)})),({nir}-{red})/{denominator},-9999)"
        )
        command.extend(
            (
                f"--outfile={raw_path}",
                f"--calc={expression}",
                "--NoDataValue=-9999",
                "--type=Float32",
                "--format=GTiff",
                "--co=TILED=YES",
                "--co=COMPRESS=DEFLATE",
                "--co=BIGTIFF=YES",
                "--hideNoData",
                "--overwrite",
                "--quiet",
            )
        )
        if crop_bounds is not None:
            command.extend(("--projwin", *(str(value) for value in crop_bounds)))
        return command

    @staticmethod
    def _statistics(metadata: dict[str, Any]) -> tuple[dict[str, Any], float | None]:
        statistics = (metadata.get("bands") or [{}])[0].get("metadata", {}).get("", {})
        value = statistics.get("STATISTICS_VALID_PERCENT")
        return statistics, None if value is None else float(value)

    @staticmethod
    def _palette_color(value: float) -> tuple[int, int, int]:
        points = [
            (stop, (red, green, blue)) for stop, red, green, blue, _alpha, _label in NDVI_COLORS
        ]
        if value <= points[0][0]:
            return points[0][1]
        for (lower, lower_color), (upper, upper_color) in zip(points, points[1:]):
            if value <= upper:
                fraction = (value - lower) / (upper - lower)
                return tuple(
                    round(start + fraction * (end - start))
                    for start, end in zip(lower_color, upper_color)
                )
        return points[-1][1]

    def _write_ndvi_cog(
        self,
        *,
        calculated_path: Path,
        output_path: Path,
        product_name: str,
    ) -> None:
        self._execute(
            (
                self._gdal["gdal_translate"],
                "-of",
                "COG",
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "BIGTIFF=YES",
                "-co",
                "BLOCKSIZE=512",
                "-co",
                "NUM_THREADS=1",
                "-mo",
                f"TERRAVAULT_PRODUCT={product_name}",
                "-mo",
                f"TERRAVAULT_SOURCE={self.config.input_path}",
                str(calculated_path),
                str(output_path),
            )
        )

    def _write_quicklook(
        self,
        *,
        ndvi_path: Path,
        palette_path: Path,
        color_path: Path,
        output_path: Path,
        width: int,
        worldfile: bool,
    ) -> Path | None:
        self._execute(
            (
                self._gdal["gdaldem"],
                "color-relief",
                "-alpha",
                "-of",
                "GTiff",
                "-co",
                "TILED=YES",
                "-co",
                "COMPRESS=DEFLATE",
                str(ndvi_path),
                str(palette_path),
                str(color_path),
            )
        )
        command = [
            self._gdal["gdal_translate"],
            "-of",
            "PNG",
            "-outsize",
            str(width),
            "0",
            "-r",
            "average",
        ]
        if worldfile:
            command.extend(("-co", "WORLDFILE=YES"))
        command.extend((str(color_path), str(output_path)))
        self._execute(command)
        generated_worldfile = output_path.with_suffix(".wld") if worldfile else None
        if (
            generated_worldfile is not None
            and not self.config.dry_run
            and not generated_worldfile.is_file()
        ):
            raise RuntimeError("GDAL did not create the PNG world file")
        return generated_worldfile

    def _compose_debug_plot(
        self,
        *,
        raw_quicklook_path: Path,
        masked_quicklook_path: Path,
        force_quicklook_path: Path | None,
        output_path: Path,
        raw_valid_percent: float | None,
        valid_percent: float | None,
        force_valid_percent: float | None,
    ) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:
            raise RuntimeError(
                "Pillow is required for the before/after debug plot; install "
                'TerraVault with `pip install "terravault[visualization]"` or '
                "pass --no-debug-plot"
            ) from exc

        canvas_width = self.config.debug_plot_width
        padding = 32
        panel_specs = [
            (raw_quicklook_path, "RAW · B04/B08 NDVI", raw_valid_percent),
            (
                masked_quicklook_path,
                "CDSE · SCL + CLD" if self.config.mask_quality else "CDSE · mask disabled",
                valid_percent,
            ),
        ]
        if force_quicklook_path is not None:
            panel_specs.append((force_quicklook_path, "FORCE · QAI", force_valid_percent))
        panel_count = len(panel_specs)
        gap = 16 if panel_count == 3 else 20
        panel_width = (canvas_width - (2 * padding) - (gap * (panel_count - 1))) // panel_count

        def font(size: int):
            try:
                return ImageFont.load_default(size=size)
            except TypeError:
                # Pillow 10.0 does not yet accept the size keyword. Prefer a
                # scalable bundled font, then retain functional legacy output.
                try:
                    return ImageFont.truetype("DejaVuSans.ttf", size=size)
                except OSError:
                    return ImageFont.load_default()

        def panel(path: Path):
            with Image.open(path) as source:
                image = source.convert("RGBA")
            target_height = max(1, round(image.height * panel_width / image.width))
            image = image.resize(
                (panel_width, target_height),
                Image.Resampling.LANCZOS,
            )
            checker = Image.new("RGBA", image.size, (230, 233, 237, 255))
            checker_draw = ImageDraw.Draw(checker)
            square = 16
            for y in range(0, image.height, square):
                for x in range(0, image.width, square):
                    if (x // square + y // square) % 2:
                        checker_draw.rectangle(
                            (x, y, x + square - 1, y + square - 1),
                            fill=(210, 215, 221, 255),
                        )
            return Image.alpha_composite(checker, image).convert("RGB")

        panels = [panel(path) for path, _label, _valid in panel_specs]
        panel_height = max(image.height for image in panels)
        panel_y = 126
        statistic_y = panel_y + panel_height + 14
        legend_y = statistic_y + 58
        canvas_height = legend_y + 84
        canvas = Image.new("RGB", (canvas_width, canvas_height), (20, 25, 32))
        draw = ImageDraw.Draw(canvas)
        title_font = font(28)
        heading_font = font(18 if panel_count == 3 else 20)
        body_font = font(16)
        muted = (181, 190, 201)
        title = "NDVI cloud-mask comparison" if panel_count == 3 else "NDVI quality-mask diagnostic"
        draw.text((padding, 20), title, fill="white", font=title_font)
        draw.text(
            (padding, 58),
            self.config.input_path.name,
            fill=muted,
            font=body_font,
        )
        for index, ((_, label, percentage), image) in enumerate(zip(panel_specs, panels)):
            x = padding + index * (panel_width + gap)
            draw.text(
                (x, 94),
                label,
                fill=(238, 242, 247),
                font=heading_font,
            )
            canvas.paste(image, (x, panel_y))
            valid_text = "valid: unavailable" if percentage is None else f"valid: {percentage:.2f}%"
            draw.text((x, statistic_y), valid_text, fill=muted, font=body_font)
            if index > 0 and raw_valid_percent is not None and percentage is not None:
                removed = max(0.0, raw_valid_percent - percentage)
                draw.text(
                    (x, statistic_y + 22),
                    f"masked vs raw: {removed:.2f} pp",
                    fill=muted,
                    font=body_font,
                )

        legend_left = padding
        legend_right = canvas_width - padding
        legend_top = legend_y + 2
        for x in range(legend_left, legend_right):
            fraction = (x - legend_left) / max(1, legend_right - legend_left - 1)
            value = -1.0 + (2.0 * fraction)
            draw.line(
                (x, legend_top, x, legend_top + 18),
                fill=self._palette_color(value),
            )
        draw.rectangle(
            (legend_left, legend_top, legend_right - 1, legend_top + 18),
            outline=(105, 114, 126),
        )
        ticks = ((-1.0, "-1"), (-0.5, "-0.5"), (0.0, "0"), (0.5, "0.5"), (1.0, "1"))
        for value, label in ticks:
            x = legend_left + round(((value + 1.0) / 2.0) * (legend_right - legend_left - 1))
            draw.line((x, legend_top + 19, x, legend_top + 25), fill=muted)
            text_box = draw.textbbox((0, 0), label, font=body_font)
            text_width = text_box[2] - text_box[0]
            draw.text(
                (x - text_width // 2, legend_top + 29),
                label,
                fill=muted,
                font=body_font,
            )
        draw.text(
            (legend_left, legend_top + 52),
            "water / low NDVI",
            fill=muted,
            font=body_font,
        )
        right_label = "dense vegetation"
        right_box = draw.textbbox((0, 0), right_label, font=body_font)
        draw.text(
            (legend_right - (right_box[2] - right_box[0]), legend_top + 52),
            right_label,
            fill=muted,
            font=body_font,
        )
        canvas.save(output_path, format="PNG", optimize=True)

    def _product_paths(self) -> list[Path]:
        paths = [self.ndvi_path, self.quicklook_path, self.worldfile_path]
        for path in (
            self.force_ndvi_path,
            self.force_quicklook_path,
            self.force_worldfile_path,
            self.debug_plot_path,
        ):
            if path is not None:
                paths.append(path)
        return paths

    @staticmethod
    def _output_records(paths: Sequence[Path]) -> list[dict[str, Any]]:
        records = []
        for path in paths:
            stat = path.stat()
            records.append(
                {
                    "path": str(path),
                    "bytes": stat.st_size,
                    "sha256": None if path.suffix.casefold() == ".tif" else _sha256(path),
                }
            )
        return records

    def _outputs_valid(
        self,
        existing: dict[str, Any],
        product_paths: Sequence[Path],
    ) -> bool:
        if (
            existing.get("schema_version") != 4
            or existing.get("algorithm_version") != VISUALIZATION_ALGORITHM_VERSION
        ):
            return False
        records = existing.get("output_files")
        if not isinstance(records, list):
            return False
        by_path = {
            str(record.get("path")): record for record in records if isinstance(record, dict)
        }
        if set(by_path) != {str(path) for path in product_paths}:
            return False
        for path in product_paths:
            record = by_path[str(path)]
            if not path.is_file() or path.stat().st_size <= 0:
                return False
            if path.stat().st_size != record.get("bytes"):
                return False
            digest = record.get("sha256")
            if digest is not None and _sha256(path) != digest:
                return False
            suffix = path.suffix.casefold()
            if suffix == ".png":
                with path.open("rb") as stream:
                    if stream.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
                        return False
            elif suffix == ".wld":
                try:
                    values = [float(line) for line in path.read_text(encoding="utf-8").splitlines()]
                except (OSError, ValueError):
                    return False
                if len(values) != 6 or not all(math.isfinite(value) for value in values):
                    return False
            elif suffix == ".tif":
                try:
                    metadata = self._inspect(path)
                except (OSError, RuntimeError, json.JSONDecodeError):
                    return False
                bands = metadata.get("bands") or []
                if (
                    len(bands) != 1
                    or bands[0].get("type") != "Float32"
                    or bands[0].get("noDataValue") != -9999
                    or not metadata.get("coordinateSystem", {}).get("wkt")
                    or [int(value) for value in metadata.get("size") or []]
                    != [int(existing.get("width") or 0), int(existing.get("height") or 0)]
                    or (metadata.get("metadata") or {}).get("IMAGE_STRUCTURE", {}).get("LAYOUT")
                    != "COG"
                ):
                    return False
        return True

    def _result(
        self,
        *,
        status: str,
        width: int,
        height: int,
        valid_percent: float | None,
        raw_valid_percent: float | None = None,
        force_valid_percent: float | None = None,
        skipped: bool = False,
    ) -> ForceVisualizationResult:
        return ForceVisualizationResult(
            status=status,
            ndvi_path=self.ndvi_path,
            quicklook_path=self.quicklook_path,
            force_ndvi_path=self.force_ndvi_path,
            force_quicklook_path=self.force_quicklook_path,
            force_worldfile_path=self.force_worldfile_path,
            debug_plot_path=self.debug_plot_path,
            worldfile_path=self.worldfile_path,
            manifest_path=self.manifest_path,
            width=width,
            height=height,
            valid_percent=valid_percent,
            raw_valid_percent=raw_valid_percent,
            force_valid_percent=force_valid_percent,
            commands=tuple(self._commands),
            skipped=skipped,
        )

    def run(self) -> ForceVisualizationResult:
        """Create or reuse the NDVI visualization products."""

        if not self.config.input_path.is_file():
            raise FileNotFoundError(f"FORCE mosaic does not exist: {self.config.input_path}")
        metadata = self._inspect(self.config.input_path)
        if not metadata.get("coordinateSystem", {}).get("wkt"):
            raise ValueError("FORCE visualization input is not georeferenced")
        bands = self._bands(metadata)
        band_semantics = self._band_semantics(metadata, bands)
        temporal_match = self._force_temporal_match()
        fingerprint, input_files = self._fingerprint(band_semantics)
        existing = self._existing_manifest()
        product_paths = self._product_paths()
        if (
            existing
            and existing.get("status") == "complete"
            and existing.get("fingerprint") == fingerprint
            and self._outputs_valid(existing, product_paths)
            and not self.config.overwrite
        ):
            logger.info(
                "FORCE visualization already complete; skipping unchanged mosaic – manifest=%s",
                self.manifest_path,
            )
            return self._result(
                status="complete",
                width=int(existing["width"]),
                height=int(existing["height"]),
                valid_percent=existing.get("valid_percent"),
                raw_valid_percent=existing.get("raw_valid_percent"),
                force_valid_percent=existing.get("force_valid_percent"),
                skipped=True,
            )
        if any(path.exists() for path in product_paths) and not self.config.overwrite:
            if existing is None or existing.get("fingerprint") != fingerprint:
                stale_algorithm = (
                    existing is not None
                    and existing.get("input_path") == str(self.config.input_path)
                    and existing.get("algorithm_version") != VISUALIZATION_ALGORITHM_VERSION
                )
                if not stale_algorithm:
                    raise FileExistsError(
                        f"Visualization outputs already exist in {self.config.output_dir}; "
                        "pass overwrite to replace changed products"
                    )
            logger.warning(
                "Repairing incomplete visualization publication for unchanged input – manifest=%s",
                self.manifest_path,
            )

        crop_bounds = self._crop_bounds()
        if self.config.dry_run:
            assert self.config.output_dir is not None
            planned_raw = self.config.output_dir / f".{self.config.output_stem}.raw.tif"
            self._execute(
                self._calculation(
                    bands=bands,
                    metadata=metadata,
                    band_semantics=band_semantics,
                    raw_path=planned_raw,
                    crop_bounds=crop_bounds,
                    mask_quality=self.config.mask_quality,
                )
            )
            if self.config.debug_plot and self.config.mask_quality:
                planned_before = (
                    self.config.output_dir / f".{self.config.output_stem}.before.raw.tif"
                )
                self._execute(
                    self._calculation(
                        bands=bands,
                        metadata=metadata,
                        band_semantics=band_semantics,
                        raw_path=planned_before,
                        crop_bounds=crop_bounds,
                        mask_quality=False,
                    )
                )
            if self.config.force_qai_path is not None:
                planned_qai = self.config.output_dir / f".{self.config.output_stem}.qai.tif"
                self._align_force_qai(
                    reference_metadata=metadata,
                    output_path=planned_qai,
                )
                planned_force = self.config.output_dir / f".{self.config.output_stem}.force.raw.tif"
                self._execute(
                    self._calculation(
                        bands=bands,
                        metadata=metadata,
                        band_semantics=band_semantics,
                        raw_path=planned_force,
                        crop_bounds=crop_bounds,
                        mask_quality=False,
                        force_qai_path=planned_qai,
                    )
                )
            return self._result(
                status="planned",
                width=int(metadata["size"][0]),
                height=int(metadata["size"][1]),
                valid_percent=None,
                force_valid_percent=None,
            )

        assert self.config.output_dir is not None
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        force_alignment: dict[str, Any] | None = None
        force_statistics: dict[str, Any] | None = None
        force_valid_percent: float | None = None
        with (
            self._processing_state(
                fingerprint=fingerprint,
                input_files=input_files,
                band_semantics=band_semantics,
            ),
            tempfile.TemporaryDirectory(
                prefix="terravault-force-viz-", dir=self.config.output_dir
            ) as temporary,
        ):
            temporary_path = Path(temporary)
            calculated_path = temporary_path / "ndvi-calculated.tif"
            ndvi_path = temporary_path / "ndvi.tif"
            color_path = temporary_path / "ndvi-color.tif"
            quicklook_path = temporary_path / "ndvi.png"
            before_path = temporary_path / "ndvi-before.tif"
            before_color_path = temporary_path / "ndvi-before-color.tif"
            before_quicklook_path = temporary_path / "ndvi-before.png"
            debug_plot_path = temporary_path / "ndvi-comparison.png"
            aligned_qai_path = temporary_path / "force-qai-aligned.tif"
            force_calculated_path = temporary_path / "ndvi-force-calculated.tif"
            force_ndvi_path = temporary_path / "ndvi-force.tif"
            force_color_path = temporary_path / "ndvi-force-color.tif"
            force_quicklook_path = temporary_path / "ndvi-force.png"
            palette_path = temporary_path / "ndvi-colors.txt"
            palette_path.write_text(self._palette_text(), encoding="utf-8")

            self._execute(
                self._calculation(
                    bands=bands,
                    metadata=metadata,
                    band_semantics=band_semantics,
                    raw_path=calculated_path,
                    crop_bounds=crop_bounds,
                    mask_quality=self.config.mask_quality,
                )
            )
            self._write_ndvi_cog(
                calculated_path=calculated_path,
                output_path=ndvi_path,
                product_name="NDVI_CDSE_QUALITY_MASK",
            )
            ndvi_metadata = self._inspect(ndvi_path, statistics=True)
            width, height = (int(value) for value in ndvi_metadata["size"])
            statistics, valid_percent = self._statistics(ndvi_metadata)
            quicklook_width = min(self.config.quicklook_width, width)
            generated_worldfile = self._write_quicklook(
                ndvi_path=ndvi_path,
                palette_path=palette_path,
                color_path=color_path,
                output_path=quicklook_path,
                width=quicklook_width,
                worldfile=True,
            )

            force_worldfile: Path | None = None
            if self.config.force_qai_path is not None:
                force_alignment = self._align_force_qai(
                    reference_metadata=metadata,
                    output_path=aligned_qai_path,
                )
                self._execute(
                    self._calculation(
                        bands=bands,
                        metadata=metadata,
                        band_semantics=band_semantics,
                        raw_path=force_calculated_path,
                        crop_bounds=crop_bounds,
                        mask_quality=False,
                        force_qai_path=aligned_qai_path,
                    )
                )
                self._write_ndvi_cog(
                    calculated_path=force_calculated_path,
                    output_path=force_ndvi_path,
                    product_name="NDVI_FORCE_QAI_MASK",
                )
                force_metadata = self._inspect(force_ndvi_path, statistics=True)
                force_size = tuple(int(value) for value in force_metadata["size"])
                if force_size != (width, height):
                    raise RuntimeError("FORCE-masked NDVI does not match the CDSE NDVI output grid")
                force_statistics, force_valid_percent = self._statistics(force_metadata)
                force_worldfile = self._write_quicklook(
                    ndvi_path=force_ndvi_path,
                    palette_path=palette_path,
                    color_path=force_color_path,
                    output_path=force_quicklook_path,
                    width=quicklook_width,
                    worldfile=True,
                )

            raw_valid_percent: float | None = None
            if self.debug_plot_path is not None:
                if self.config.mask_quality:
                    self._execute(
                        self._calculation(
                            bands=bands,
                            metadata=metadata,
                            band_semantics=band_semantics,
                            raw_path=before_path,
                            crop_bounds=crop_bounds,
                            mask_quality=False,
                        )
                    )
                    before_metadata = self._inspect(before_path, statistics=True)
                    _raw_statistics, raw_valid_percent = self._statistics(before_metadata)
                else:
                    before_path = calculated_path
                    raw_valid_percent = valid_percent
                self._write_quicklook(
                    ndvi_path=before_path,
                    palette_path=palette_path,
                    color_path=before_color_path,
                    output_path=before_quicklook_path,
                    width=quicklook_width,
                    worldfile=False,
                )
                self._compose_debug_plot(
                    raw_quicklook_path=before_quicklook_path,
                    masked_quicklook_path=quicklook_path,
                    force_quicklook_path=(
                        force_quicklook_path if self.config.force_qai_path is not None else None
                    ),
                    output_path=debug_plot_path,
                    raw_valid_percent=raw_valid_percent,
                    valid_percent=valid_percent,
                    force_valid_percent=force_valid_percent,
                )

            assert generated_worldfile is not None
            replacements = [
                (ndvi_path, self.ndvi_path),
                (quicklook_path, self.quicklook_path),
                (generated_worldfile, self.worldfile_path),
            ]
            if self.config.force_qai_path is not None:
                assert self.force_ndvi_path is not None
                assert self.force_quicklook_path is not None
                assert self.force_worldfile_path is not None
                assert force_worldfile is not None
                replacements.extend(
                    (
                        (force_ndvi_path, self.force_ndvi_path),
                        (force_quicklook_path, self.force_quicklook_path),
                        (force_worldfile, self.force_worldfile_path),
                    )
                )
            if self.debug_plot_path is not None:
                replacements.append((debug_plot_path, self.debug_plot_path))
            for source, destination in replacements:
                os.replace(source, destination)

        manifest = {
            "schema_version": 4,
            "algorithm_version": VISUALIZATION_ALGORITHM_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "complete",
            "fingerprint": fingerprint,
            "input_path": str(self.config.input_path),
            "input_files": input_files,
            "band_semantics": band_semantics,
            "force_job_manifest": (
                None if self._force_job_manifest() is None else str(self._force_job_manifest())
            ),
            "crop_bounds": None if crop_bounds is None else list(crop_bounds),
            "bands": bands,
            "mask_quality": self.config.mask_quality,
            "cloud_threshold": self.config.cloud_threshold,
            "invalid_scl_classes": list(self.config.invalid_scl_classes),
            "ndvi_formula": ("(physical_B08 - physical_B04) / (physical_B08 + physical_B04)"),
            "ndvi_path": str(self.ndvi_path),
            "quicklook_path": str(self.quicklook_path),
            "debug_plot_path": (
                None if self.debug_plot_path is None else str(self.debug_plot_path)
            ),
            "worldfile_path": str(self.worldfile_path),
            "width": width,
            "height": height,
            "valid_percent": valid_percent,
            "raw_valid_percent": raw_valid_percent,
            "masked_percentage_points": (
                None
                if raw_valid_percent is None or valid_percent is None
                else round(max(0.0, raw_valid_percent - valid_percent), 6)
            ),
            "statistics": statistics,
            "palette": [
                {
                    "value": value,
                    "rgba": [red, green, blue, alpha],
                    "label": label,
                }
                for value, red, green, blue, alpha, label in NDVI_COLORS
            ],
            "output_files": self._output_records(product_paths),
            "commands": [list(command) for command in self._commands],
        }
        if self.config.force_qai_path is not None:
            manifest.update(
                {
                    "force_qai_path": str(self.config.force_qai_path),
                    "force_qai_mask": self.config.force_qai_mask,
                    "force_qai_mask_hex": f"0x{self.config.force_qai_mask:04X}",
                    "force_qai_flags": [
                        {
                            "bits": bits,
                            "mask": field_mask,
                            "name": name,
                            "screened_mask": (self.config.force_qai_mask & field_mask),
                            "fully_screened": (self.config.force_qai_mask & field_mask)
                            == field_mask,
                        }
                        for bits, field_mask, name in FORCE_QAI_FIELDS
                    ],
                    "force_temporal_match": temporal_match,
                    "force_alignment": force_alignment,
                    "force_ndvi_path": str(self.force_ndvi_path),
                    "force_quicklook_path": str(self.force_quicklook_path),
                    "force_worldfile_path": str(self.force_worldfile_path),
                    "force_valid_percent": force_valid_percent,
                    "force_masked_percentage_points": (
                        None
                        if raw_valid_percent is None or force_valid_percent is None
                        else round(
                            max(0.0, raw_valid_percent - force_valid_percent),
                            6,
                        )
                    ),
                    "force_statistics": force_statistics,
                    "comparison_semantics": (
                        "All panels use the same L2A B04/B08 reflectance; only "
                        "the quality mask changes between raw, CDSE SCL+CLD, "
                        "and FORCE QAI."
                    ),
                }
            )
        _write_json_atomic(self.manifest_path, manifest)
        logger.info(
            "FORCE visualization complete – ndvi=%s quicklook=%s debug_plot=%s valid=%.2f%%",
            self.ndvi_path,
            self.quicklook_path,
            self.debug_plot_path,
            valid_percent or 0,
        )
        return self._result(
            status="complete",
            width=width,
            height=height,
            valid_percent=valid_percent,
            raw_valid_percent=raw_valid_percent,
            force_valid_percent=force_valid_percent,
        )
