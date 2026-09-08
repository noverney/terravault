"""Render three-source monthly vegetation maps and comparison statistics."""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import SWITZERLAND_BBOX
from .vegetation import (
    DEFAULT_VEGETATION_NODATA,
    REFLECTANCE_BAND_NAMES,
    VEGETATION_INDEX_NAMES,
    locate_gdal,
    run_gdal,
    set_vrt_band_descriptions,
)

COMPARISON_SOURCES: tuple[str, ...] = ("copernicus", "force", "swisseo")
SOURCE_LABELS: dict[str, str] = {
    "copernicus": "Sentinel-2 / Copernicus L2A",
    "force": "FORCE BOA/QAI",
    "swisseo": "swissEO S2-SR",
}
SOURCE_DIRECTORY_NAMES: dict[str, str] = {
    "copernicus": "sentinel-copernicus",
    "force": "force",
    "swisseo": "swisseo",
}
MONTH_LABELS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class ComparisonProduct:
    source: str
    year: int
    month: int
    reflectance_path: Path
    indices_path: Path
    manifest_path: Path

    def __post_init__(self) -> None:
        if self.source not in COMPARISON_SOURCES:
            raise ValueError(f"Unknown comparison source: {self.source}")
        if not 1 <= self.month <= 12:
            raise ValueError("month must be between 1 and 12")
        for name in ("reflectance_path", "indices_path", "manifest_path"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser().resolve())


def resolution_token(resolution: float) -> str:
    """Filename token for an output resolution, matching the monthly stems."""

    if resolution <= 0:
        raise ValueError("resolution must be positive")
    return f"{resolution:g}m".replace(".", "p")


def _unique_file(directory: Path, pattern: str) -> Path:
    matches = tuple(sorted(directory.glob(pattern)))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"Expected one {pattern!r} in {directory}; found none")
    names = ", ".join(path.name for path in matches)
    raise FileNotFoundError(
        f"Expected exactly one {pattern!r} in {directory}; found {len(matches)}: "
        f"{names}. Products of more than one resolution share this directory; "
        "select one with product_resolution (--product-resolution)."
    )


def discover_comparison_products(
    *,
    year: int,
    months: Sequence[int],
    copernicus_root: str | Path,
    force_root: str | Path,
    swisseo_root: str | Path,
    sources: Sequence[str] = COMPARISON_SOURCES,
    product_resolution: float | None = None,
) -> tuple[ComparisonProduct, ...]:
    """Resolve the selected source/month products from deterministic output trees.

    A month directory holds one set of products per output resolution, because
    the resolution is part of every filename. Pass ``product_resolution`` to
    select one set when several coexist; leave it unset when only one does.
    """

    requested = tuple(str(source) for source in sources)
    if not requested:
        raise ValueError("Comparison requires at least one source")
    if len(requested) != len(set(requested)):
        raise ValueError("Comparison sources contain duplicates")
    unknown = sorted(set(requested) - set(COMPARISON_SOURCES))
    if unknown:
        raise ValueError("Unknown comparison sources: " + ", ".join(unknown))
    selected = tuple(source for source in COMPARISON_SOURCES if source in requested)
    suffix = "" if product_resolution is None else f"_{resolution_token(product_resolution)}"

    roots = {
        "copernicus": Path(copernicus_root).expanduser().resolve(),
        "force": Path(force_root).expanduser().resolve(),
        "swisseo": Path(swisseo_root).expanduser().resolve(),
    }
    products: list[ComparisonProduct] = []
    for source in selected:
        for month in months:
            directory = roots[source] / f"year={year:04d}" / f"month={int(month):02d}"
            products.append(
                ComparisonProduct(
                    source=source,
                    year=year,
                    month=int(month),
                    reflectance_path=_unique_file(
                        directory, f"*{suffix}_reflectance.tif"
                    ),
                    indices_path=_unique_file(directory, f"*{suffix}_indices.tif"),
                    manifest_path=_unique_file(directory, f"*{suffix}.manifest.json"),
                )
            )
    return tuple(products)


@dataclass(frozen=True)
class ComparisonConfig:
    products: tuple[ComparisonProduct, ...]
    output_root: Path
    bbox: tuple[float, float, float, float] = field(
        default_factory=lambda: tuple(SWITZERLAND_BBOX)
    )
    cutline_path: Path | None = None
    target_crs: str = "EPSG:2056"
    analysis_resolution: float = 20.0
    nodata: float = DEFAULT_VEGETATION_NODATA
    preview_width: int = 4096
    atlas_width: int = 2400
    atlas_height: int = 1800
    histogram_bins: int = 64
    overwrite: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_root", Path(self.output_root).expanduser())
        if len(self.bbox) != 4:
            raise ValueError("bbox must contain west, south, east and north")
        west, south, east, north = (float(value) for value in self.bbox)
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bbox must be valid WGS84 coordinates")
        object.__setattr__(self, "bbox", (west, south, east, north))
        if self.cutline_path is not None:
            object.__setattr__(
                self,
                "cutline_path",
                Path(self.cutline_path).expanduser().resolve(),
            )
            if not self.cutline_path.is_file():
                raise FileNotFoundError(f"Comparison cutline does not exist: {self.cutline_path}")
        if self.analysis_resolution <= 0:
            raise ValueError("analysis_resolution must be positive")
        if self.preview_width < 512:
            raise ValueError("preview_width must be at least 512")
        if self.atlas_width < 1200 or self.atlas_height < 900:
            raise ValueError("atlas dimensions are too small for the comparison")
        if self.histogram_bins < 16:
            raise ValueError("histogram_bins must be at least 16")
        keys = [(product.source, product.year, product.month) for product in self.products]
        if not keys:
            raise ValueError("Comparison requires at least one product")
        if len(keys) != len(set(keys)):
            raise ValueError("Comparison products contain duplicate source/year/month keys")
        years = {product.year for product in self.products}
        if len(years) != 1:
            raise ValueError("All comparison products must use one year")
        months = sorted({product.month for product in self.products})
        if not 1 <= len(months) <= 3:
            raise ValueError("Comparison requires one to three distinct months")
        year = next(iter(years))
        sources = {product.source for product in self.products}
        expected = {
            (source, year, month)
            for source in sources
            for month in months
        }
        if set(keys) != expected:
            raise ValueError("Comparison requires every source selected for every requested month")

    @property
    def sources(self) -> tuple[str, ...]:
        """Sources present in the product grid, in canonical display order."""

        present = {product.source for product in self.products}
        return tuple(source for source in COMPARISON_SOURCES if source in present)


