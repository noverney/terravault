"""NDVI products and quicklooks from TerraVault FORCE feature mosaics.

The input is expected to use the standard TerraVault band order:
B04 (red), B08 (near infrared), SCL and CLD. Pixel processing stays in GDAL;
Python only validates metadata, assembles commands and records provenance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
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


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


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
    debug_plot_path: Path | None
    worldfile_path: Path
    manifest_path: Path
    width: int
    height: int
    valid_percent: float | None
    raw_valid_percent: float | None
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
        self.debug_plot_path = (
            config.output_dir / f"{config.output_stem}_before_after.png"
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
        for name in ("gdal_calc.py", "gdal_translate", "gdaldem", "gdalinfo"):
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
                f"Visualization command failed with exit code "
                f"{completed.returncode}: {detail}"
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

    def _bands(self, metadata: dict[str, Any]) -> dict[str, int]:
        result = {
            "red": self.config.red_band
            or self._described_band(metadata, "B04_10m", 1),
            "nir": self.config.nir_band
            or self._described_band(metadata, "B08_10m", 2),
            "scl": self.config.scl_band
            or self._described_band(metadata, "SCL_20m", 3),
            "cloud": self.config.cloud_band
            or self._described_band(metadata, "CLD_20m", 4),
        }
        band_count = len(metadata.get("bands") or [])
        required = ("red", "nir", "scl", "cloud") if self.config.mask_quality else ("red", "nir")
        invalid = [name for name in required if result[name] > band_count]
        if invalid:
            raise ValueError(
                f"Input has {band_count} bands; invalid mappings for: "
                + ", ".join(invalid)
            )
        return result

    @staticmethod
    def _band_nodata(metadata: dict[str, Any], band_number: int) -> float | int:
        bands = metadata.get("bands") or []
        value = bands[band_number - 1].get("noDataValue")
        if value is None:
            raise ValueError(f"Input band {band_number} has no nodata value")
        return value

    def _force_job_manifest(self) -> Path | None:
        path = self.config.input_path
        if path.parent.name != "mosaic" or path.parent.parent.name != "datacube":
            return None
        candidate = (
            path.parent.parent.parent
            / "_terravault"
            / "force"
            / "jobs"
            / f"{path.stem}.json"
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
                "Original FORCE input is unavailable; retaining complete tile extent – "
                "path=%s",
                source_path,
            )
            return None
        metadata = self._inspect(source_path)
        corners = metadata.get("cornerCoordinates") or {}
        upper_left = corners.get("upperLeft")
        lower_right = corners.get("lowerRight")
        if not upper_left or not lower_right:
            return None
        return (
            float(upper_left[0]),
            float(upper_left[1]),
            float(lower_right[0]),
            float(lower_right[1]),
        )

    def _input_files(self) -> list[Path]:
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
        return sorted(paths)

    def _fingerprint(self) -> tuple[str, list[dict[str, Any]]]:
        files: list[dict[str, Any]] = []
        for path in self._input_files():
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
        payload = {
            "files": files,
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
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return fingerprint, files

    def _existing_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

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
        raw_path: Path,
        crop_bounds: tuple[float, float, float, float] | None,
        mask_quality: bool,
    ) -> list[str]:
        red_nodata = self._band_nodata(metadata, bands["red"])
        nir_nodata = self._band_nodata(metadata, bands["nir"])
        conditions = [
            f"A!={nir_nodata}",
            f"B!={red_nodata}",
            "(A+B)>0",
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
            conditions.extend(
                (
                    f"C!={scl_nodata}",
                    f"D!={cloud_nodata}",
                    f"D<={self.config.cloud_threshold}",
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
        expression = (
            f"where(logical_and.reduce(({','.join(conditions)})),"
            "(A.astype(float32)-B)/(A+B),-9999)"
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
                "--overwrite",
                "--quiet",
            )
        )
        if crop_bounds is not None:
            command.extend(("--projwin", *(str(value) for value in crop_bounds)))
        return command

    @staticmethod
    def _statistics(metadata: dict[str, Any]) -> tuple[dict[str, Any], float | None]:
        statistics = (
            (metadata.get("bands") or [{}])[0]
            .get("metadata", {})
            .get("", {})
        )
        value = statistics.get("STATISTICS_VALID_PERCENT")
        return statistics, None if value is None else float(value)

    @staticmethod
    def _palette_color(value: float) -> tuple[int, int, int]:
        points = [
            (stop, (red, green, blue))
            for stop, red, green, blue, _alpha, _label in NDVI_COLORS
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

    def _compose_debug_plot(
        self,
        *,
        raw_quicklook_path: Path,
        masked_quicklook_path: Path,
        output_path: Path,
        raw_valid_percent: float | None,
        valid_percent: float | None,
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
        gap = 20
        panel_width = (canvas_width - (2 * padding) - gap) // 2

        def font(size: int):
            return ImageFont.load_default(size=size)

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

        before = panel(raw_quicklook_path)
        after = panel(masked_quicklook_path)
        panel_height = max(before.height, after.height)
        panel_y = 126
        statistic_y = panel_y + panel_height + 14
        legend_y = statistic_y + 52
        canvas_height = legend_y + 84
        canvas = Image.new("RGB", (canvas_width, canvas_height), (20, 25, 32))
        draw = ImageDraw.Draw(canvas)
        title_font = font(28)
        heading_font = font(20)
        body_font = font(16)
        muted = (181, 190, 201)
        draw.text((padding, 20), "NDVI quality-mask diagnostic", fill="white", font=title_font)
        draw.text(
            (padding, 58),
            self.config.input_path.name,
            fill=muted,
            font=body_font,
        )
        after_x = padding + panel_width + gap
        draw.text(
            (padding, 94),
            "BEFORE · raw B04/B08 NDVI",
            fill=(238, 242, 247),
            font=heading_font,
        )
        after_label = (
            "AFTER · SCL + CLD quality mask"
            if self.config.mask_quality
            else "AFTER · quality mask disabled"
        )
        draw.text(
            (after_x, 94),
            after_label,
            fill=(238, 242, 247),
            font=heading_font,
        )
        canvas.paste(before, (padding, panel_y))
        canvas.paste(after, (after_x, panel_y))

        raw_text = (
            "raw valid pixels: unavailable"
            if raw_valid_percent is None
            else f"raw valid pixels: {raw_valid_percent:.2f}%"
        )
        after_text = (
            "after valid pixels: unavailable"
            if valid_percent is None
            else f"after valid pixels: {valid_percent:.2f}%"
        )
        if raw_valid_percent is not None and valid_percent is not None:
            removed = max(0.0, raw_valid_percent - valid_percent)
            after_text += f"  ·  masked: {removed:.2f} percentage points"
        draw.text((padding, statistic_y), raw_text, fill=muted, font=body_font)
        draw.text((after_x, statistic_y), after_text, fill=muted, font=body_font)

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
        for value, label in ((-1.0, "-1"), (-0.5, "-0.5"), (0.0, "0"), (0.5, "0.5"), (1.0, "1")):
            x = legend_left + round(((value + 1.0) / 2.0) * (legend_right - legend_left - 1))
            draw.line((x, legend_top + 19, x, legend_top + 25), fill=muted)
            text_box = draw.textbbox((0, 0), label, font=body_font)
            text_width = text_box[2] - text_box[0]
            draw.text((x - text_width // 2, legend_top + 29), label, fill=muted, font=body_font)
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

    def _result(
        self,
        *,
        status: str,
        width: int,
        height: int,
        valid_percent: float | None,
        raw_valid_percent: float | None = None,
        skipped: bool = False,
    ) -> ForceVisualizationResult:
        return ForceVisualizationResult(
            status=status,
            ndvi_path=self.ndvi_path,
            quicklook_path=self.quicklook_path,
            debug_plot_path=self.debug_plot_path,
            worldfile_path=self.worldfile_path,
            manifest_path=self.manifest_path,
            width=width,
            height=height,
            valid_percent=valid_percent,
            raw_valid_percent=raw_valid_percent,
            commands=tuple(self._commands),
            skipped=skipped,
        )

    def run(self) -> ForceVisualizationResult:
        """Create or reuse the NDVI visualization products."""

        if not self.config.input_path.is_file():
            raise FileNotFoundError(
                f"FORCE mosaic does not exist: {self.config.input_path}"
            )
        metadata = self._inspect(self.config.input_path)
        if not metadata.get("coordinateSystem", {}).get("wkt"):
            raise ValueError("FORCE visualization input is not georeferenced")
        bands = self._bands(metadata)
        fingerprint, input_files = self._fingerprint()
        existing = self._existing_manifest()
        product_paths = [self.ndvi_path, self.quicklook_path, self.worldfile_path]
        if self.debug_plot_path is not None:
            product_paths.append(self.debug_plot_path)
        products_exist = all(path.is_file() for path in product_paths)
        if (
            existing
            and existing.get("status") == "complete"
            and existing.get("fingerprint") == fingerprint
            and products_exist
            and not self.config.overwrite
        ):
            logger.info(
                "FORCE visualization already complete; skipping unchanged mosaic – "
                "manifest=%s",
                self.manifest_path,
            )
            return self._result(
                status="complete",
                width=int(existing["width"]),
                height=int(existing["height"]),
                valid_percent=existing.get("valid_percent"),
                raw_valid_percent=existing.get("raw_valid_percent"),
                skipped=True,
            )
        if any(path.exists() for path in product_paths) and not self.config.overwrite:
            raise FileExistsError(
                f"Visualization outputs already exist in {self.config.output_dir}; "
                "pass overwrite to replace changed products"
            )

        crop_bounds = self._crop_bounds()
        if self.config.dry_run:
            planned_raw = self.config.output_dir / f".{self.config.output_stem}.raw.tif"
            self._execute(
                self._calculation(
                    bands=bands,
                    metadata=metadata,
                    raw_path=planned_raw,
                    crop_bounds=crop_bounds,
                    mask_quality=self.config.mask_quality,
                )
            )
            if self.config.debug_plot and self.config.mask_quality:
                planned_before = (
                    self.config.output_dir
                    / f".{self.config.output_stem}.before.raw.tif"
                )
                self._execute(
                    self._calculation(
                        bands=bands,
                        metadata=metadata,
                        raw_path=planned_before,
                        crop_bounds=crop_bounds,
                        mask_quality=False,
                    )
                )
            return self._result(
                status="planned",
                width=int(metadata["size"][0]),
                height=int(metadata["size"][1]),
                valid_percent=None,
            )

        assert self.config.output_dir is not None
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="terravault-force-viz-",
            dir=self.config.output_dir,
        ) as temporary:
            temporary_path = Path(temporary)
            calculated_path = temporary_path / "ndvi-calculated.tif"
            ndvi_path = temporary_path / "ndvi.tif"
            color_path = temporary_path / "ndvi-color.tif"
            quicklook_path = temporary_path / "ndvi.png"
            before_path = temporary_path / "ndvi-before.tif"
            before_color_path = temporary_path / "ndvi-before-color.tif"
            before_quicklook_path = temporary_path / "ndvi-before.png"
            debug_plot_path = temporary_path / "ndvi-before-after.png"
            palette_path = temporary_path / "ndvi-colors.txt"
            palette_path.write_text(self._palette_text(), encoding="utf-8")

            self._execute(
                self._calculation(
                    bands=bands,
                    metadata=metadata,
                    raw_path=calculated_path,
                    crop_bounds=crop_bounds,
                    mask_quality=self.config.mask_quality,
                )
            )
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
                    "TERRAVAULT_PRODUCT=NDVI",
                    "-mo",
                    f"TERRAVAULT_SOURCE={self.config.input_path}",
                    str(calculated_path),
                    str(ndvi_path),
                )
            )
            ndvi_metadata = self._inspect(ndvi_path, statistics=True)
            width, height = (int(value) for value in ndvi_metadata["size"])
            statistics, valid_percent = self._statistics(ndvi_metadata)
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
            quicklook_width = min(self.config.quicklook_width, width)
            self._execute(
                (
                    self._gdal["gdal_translate"],
                    "-of",
                    "PNG",
                    "-outsize",
                    str(quicklook_width),
                    "0",
                    "-r",
                    "average",
                    "-co",
                    "WORLDFILE=YES",
                    str(color_path),
                    str(quicklook_path),
                )
            )
            generated_worldfile = quicklook_path.with_suffix(".wld")
            if not generated_worldfile.is_file():
                raise RuntimeError("GDAL did not create the PNG world file")

            raw_valid_percent: float | None = None
            if self.debug_plot_path is not None:
                if self.config.mask_quality:
                    self._execute(
                        self._calculation(
                            bands=bands,
                            metadata=metadata,
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
                        str(before_path),
                        str(palette_path),
                        str(before_color_path),
                    )
                )
                self._execute(
                    (
                        self._gdal["gdal_translate"],
                        "-of",
                        "PNG",
                        "-outsize",
                        str(quicklook_width),
                        "0",
                        "-r",
                        "average",
                        str(before_color_path),
                        str(before_quicklook_path),
                    )
                )
                self._compose_debug_plot(
                    raw_quicklook_path=before_quicklook_path,
                    masked_quicklook_path=quicklook_path,
                    output_path=debug_plot_path,
                    raw_valid_percent=raw_valid_percent,
                    valid_percent=valid_percent,
                )

            replacements = [
                (ndvi_path, self.ndvi_path),
                (quicklook_path, self.quicklook_path),
                (generated_worldfile, self.worldfile_path),
            ]
            if self.debug_plot_path is not None:
                replacements.append((debug_plot_path, self.debug_plot_path))
            for source, destination in replacements:
                os.replace(source, destination)

        manifest = {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "complete",
            "fingerprint": fingerprint,
            "input_path": str(self.config.input_path),
            "input_files": input_files,
            "force_job_manifest": (
                None
                if self._force_job_manifest() is None
                else str(self._force_job_manifest())
            ),
            "crop_bounds": None if crop_bounds is None else list(crop_bounds),
            "bands": bands,
            "mask_quality": self.config.mask_quality,
            "cloud_threshold": self.config.cloud_threshold,
            "invalid_scl_classes": list(self.config.invalid_scl_classes),
            "ndvi_formula": "(B08 - B04) / (B08 + B04)",
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
            "commands": [list(command) for command in self._commands],
        }
        _write_json_atomic(self.manifest_path, manifest)
        logger.info(
            "FORCE visualization complete – ndvi=%s quicklook=%s debug_plot=%s "
            "valid=%.2f%%",
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
        )