@dataclass(frozen=True)
class ComparisonResult:
    atlas_path: Path
    statistics_json_path: Path
    statistics_csv_path: Path
    mosaic_paths: tuple[Path, ...]
    index_map_paths: tuple[Path, ...]
    index_tiff_paths: tuple[Path, ...]
    source_overview_paths: tuple[Path, ...]
    report_manifest_path: Path
    report_readme_path: Path


class MonthlyComparisonRenderer:
    """Export individual maps plus one three-source comparison atlas."""

    def __init__(self, config: ComparisonConfig) -> None:
        self.config = config
        self._gdal = locate_gdal(
            "gdal_translate",
            "gdalinfo",
            "gdaldem",
            "gdalwarp",
            "gdal_rasterize",
            "ogr2ogr",
        )

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"Manifest must be a JSON object: {path}")
        return payload

    @staticmethod
    def _timings(manifest: Mapping[str, Any]) -> dict[str, float]:
        raw = manifest.get("timings_seconds") or {}
        monthly = raw.get("monthly_mosaic_generation")
        if monthly is None:
            stage_names = (
                "force_qai_screening",
                "monthly_compositing",
                "national_clipping_and_cog",
                "scene_masking",
                "tile_compositing",
                "country_mosaicking",
                "remote_cog_alignment",
                "quality_mask_and_reflectance_cog",
            )
            if not any(name in raw for name in stage_names):
                raise ValueError("Manifest does not contain monthly mosaic timing")
            monthly = sum(
                float(raw.get(name, 0.0))
                for name in stage_names
            )
        if raw.get("index_calculation") is None or raw.get("total") is None:
            raise ValueError("Manifest does not contain index and total timings")
        result = {
            "monthly_mosaic_generation": float(monthly),
            "index_calculation": float(raw["index_calculation"]),
            "total": float(raw["total"]),
        }
        if any(not math.isfinite(value) or value < 0 for value in result.values()):
            raise ValueError("Manifest timings must be finite and non-negative")
        if (
            result["total"] + 0.01
            < result["monthly_mosaic_generation"] + result["index_calculation"]
        ):
            raise ValueError("Manifest total timing is shorter than mosaic plus indices")
        return result

    @staticmethod
    def _reported_mask(manifest: Mapping[str, Any]) -> tuple[str | None, float | None]:
        if manifest.get("average_catalog_cloud_cover_pct") is not None:
            return "catalog cloud", float(manifest["average_catalog_cloud_cover_pct"])
        if manifest.get("average_source_percent_masked") is not None:
            return "source masked", float(manifest["average_source_percent_masked"])
        return None, None

    def _analysis_vrt(self, product: ComparisonProduct, output: Path) -> None:
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
            str(self.config.analysis_resolution),
            str(self.config.analysis_resolution),
            "-tap",
            "-r",
            "average",
            "-dstnodata",
            str(self.config.nodata),
        ]
        if self.config.cutline_path is not None:
            command.extend(("-cutline", str(self.config.cutline_path), "-crop_to_cutline"))
        command.extend((str(product.indices_path), str(output)))
        run_gdal(command)

    def _reflectance_preview_vrt(self, product: ComparisonProduct, output: Path) -> None:
        """Align and clip reflectance so previews use the same Swiss ROI as maps."""

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
            str(self.config.analysis_resolution),
            str(self.config.analysis_resolution),
            "-tap",
            "-r",
            "average",
            "-srcnodata",
            str(self.config.nodata),
            "-dstnodata",
            str(self.config.nodata),
            "-dstalpha",
        ]
        if self.config.cutline_path is not None:
            command.extend(("-cutline", str(self.config.cutline_path), "-crop_to_cutline"))
        command.extend((str(product.reflectance_path), str(output)))
        run_gdal(command)

    def _roi_pixel_count(self, analysis_path: Path, temporary: Path) -> tuple[int, int]:
        metadata = json.loads(
            run_gdal((self._gdal["gdalinfo"], "-json", str(analysis_path))).stdout
        )
        width, height = (int(value) for value in metadata["size"])
        total_pixels = width * height
        if self.config.cutline_path is None:
            return total_pixels, total_pixels
        transform = metadata["geoTransform"]
        west = float(transform[0])
        north = float(transform[3])
        east = west + width * float(transform[1])
        south = north + height * float(transform[5])
        mask = temporary / "comparison-roi-mask.tif"
        projected_cutline = temporary / "comparison-roi.geojson"
        run_gdal(
            (
                self._gdal["ogr2ogr"],
                "-f",
                "GeoJSON",
                "-t_srs",
                self.config.target_crs,
                str(projected_cutline),
                str(self.config.cutline_path),
            )
        )
        run_gdal(
            (
                self._gdal["gdal_rasterize"],
                "-burn",
                "1",
                "-init",
                "0",
                "-a_nodata",
                "0",
                "-ot",
                "Byte",
                "-a_srs",
                self.config.target_crs,
                "-te",
                str(min(west, east)),
                str(min(south, north)),
                str(max(west, east)),
                str(max(south, north)),
                "-ts",
                str(width),
                str(height),
                str(projected_cutline),
                str(mask),
            )
        )
        mask_info = json.loads(
            run_gdal((self._gdal["gdalinfo"], "-json", "-stats", str(mask))).stdout
        )
        metadata_domain = (mask_info["bands"][0].get("metadata") or {}).get("") or {}
        valid_percent = float(metadata_domain.get("STATISTICS_VALID_PERCENT", 0.0))
        roi_pixels = round(total_pixels * valid_percent / 100.0)
        if roi_pixels <= 0:
            raise ValueError("Comparison cutline contains no pixels on the analysis grid")
        return roi_pixels, total_pixels

    def _raster_statistics(
        self,
        product: ComparisonProduct,
        analysis_path: Path,
        roi_pixel_count: int,
    ) -> list[dict[str, Any]]:
        completed = run_gdal(
            (self._gdal["gdalinfo"], "-json", "-stats", "-hist", str(analysis_path))
        )
        metadata = json.loads(completed.stdout)
        width, height = (int(value) for value in metadata["size"])
        total_pixels = width * height
        results: list[dict[str, Any]] = []
        bands = metadata.get("bands") or []
        if len(bands) != len(VEGETATION_INDEX_NAMES):
            raise ValueError(f"Expected four vegetation-index bands: {product.indices_path}")
        for expected_name, band in zip(VEGETATION_INDEX_NAMES, bands, strict=True):
            description = str(band.get("description") or expected_name).upper()
            if description != expected_name:
                raise ValueError(
                    f"Band order mismatch in {product.indices_path}: expected {expected_name}"
                )
            statistics = (band.get("metadata") or {}).get("") or {}
            valid_percent = float(statistics.get("STATISTICS_VALID_PERCENT", 0.0))
            histogram = band.get("histogram") or {}
            source_buckets = [int(value) for value in histogram.get("buckets") or []]
            source_min = float(histogram.get("min", -1.0))
            source_max = float(histogram.get("max", 1.0))
            rebinned = [0] * self.config.histogram_bins
            underflow = 0
            overflow = 0
            if source_buckets and source_max > source_min:
                source_width = (source_max - source_min) / len(source_buckets)
                for index, count in enumerate(source_buckets):
                    centre = source_min + (index + 0.5) * source_width
                    if centre < -1.0:
                        underflow += count
                        continue
                    if centre > 1.0:
                        overflow += count
                        continue
                    target = min(
                        self.config.histogram_bins - 1,
                        int((centre + 1.0) / 2.0 * self.config.histogram_bins),
                    )
                    rebinned[target] += count
            in_range_count = sum(rebinned)
            bin_width = 2.0 / self.config.histogram_bins
            density = [
                0.0 if in_range_count == 0 else count / in_range_count / bin_width
                for count in rebinned
            ]
            valid_count = round(total_pixels * valid_percent / 100.0)
            roi_coverage = min(100.0, valid_count / roi_pixel_count * 100.0)
            results.append(
                {
                    "index": expected_name,
                    "minimum": band.get("minimum", band.get("min")),
                    "maximum": band.get("maximum", band.get("max")),
                    "mean": band.get("mean"),
                    "stddev": band.get("stdDev"),
                    "valid_pixel_coverage_pct": roi_coverage,
                    "masked_or_missing_pct": max(0.0, 100.0 - roi_coverage),
                    "valid_pixel_count_estimate": valid_count,
                    "roi_pixel_count": roi_pixel_count,
                    "total_output_pixels": total_pixels,
                    "histogram_min": -1.0,
                    "histogram_max": 1.0,
                    "histogram_counts": rebinned,
                    "histogram_density": density,
                    "histogram_underflow_count": underflow,
                    "histogram_overflow_count": overflow,
                }
            )
        return results

    @staticmethod
    def _color_table(path: Path) -> None:
        path.write_text(
            """nv 0 0 0 0
-1.0 49 54 149 255
-0.5 69 117 180 255
0.0 190 77 47 255
0.2 245 216 120 255
0.4 166 217 106 255
0.6 72 161 78 255
0.8 0 104 55 255
1.0 0 68 27 255
""",
            encoding="utf-8",
        )

    def _render_index_map(
        self,
        *,
        product: ComparisonProduct,
        analysis_path: Path,
        band_number: int,
        output: Path,
        temporary: Path,
        color_table: Path,
    ) -> None:
        band = temporary / f"{product.source}-{product.month:02d}-{band_number}.tif"
        run_gdal(
            (
                self._gdal["gdal_translate"], "-of", "GTiff", "-b", str(band_number),
                "-outsize", str(self.config.preview_width), "0", "-r", "average",
                str(analysis_path), str(band),
            )
        )
        run_gdal(
            (
                self._gdal["gdaldem"], "color-relief", "-alpha", "-of", "PNG",
                str(band), str(color_table), str(output),
            )
        )

    def _export_native_index_cog(
        self,
        *,
        product: ComparisonProduct,
        band_number: int,
        index_name: str,
        output: Path,
        temporary: Path,
    ) -> None:
        """Extract one index at the source COG's native grid without resampling."""

        selected_vrt = temporary / (
            f"{product.source}-{product.year:04d}-{product.month:02d}-"
            f"{index_name.lower()}-native.vrt"
        )
        staged = temporary / (
            f".{product.source}-{product.year:04d}-{product.month:02d}-"
            f"{index_name.lower()}.{uuid.uuid4().hex}.tif"
        )
        try:
            run_gdal(
                (
                    self._gdal["gdal_translate"],
                    "-of",
                    "VRT",
                    "-b",
                    str(band_number),
                    str(product.indices_path),
                    str(selected_vrt),
                )
            )
            set_vrt_band_descriptions(selected_vrt, (index_name,))
            run_gdal(
                (
                    self._gdal["gdal_translate"],
                    "-of",
                    "COG",
                    "-co",
                    "COMPRESS=DEFLATE",
                    "-co",
                    "BIGTIFF=IF_SAFER",
                    "-co",
                    "BLOCKSIZE=512",
                    "-mo",
                    f"TERRAVAULT_INDEX={index_name}",
                    "-mo",
                    f"TERRAVAULT_SOURCE_BAND={band_number}",
                    str(selected_vrt),
                    str(staged),
                )
            )
            self._publish_file_sequential(staged, output)
        finally:
            selected_vrt.unlink(missing_ok=True)
            staged.unlink(missing_ok=True)

    @staticmethod
    def _publish_file_sequential(source: Path, output: Path) -> None:
        """Publish a locally staged file without random-seek writes on SMB."""

        partial = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial")
        try:
            with source.open("rb") as reader, partial.open("xb") as writer:
                while chunk := reader.read(8 * 1024 * 1024):
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(partial, output)
        finally:
            partial.unlink(missing_ok=True)

    def _native_raster_grid(self, path: Path) -> dict[str, Any]:
        """Describe the stored grid used by a scientific-resolution export."""

        metadata = json.loads(
            run_gdal((self._gdal["gdalinfo"], "-json", str(path))).stdout
        )
        width, height = (int(value) for value in metadata["size"])
        transform = tuple(float(value) for value in metadata["geoTransform"])
        pixel_size_x = math.hypot(transform[1], transform[4])
        pixel_size_y = math.hypot(transform[2], transform[5])
        coordinate_system = metadata.get("coordinateSystem") or {}
        return {
            "raster_size": {"width": width, "height": height},
            "pixel_size": {"x": pixel_size_x, "y": pixel_size_y},
            "crs_wkt": coordinate_system.get("wkt"),
            "nodata": (metadata.get("bands") or [{}])[0].get("noDataValue"),
        }

    def _render_mosaic(self, analysis_path: Path, output: Path) -> None:
        # NIR/red/green false colour uses only the standardized bands shared by
        # all three sources, so no source gains an RGB-only advantage.
        band_number = {name: number for number, name in enumerate(REFLECTANCE_BAND_NAMES, start=1)}
        alpha_band = len(REFLECTANCE_BAND_NAMES) + 1
        run_gdal(
            (
                self._gdal["gdal_translate"], "-of", "PNG", "-ot", "Byte",
                "-b", str(band_number["B08"]), "-b", str(band_number["B04"]),
                "-b", str(band_number["B03"]), "-b", str(alpha_band),
                "-colorinterp", "red,green,blue,alpha",
                "-outsize", str(self.config.preview_width),
                "0", "-r", "average", "-scale_1", "0", "0.8", "0", "255",
                "-scale_2", "0", "0.5", "0", "255", "-scale_3", "0", "0.5",
                "0", "255", str(analysis_path), str(output),
            )
        )

    @staticmethod
    def _paste_preview(canvas, source_path: Path, box: tuple[int, int, int, int]) -> None:
        """Paste a preview onto white while preserving its transparent ROI shape."""

        from PIL import Image

        left, top, right, bottom = box
        with Image.open(source_path) as source_image:
            image = source_image.convert("RGBA")
            image.thumbnail((max(1, right - left), max(1, bottom - top)))
            white = Image.new("RGBA", image.size, "white")
            white.alpha_composite(image)
            x = left + max(0, (right - left - image.width) // 2)
            y = top + max(0, (bottom - top - image.height) // 2)
            canvas.paste(white.convert("RGB"), (x, y))

    @staticmethod
    def _font(size: int, *, bold: bool = False):
        from PIL import ImageFont

        names = (
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
            if bold
            else "/System/Library/Fonts/Supplemental/Arial.ttf",
        )
        for name in names:
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _draw_atlas(
        self,
        *,
        rows: Sequence[dict[str, Any]],
        map_paths: Mapping[tuple[str, int, str], Path],
        output: Path,
    ) -> None:
        from PIL import Image, ImageDraw

        canvas = Image.new("RGB", (self.config.atlas_width, self.config.atlas_height), "white")
        draw = ImageDraw.Draw(canvas)
        title_font = self._font(30, bold=True)
        header_font = self._font(20, bold=True)
        row_font = self._font(17, bold=True)
        small_font = self._font(14)
        tiny_font = self._font(11)
        left = 390
        top = 100
        bottom = 40
        column_width = (self.config.atlas_width - left - 24) // 4
        row_height = (self.config.atlas_height - top - bottom) // len(rows)
        draw.text(
            (24, 20),
            "Monthly vegetation comparison · maps, coverage, distributions and time",
            fill="black",
            font=title_font,
        )
        for column, index_name in enumerate(VEGETATION_INDEX_NAMES):
            x = left + column * column_width
            draw.text((x + 8, 66), index_name, fill="black", font=header_font)
        for row_number, row in enumerate(rows):
            y = top + row_number * row_height
            draw.line((16, y, self.config.atlas_width - 16, y), fill=(205, 205, 205), width=1)
            source = row["source"]
            month = int(row["month"])
            timings = row["timings_seconds"]
            draw.text(
                (20, y + 10),
                f"{SOURCE_LABELS[source]} · {MONTH_LABELS[month - 1]}",
                fill="black",
                font=row_font,
            )
            draw.text(
                (20, y + 38),
                f"mosaic  {timings['monthly_mosaic_generation']:.1f} s",
                fill=(50, 50, 50),
                font=small_font,
            )
            draw.text(
                (20, y + 57),
                f"4 indices  {timings['index_calculation']:.1f} s",
                fill=(50, 50, 50),
                font=small_font,
            )
            draw.text(
                (20, y + 76),
                f"total  {timings['total']:.1f} s",
                fill=(50, 50, 50),
                font=small_font,
            )
            label = row.get("reported_mask_label")
            value = row.get("reported_mask_pct")
            if label is not None and value is not None:
                draw.text(
                    (20, y + 98),
                    f"{label}: {value:.1f}%",
                    fill=(75, 75, 75),
                    font=small_font,
                )
            stats_by_index = {entry["index"]: entry for entry in row["indices"]}
            for column, index_name in enumerate(VEGETATION_INDEX_NAMES):
                x = left + column * column_width
                cell_right = x + column_width - 10
                cell_bottom = y + row_height - 8
                map_width = int(column_width * 0.55)
                self._paste_preview(
                    canvas,
                    map_paths[(source, month, index_name)],
                    (x + 6, y + 6, x + map_width - 6, y + row_height - 28),
                )
                stats = stats_by_index[index_name]
                histogram = stats["histogram_density"]
                chart_left = x + map_width + 4
                chart_top = y + 24
                chart_right = cell_right - 5
                chart_bottom = cell_bottom - 45
                draw.text(
                    (chart_left, y + 7),
                    "normalized density",
                    fill=(75, 75, 75),
                    font=tiny_font,
                )
                draw.rectangle(
                    (chart_left, chart_top, chart_right, chart_bottom),
                    outline=(180, 180, 180),
                    width=1,
                )
                maximum = max(histogram) if histogram else 0
                if maximum > 0 and len(histogram) > 1:
                    points = []
                    for position, count in enumerate(histogram):
                        px = (
                            chart_left
                            + position
                            / (len(histogram) - 1)
                            * (chart_right - chart_left)
                        )
                        py = chart_bottom - count / maximum * (chart_bottom - chart_top - 4)
                        points.append((px, py))
                    draw.line(points, fill=(0, 104, 55), width=2)
                for fraction, tick in ((0.0, "-1"), (0.5, "0"), (1.0, "+1")):
                    tick_x = chart_left + fraction * (chart_right - chart_left)
                    draw.line(
                        (tick_x, chart_bottom, tick_x, chart_bottom + 3),
                        fill=(110, 110, 110),
                        width=1,
                    )
                    draw.text(
                        (tick_x - 7, chart_bottom + 3),
                        tick,
                        fill=(75, 75, 75),
                        font=tiny_font,
                    )
                coverage = float(stats["valid_pixel_coverage_pct"])
                bar_left = chart_left
                bar_right = chart_right
                bar_top = cell_bottom - 25
                draw.rectangle(
                    (bar_left, bar_top, bar_right, bar_top + 9),
                    fill=(225, 225, 225),
                )
                draw.rectangle(
                    (
                        bar_left,
                        bar_top,
                        bar_left + (bar_right - bar_left) * coverage / 100.0,
                        bar_top + 9,
                    ),
                    fill=(72, 161, 78),
                )
                draw.text(
                    (chart_left, cell_bottom - 14),
                    f"valid {coverage:.1f}%",
                    fill="black",
                    font=small_font,
                )
        draw.line(
            (
                16,
                self.config.atlas_height - bottom,
                self.config.atlas_width - 16,
                self.config.atlas_height - bottom,
            ),
            fill=(205, 205, 205),
            width=1,
        )
        draw.text(
            (24, self.config.atlas_height - 27),
            "Times are measured monthly-stage runtimes; source input and retrieval scopes differ.",
            fill=(75, 75, 75),
            font=tiny_font,
        )
        canvas.save(output, format="PNG", optimize=True)

    def _draw_source_overview(
        self,
        *,
        source: str,
        rows: Sequence[dict[str, Any]],
        map_paths: Mapping[tuple[str, int, str], Path],
        mosaic_paths: Mapping[tuple[str, int], Path],
        output: Path,
    ) -> None:
        """Create one legible month-by-product contact sheet for a source."""

        from PIL import Image, ImageDraw

        source_rows = sorted(
            (row for row in rows if row["source"] == source),
            key=lambda row: int(row["month"]),
        )
        width = 2400
        height = 1320
        canvas = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(canvas)
        title_font = self._font(34, bold=True)
        header_font = self._font(22, bold=True)
        row_font = self._font(20, bold=True)
        small_font = self._font(16)
        tiny_font = self._font(13)
        left = 330
        top = 125
        bottom = 95
        columns = ("False colour", *VEGETATION_INDEX_NAMES)
        column_width = (width - left - 30) // len(columns)
        row_height = (height - top - bottom) // len(source_rows)
        year = int(source_rows[0]["year"])
        draw.text(
            (28, 22),
            f"{SOURCE_LABELS[source]} · Switzerland monthly overview · {year}",
            fill="black",
            font=title_font,
        )
        draw.text(
            (28, 69),
            "Transparent/nodata pixels are white; index colours use one fixed −1 to +1 scale.",
            fill=(70, 70, 70),
            font=small_font,
        )
        for column, label in enumerate(columns):
            x = left + column * column_width
            draw.text((x + 10, 94), label, fill="black", font=header_font)
        for row_number, row in enumerate(source_rows):
            month = int(row["month"])
            y = top + row_number * row_height
            draw.line((18, y, width - 18, y), fill=(205, 205, 205), width=1)
            timings = row["timings_seconds"]
            draw.text(
                (28, y + 18),
                MONTH_LABELS[month - 1],
                fill="black",
                font=row_font,
            )
            draw.text(
                (28, y + 55),
                f"mosaic {timings['monthly_mosaic_generation']:.1f} s",
                fill=(55, 55, 55),
                font=small_font,
            )
            draw.text(
                (28, y + 80),
                f"4 indices {timings['index_calculation']:.1f} s",
                fill=(55, 55, 55),
                font=small_font,
            )
            draw.text(
                (28, y + 105),
                f"total {timings['total']:.1f} s",
                fill=(55, 55, 55),
                font=small_font,
            )
            label = row.get("reported_mask_label")
            reported = row.get("reported_mask_pct")
            if label is not None and reported is not None:
                draw.text(
                    (28, y + 137),
                    f"{label}: {reported:.1f}%",
                    fill=(80, 80, 80),
                    font=tiny_font,
                )
            previews = [
                mosaic_paths[(source, month)],
                *(map_paths[(source, month, name)] for name in VEGETATION_INDEX_NAMES),
            ]
            for column, preview in enumerate(previews):
                x = left + column * column_width
                self._paste_preview(
                    canvas,
                    preview,
                    (x + 8, y + 10, x + column_width - 8, y + row_height - 18),
                )
        legend_left = left + column_width + 18
        legend_right = width - 40
        legend_top = height - 61
        colours = (
            (49, 54, 149),
            (69, 117, 180),
            (190, 77, 47),
            (245, 216, 120),
            (166, 217, 106),
            (72, 161, 78),
            (0, 104, 55),
            (0, 68, 27),
        )
        segment = (legend_right - legend_left) / len(colours)
        for index, colour in enumerate(colours):
            x0 = round(legend_left + index * segment)
            x1 = round(legend_left + (index + 1) * segment)
            draw.rectangle((x0, legend_top, x1, legend_top + 15), fill=colour)
        for fraction, label in ((0.0, "−1"), (0.5, "0"), (1.0, "+1")):
            x = legend_left + fraction * (legend_right - legend_left)
            draw.text((x - 8, legend_top + 20), label, fill=(70, 70, 70), font=tiny_font)
        draw.text(
            (28, height - 55),
            "False colour: NIR / red / green",
            fill=(70, 70, 70),
            font=small_font,
        )
        canvas.save(output, format="PNG", optimize=True)

    def _write_source_report(
        self,
        *,
        output_root: Path,
        products: Mapping[tuple[str, int], ComparisonProduct],
        rows: Sequence[dict[str, Any]],
        map_paths: Mapping[tuple[str, int, str], Path],
        mosaic_paths: Mapping[tuple[str, int], Path],
        atlas: Path,
        statistics_json: Path,
        statistics_csv: Path,
        temporary: Path,
    ) -> tuple[tuple[Path, ...], tuple[Path, ...], Path, Path]:
        """Organize previews and metadata into source-specific report folders."""

        source_overviews: list[Path] = []
        native_index_cogs: list[Path] = []
        source_payload: dict[str, Any] = {}
        for source in self.config.sources:
            directory_name = SOURCE_DIRECTORY_NAMES[source]
            source_root = output_root / "sources" / directory_name
            source_root.mkdir(parents=True, exist_ok=True)
            overview = source_root / "overview.png"
            self._draw_source_overview(
                source=source,
                rows=rows,
                map_paths=map_paths,
                mosaic_paths=mosaic_paths,
                output=overview,
            )
            source_overviews.append(overview)
            months_payload: list[dict[str, Any]] = []
            for row in sorted(
                (entry for entry in rows if entry["source"] == source),
                key=lambda entry: int(entry["month"]),
            ):
                year = int(row["year"])
                month = int(row["month"])
                month_root = source_root / f"year={year:04d}" / f"month={month:02d}"
                month_root.mkdir(parents=True, exist_ok=True)
                copied_mosaic = month_root / "false-colour.png"
                shutil.copy2(mosaic_paths[(source, month)], copied_mosaic)
                product = products[(source, month)]
                copied_indices: dict[str, dict[str, Any]] = {}
                for band_number, index_name in enumerate(
                    VEGETATION_INDEX_NAMES,
                    start=1,
                ):
                    copied = month_root / f"{index_name.lower()}.png"
                    shutil.copy2(map_paths[(source, month, index_name)], copied)
                    scientific = month_root / f"{index_name.lower()}.tif"
                    self._export_native_index_cog(
                        product=product,
                        band_number=band_number,
                        index_name=index_name,
                        output=scientific,
                        temporary=temporary,
                    )
                    native_index_cogs.append(scientific)
                    grid = self._native_raster_grid(scientific)
                    copied_indices[index_name] = {
                        "png": str(copied.relative_to(output_root)),
                        "tiff": str(scientific.relative_to(output_root)),
                        "source_band": band_number,
                        "source_band_name": index_name,
                        "pixel_size": grid["pixel_size"],
                        "raster_size": grid["raster_size"],
                        "crs_wkt": grid["crs_wkt"],
                        "nodata": grid["nodata"],
                        "source_multi_band_cog": row["indices_path"],
                    }
                months_payload.append(
                    {
                        "year": year,
                        "month": month,
                        "source_manifest": row["manifest_path"],
                        "source_reflectance": row["reflectance_path"],
                        "source_indices": row["indices_path"],
                        "false_colour": str(copied_mosaic.relative_to(output_root)),
                        "indices": copied_indices,
                        "timings_seconds": row["timings_seconds"],
                        "reported_mask_label": row.get("reported_mask_label"),
                        "reported_mask_pct": row.get("reported_mask_pct"),
                    }
                )
            source_payload[source] = {
                "label": SOURCE_LABELS[source],
                "folder": str(source_root.relative_to(output_root)),
                "overview": str(overview.relative_to(output_root)),
                "months": months_payload,
            }
        manifest = output_root / "report-manifest.json"
        readme = output_root / "README.md"
        _write_json_atomic(
            manifest,
            {
                "schema_version": 2,
                "report_type": "switzerland-monthly-vegetation-comparison",
                "atlas": str(atlas.relative_to(output_root)),
                "statistics_json": str(statistics_json.relative_to(output_root)),
                "statistics_csv": str(statistics_csv.relative_to(output_root)),
                "sources": source_payload,
            },
        )
        readme.write_text(
            "# Switzerland monthly vegetation report\n\n"
            "This folder contains the source products supplied to the report command. The "
            f"{len(self.config.sources)} source folders keep the selected datasets separate; "
            "`report-manifest.json` records the originating scientific rasters and manifests.\n\n"
            f"- `{atlas.name}`: cross-source maps, distributions, coverage and time\n"
            "- `sources/*/overview.png`: one overview per data source\n"
            "- `sources/*/year=*/month=*`: false-colour PNG plus four vegetation-index "
            "PNG/GeoTIFF pairs\n"
            "- `comparison-statistics.json` and `.csv`: coverage, distributions and timing data\n\n"
            "Each index GeoTIFF is a losslessly compressed, single-band COG extracted at "
            "the source index raster's native stored grid. No comparison-grid resampling is "
            "applied. COG encoding is staged locally before a sequential publish to this "
            "folder. The report does not duplicate the six-band reflectance COG.\n\n"
            "White pixels are outside the supplied Swiss cutline or are nodata. Green index "
            "pixels represent positive vegetation-index values; colour alone is not a cloud mask.\n",
            encoding="utf-8",
        )
        return tuple(source_overviews), tuple(native_index_cogs), manifest, readme

    def run(self) -> ComparisonResult:
        output_root = self.config.output_root.resolve()
        maps_root = output_root / "index-maps"
        mosaics_root = output_root / "false-colour-mosaics"
        atlas = output_root / f"comparison-atlas-{len(self.config.products)}x12.png"
        statistics_json = output_root / "comparison-statistics.json"
        statistics_csv = output_root / "comparison-statistics.csv"
        report_manifest = output_root / "report-manifest.json"
        report_readme = output_root / "README.md"
        source_overviews = tuple(
            output_root / "sources" / SOURCE_DIRECTORY_NAMES[source] / "overview.png"
            for source in self.config.sources
        )
        expected_mosaics = tuple(
            mosaics_root
            / (
                f"{product.year:04d}-{product.month:02d}_"
                f"{product.source}_false-colour.png"
            )
            for product in self.config.products
        )
        expected_maps = tuple(
            maps_root
            / (
                f"{product.year:04d}-{product.month:02d}_"
                f"{product.source}_{index_name.lower()}.png"
            )
            for product in self.config.products
            for index_name in VEGETATION_INDEX_NAMES
        )
        expected_index_tiffs = tuple(
            output_root
            / "sources"
            / SOURCE_DIRECTORY_NAMES[product.source]
            / f"year={product.year:04d}"
            / f"month={product.month:02d}"
            / f"{index_name.lower()}.tif"
            for product in self.config.products
            for index_name in VEGETATION_INDEX_NAMES
        )
        outputs = (
            atlas,
            statistics_json,
            statistics_csv,
            report_manifest,
            report_readme,
            *source_overviews,
            *expected_mosaics,
            *expected_maps,
            *expected_index_tiffs,
        )
        if any(path.exists() for path in outputs) and not self.config.overwrite:
            raise FileExistsError("Comparison outputs exist; pass overwrite=True")
        if self.config.overwrite:
            for path in outputs:
                path.unlink(missing_ok=True)
        maps_root.mkdir(parents=True, exist_ok=True)
        mosaics_root.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        index_map_paths: list[Path] = []
        mosaic_paths: list[Path] = []
        maps: dict[tuple[str, int, str], Path] = {}
        mosaics: dict[tuple[str, int], Path] = {}
        products = {
            (product.source, product.month): product
            for product in self.config.products
        }
        with tempfile.TemporaryDirectory(prefix="terravault-comparison-") as temporary:
            temp = Path(temporary)
            color_table = temp / "vegetation-colors.txt"
            self._color_table(color_table)
            roi_pixel_count: int | None = None
            total_grid_pixels: int | None = None
            for product in sorted(
                self.config.products,
                key=lambda value: (value.month, COMPARISON_SOURCES.index(value.source)),
            ):
                for path in (product.reflectance_path, product.indices_path, product.manifest_path):
                    if not path.is_file():
                        raise FileNotFoundError(path)
                manifest = self._load_manifest(product.manifest_path)
                label, reported = self._reported_mask(manifest)
                row = {
                    "source": product.source,
                    "source_label": SOURCE_LABELS[product.source],
                    "year": product.year,
                    "month": product.month,
                    "manifest_path": str(product.manifest_path),
                    "reflectance_path": str(product.reflectance_path),
                    "indices_path": str(product.indices_path),
                    "reported_mask_label": label,
                    "reported_mask_pct": reported,
                    "timings_seconds": self._timings(manifest),
                }
                analysis = temp / f"{product.source}-{product.month:02d}-analysis.vrt"
                self._analysis_vrt(product, analysis)
                reflectance_preview = (
                    temp / f"{product.source}-{product.month:02d}-reflectance.vrt"
                )
                self._reflectance_preview_vrt(product, reflectance_preview)
                if roi_pixel_count is None:
                    roi_pixel_count, total_grid_pixels = self._roi_pixel_count(analysis, temp)
                row["indices"] = self._raster_statistics(
                    product,
                    analysis,
                    roi_pixel_count,
                )
                rows.append(row)
                mosaic = mosaics_root / (
                    f"{product.year:04d}-{product.month:02d}_"
                    f"{product.source}_false-colour.png"
                )
                self._render_mosaic(reflectance_preview, mosaic)
                mosaic_paths.append(mosaic)
                mosaics[(product.source, product.month)] = mosaic
                for band_number, index_name in enumerate(VEGETATION_INDEX_NAMES, start=1):
                    output = maps_root / (
                        f"{product.year:04d}-{product.month:02d}_"
                        f"{product.source}_{index_name.lower()}.png"
                    )
                    self._render_index_map(
                        product=product,
                        analysis_path=analysis,
                        band_number=band_number,
                        output=output,
                        temporary=temp,
                        color_table=color_table,
                    )
                    maps[(product.source, product.month, index_name)] = output
                    index_map_paths.append(output)
            _write_json_atomic(
                statistics_json,
                {
                    "schema_version": 1,
                    "coverage_definition": (
                        "Valid index pixels divided by pixels inside the shared comparison ROI. "
                        "Masked/missing includes cloud, shadow, snow, nodata and unobserved "
                        "coverage within that ROI."
                    ),
                    "comparison_grid": {
                        "bbox_wgs84": list(self.config.bbox),
                        "cutline_path": (
                            None
                            if self.config.cutline_path is None
                            else str(self.config.cutline_path)
                        ),
                        "target_crs": self.config.target_crs,
                        "resolution": self.config.analysis_resolution,
                        "resampling": "average",
                        "nodata": self.config.nodata,
                        "roi_pixel_count": roi_pixel_count,
                        "total_grid_pixels": total_grid_pixels,
                    },
                    "reported_mask_warning": (
                        "Catalog cloud cover and swissEO percent_masked use different source "
                        "definitions and are shown separately from output valid-pixel coverage."
                    ),
                    "rows": rows,
                },
            )
            with statistics_csv.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=(
                        "source", "year", "month", "index", "valid_pixel_coverage_pct",
                        "masked_or_missing_pct", "mean", "stddev", "minimum", "maximum",
                        "reported_mask_label", "reported_mask_pct", "mosaic_seconds",
                        "four_indices_seconds", "total_seconds",
                    ),
                )
                writer.writeheader()
                for row in rows:
                    for stats in row["indices"]:
                        writer.writerow(
                            {
                                "source": row["source"], "year": row["year"],
                                "month": row["month"], "index": stats["index"],
                                "valid_pixel_coverage_pct": stats["valid_pixel_coverage_pct"],
                                "masked_or_missing_pct": stats["masked_or_missing_pct"],
                                "mean": stats["mean"], "stddev": stats["stddev"],
                                "minimum": stats["minimum"], "maximum": stats["maximum"],
                                "reported_mask_label": row["reported_mask_label"],
                                "reported_mask_pct": row["reported_mask_pct"],
                                "mosaic_seconds": row["timings_seconds"][
                                    "monthly_mosaic_generation"
                                ],
                                "four_indices_seconds": row["timings_seconds"][
                                    "index_calculation"
                                ],
                                "total_seconds": row["timings_seconds"]["total"],
                            }
                        )
            self._draw_atlas(rows=rows, map_paths=maps, output=atlas)
            (
                source_overview_paths,
                index_tiff_paths,
                report_manifest_path,
                report_readme_path,
            ) = (
                self._write_source_report(
                    output_root=output_root,
                    products=products,
                    rows=rows,
                    map_paths=maps,
                    mosaic_paths=mosaics,
                    atlas=atlas,
                    statistics_json=statistics_json,
                    statistics_csv=statistics_csv,
                    temporary=temp,
                )
            )
        return ComparisonResult(
            atlas_path=atlas,
            statistics_json_path=statistics_json,
            statistics_csv_path=statistics_csv,
            mosaic_paths=tuple(mosaic_paths),
            index_map_paths=tuple(index_map_paths),
            index_tiff_paths=index_tiff_paths,
            source_overview_paths=source_overview_paths,
            report_manifest_path=report_manifest_path,
            report_readme_path=report_readme_path,
        )
