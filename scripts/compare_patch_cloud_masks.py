#!/usr/bin/env python
"""Compare the per-acquisition cloud masks of the three monthly routes on a patch.

The monthly comparison (``terravault compare-monthly``) shows what each route's
composite looks like.  This script answers the question underneath it: for the
same Sentinel-2 acquisitions, which pixels does each processor's quality layer
throw away?

* Copernicus L2A (Sen2Cor): SCL classes and the CLD cloud probability, exactly
  as ``copernicus-monthly`` screens them.
* swissEO S2-SR v200: OmniCloudMask classes and the terrain (solar incidence)
  mask; v100: the combined cloud/cloud-shadow mask and terrain shadow.
* FORCE L2PS: the QAI bit flags, with and without FORCE's 300 m cloud buffer.

Every mask is warped with nearest neighbour onto the same EPSG:2056 grid that
the monthly products use, so pixel counts are directly comparable.  Outputs are
a ``summary.json``, a per-date ``per_date.csv`` and PNG figures.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
from osgeo import gdal
from PIL import Image, ImageDraw, ImageFont
from pystac_client import Client

gdal.UseExceptions()

SWISSEO_STAC_URL = "https://data.geo.admin.ch/api/stac/v1"
SWISSEO_COLLECTIONS = {
    "v100": "ch.swisstopo.swisseo_s2-sr_v100",
    "v200": "ch.swisstopo.swisseo_s2-sr_v200",
}
# copernicus-monthly defaults (DEFAULT_INVALID_SCL_CLASSES, CLD threshold 30).
SCL_ROUTE_INVALID = (0, 1, 3, 8, 9, 10, 11)
SCL_CLOUD = (8, 9, 10)
SCL_SHADOW = (3,)
SCL_SNOW = (11,)
CLD_THRESHOLD = 30
# monthly (post-FORCE) default.
FORCE_QAI_ROUTE_MASK = 0x031F

# Mask colours for the figures: cloud, cloud buffer, shadow, snow, terrain/other.
COLOURS = {
    "cloud": (255, 60, 60),
    "buffer": (255, 170, 60),
    "shadow": (60, 120, 255),
    "snow": (0, 230, 230),
    "other": (200, 0, 200),
}
LEGEND = (
    ("cloud", "cloud (incl. cirrus; Copernicus also CLD > 30%)"),
    ("buffer", "FORCE 300 m cloud buffer"),
    ("shadow", "cloud shadow"),
    ("snow", "snow"),
    ("other", "other invalid: swissEO terrain mask (v200 also open water), nodata, saturation"),
)


@dataclass
class SourceMasks:
    """Boolean layers of one source for one acquisition, on the common grid."""

    cloud: np.ndarray
    shadow: np.ndarray
    snow: np.ndarray
    route_invalid: np.ndarray
    buffer: np.ndarray | None = None
    other: np.ndarray | None = None
    # FORCE only: the route's invalid pixels if its 300 m buffer counted as clear.
    route_invalid_without_buffer: np.ndarray | None = None

    @property
    def cloud_or_shadow(self) -> np.ndarray:
        return self.cloud | self.shadow


def warp(source: str, bbox, resolution: float, *, resampling: str = "near", band: int = 1):
    """Read one band onto the common EPSG:2056 patch grid."""

    dataset = gdal.Warp(
        "",
        source,
        format="MEM",
        dstSRS="EPSG:2056",
        outputBounds=bbox,
        outputBoundsSRS="EPSG:4326",
        xRes=resolution,
        yRes=resolution,
        targetAlignedPixels=True,
        resampleAlg=resampling,
        multithread=True,
    )
    return dataset.GetRasterBand(band).ReadAsArray(), dataset.GetGeoTransform()


def force_products(force_root: Path) -> dict[date, Path]:
    products: dict[date, Path] = {}
    for qai in sorted(force_root.glob("level2/products/*/mosaic/*_LEVEL2_SEN2*_QAI.vrt")):
        stamp = qai.name[:8]
        products[date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:]))] = qai
    return products


def force_masks(qai_path: Path, bbox, resolution: float) -> SourceMasks:
    qai, _ = warp(str(qai_path), bbox, resolution)
    qai = qai.astype(np.uint16)
    cloud_state = (qai >> 1) & 0b11
    # 2 = confident opaque cloud, 3 = cirrus; 1 = FORCE's buffered cloud.
    cloud = (cloud_state == 2) | (cloud_state == 3)
    # Shadow, snow and the other screened flags still apply inside the buffer.
    other_flags = (qai & (FORCE_QAI_ROUTE_MASK & ~0x0006)) != 0
    return SourceMasks(
        cloud=cloud,
        route_invalid_without_buffer=other_flags | cloud,
        buffer=cloud_state == 1,
        shadow=((qai >> 3) & 1).astype(bool),
        snow=((qai >> 4) & 1).astype(bool),
        route_invalid=(qai & FORCE_QAI_ROUTE_MASK) != 0,
        other=((qai & 0x0001) | (qai & 0x0300)) != 0,
    )


def force_rgb(qai_path: Path, bbox, resolution: float) -> np.ndarray:
    boa = str(qai_path).replace("_QAI.vrt", "_BOA.vrt")
    channels = []
    for band in (3, 2, 1):  # FORCE BOA: BLUE, GREEN, RED, ...
        values, _ = warp(boa, bbox, resolution, band=band)
        channels.append(values.astype(np.float32) * 0.0001)
    return np.dstack(channels)


def copernicus_pieces(dataset_db: Path) -> dict[date, dict[str, str]]:
    connection = duckdb.connect(str(dataset_db), read_only=True)
    rows = connection.execute(
        """
        SELECT acquisition_time, asset_key, local_path
        FROM raster_pieces
        WHERE asset_key IN ('SCL_20m', 'CLD_20m')
        """
    ).fetchall()
    pieces: dict[date, dict[str, str]] = {}
    for acquired, asset_key, local_path in rows:
        pieces.setdefault(acquired.date(), {})[asset_key] = local_path
    return pieces


def copernicus_masks(paths: dict[str, str], bbox, resolution: float) -> SourceMasks:
    scl, _ = warp(paths["SCL_20m"], bbox, resolution)
    cld, _ = warp(paths["CLD_20m"], bbox, resolution)
    cloudy = cld > CLD_THRESHOLD
    return SourceMasks(
        cloud=np.isin(scl, SCL_CLOUD) | cloudy,
        shadow=np.isin(scl, SCL_SHADOW),
        snow=np.isin(scl, SCL_SNOW),
        route_invalid=np.isin(scl, SCL_ROUTE_INVALID) | cloudy,
        other=np.isin(scl, (0, 1)),
    )


def swisseo_items(version: str, year: int, month: int, bbox) -> dict[date, dict[str, str]]:
    client = Client.open(SWISSEO_STAC_URL)
    last = 31 if month == 12 else (date(year, month + 1, 1) - date(year, month, 1)).days
    items = client.search(
        collections=[SWISSEO_COLLECTIONS[version]],
        bbox=bbox,
        datetime=f"{year:04d}-{month:02d}-01T00:00:00Z/{year:04d}-{month:02d}-{last}T23:59:59Z",
    ).items()
    found: dict[date, dict[str, str]] = {}
    for item in items:
        hrefs = {asset.href.rsplit("/", 1)[-1]: asset.href for asset in item.assets.values()}
        found[item.datetime.date()] = hrefs
    return found


def _href(hrefs: dict[str, str], suffix: str) -> str:
    (match,) = [href for name, href in hrefs.items() if name.endswith(suffix)]
    return f"/vsicurl/{match}"


def swisseo_masks(version: str, hrefs: dict[str, str], bbox, resolution: float) -> SourceMasks:
    if version == "v200":
        cloud, _ = warp(_href(hrefs, "_cloudmask_10m.tif"), bbox, resolution)
        terrain, _ = warp(_href(hrefs, "_terrainmask_10m.tif"), bbox, resolution)
        terrain_invalid = terrain >= 90  # self shadow 90-180, cast shadow 200, nodata 255
        return SourceMasks(
            cloud=(cloud == 1) | (cloud == 2),
            shadow=cloud == 3,
            snow=np.zeros_like(terrain_invalid),
            route_invalid=(cloud != 0) | terrain_invalid,
            other=terrain_invalid,
        )
    masks = _href(hrefs, "_masks-10m.tif")
    terrain, _ = warp(masks, bbox, resolution, band=1)
    cloud_or_shadow, _ = warp(masks, bbox, resolution, band=2)
    combined = cloud_or_shadow == 1
    terrain_invalid = (terrain == 100) | (terrain == 255) | (cloud_or_shadow == 255)
    # v100 does not separate cloud from cloud shadow; count it all as cloud.
    return SourceMasks(
        cloud=combined,
        shadow=np.zeros_like(combined),
        snow=np.zeros_like(combined),
        route_invalid=combined | terrain_invalid,
        other=terrain_invalid,
    )


def confusion(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """Counts of (both, first only, second only, neither) flagged pixels."""

    both = np.count_nonzero(first & second)
    first_only = np.count_nonzero(first & ~second)
    second_only = np.count_nonzero(~first & second)
    return np.array([both, first_only, second_only, first.size - both - first_only - second_only])


def agreement(counts: np.ndarray) -> dict[str, float]:
    """Pixel agreement, intersection over union and Cohen's kappa from counts.

    Kappa discounts the agreement two masks would reach by chance, so on a
    scene that is almost entirely cloud it is near zero even when both masks
    agree; pooled counts over all dates are the fairer summary.
    """

    both, first_only, second_only, neither = (float(value) for value in counts)
    n = both + first_only + second_only + neither
    either = both + first_only + second_only
    p_first = (both + first_only) / n
    p_second = (both + second_only) / n
    observed = (both + neither) / n
    expected = p_first * p_second + (1 - p_first) * (1 - p_second)
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    return {
        "agreement_pct": round(100 * observed, 2),
        "iou": round(both / either, 4) if either else 1.0,
        "kappa": round(kappa, 4),
    }


def percent(mask: np.ndarray) -> float:
    return round(100.0 * float(np.count_nonzero(mask)) / mask.size, 2)


def stretch(rgb: np.ndarray, low: float = 0.0, high: float = 0.22) -> np.ndarray:
    scaled = np.clip((rgb - low) / (high - low), 0, 1) ** (1 / 1.4)
    return (scaled * 255).astype(np.uint8)


def overlay(rgb: np.ndarray, masks: SourceMasks | None) -> np.ndarray:
    image = (rgb.astype(np.float32) * 0.55).astype(np.uint8)
    if masks is None:
        return np.full_like(rgb, 235)
    layers = (
        ("other", masks.other),
        ("snow", masks.snow),
        ("buffer", masks.buffer),
        ("shadow", masks.shadow),
        ("cloud", masks.cloud),
    )
    for name, layer in layers:
        if layer is not None:
            image[layer] = COLOURS[name]
    return image


INDEX_NAMES = ("NDVI", "GNDVI", "NDRE", "NDWI")
NDVI_RAMP = (
    (-1.0, (49, 54, 149)),
    (0.0, (190, 77, 47)),
    (0.2, (245, 216, 120)),
    (0.4, (166, 217, 106)),
    (0.6, (72, 161, 78)),
    (0.8, (0, 104, 55)),
    (1.0, (0, 68, 27)),
)
DIFFERENCE_RAMP = ((-0.3, (33, 102, 172)), (0.0, (247, 247, 247)), (0.3, (178, 24, 43)))
NODATA_GREY = (200, 200, 200)


def colourise(values: np.ndarray, valid: np.ndarray, ramp) -> np.ndarray:
    stops = np.array([stop for stop, _ in ramp])
    image = np.empty((*values.shape, 3), dtype=np.uint8)
    clipped = np.clip(values, stops[0], stops[-1])
    for channel in range(3):
        image[..., channel] = np.interp(clipped, stops, [colour[channel] for _, colour in ramp])
    image[~valid] = NODATA_GREY
    return image


def read_indices(path: Path, bbox, resolution: float) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """The four index bands and their validity on the common grid."""

    dataset = gdal.Warp(
        "",
        str(path),
        format="MEM",
        dstSRS="EPSG:2056",
        outputBounds=bbox,
        outputBoundsSRS="EPSG:4326",
        xRes=resolution,
        yRes=resolution,
        targetAlignedPixels=True,
        resampleAlg="near",
    )
    values, valid = [], []
    for number in range(1, len(INDEX_NAMES) + 1):
        band = dataset.GetRasterBand(number)
        array = band.ReadAsArray().astype(np.float64)
        nodata = band.GetNoDataValue()
        ok = np.isfinite(array)
        if nodata is not None:
            ok &= array != nodata
        values.append(array)
        valid.append(ok)
    return values, valid


def difference_statistics(
    first: np.ndarray, second: np.ndarray, common: np.ndarray
) -> dict[str, float]:
    x, y = first[common], second[common]
    difference = y - x
    return {
        "common_valid_pct": percent(common),
        "mean_difference": round(float(np.mean(difference)), 4),
        "median_difference": round(float(np.median(difference)), 4),
        "rmsd": round(float(np.sqrt(np.mean(difference**2))), 4),
        "mean_absolute_difference": round(float(np.mean(np.abs(difference))), 4),
        "pearson_r": round(float(np.corrcoef(x, y)[0, 1]), 4),
        "abs_difference_above_0.1_pct": round(100.0 * float(np.mean(np.abs(difference) > 0.1)), 2),
    }


def composite_agreement(
    indices: list[tuple[str, Path]], bbox, resolution: float, output_dir: Path, scale: int
) -> dict[str, object]:
    """Pixel-by-pixel agreement between the monthly index composites."""

    loaded = {source: read_indices(path, bbox, resolution) for source, path in indices}
    coverage = {
        source: {name: percent(valid[i]) for i, name in enumerate(INDEX_NAMES)}
        for source, (_, valid) in loaded.items()
    }
    sources = [source for source, _ in indices]
    # swissEO v200 never has a value over open water, where NDVI is unstable
    # and atmospheric corrections differ most, so its valid pixels give a
    # land-only view of the same comparison.
    land = loaded["swisseo_v200"][1][0] if "swisseo_v200" in loaded else None
    pairs: dict[str, dict[str, dict[str, float]]] = {}
    land_pairs: dict[str, dict[str, dict[str, float]]] = {}
    difference_images: list[np.ndarray] = []
    difference_labels: list[str] = []
    for position, first in enumerate(sources):
        for second in sources[position + 1 :]:
            key = f"{second}_minus_{first}"
            pairs[key] = {}
            land_pairs[key] = {}
            for i, name in enumerate(INDEX_NAMES):
                common = loaded[first][1][i] & loaded[second][1][i]
                pairs[key][name] = difference_statistics(
                    loaded[first][0][i], loaded[second][0][i], common
                )
                if land is not None:
                    land_pairs[key][name] = difference_statistics(
                        loaded[first][0][i], loaded[second][0][i], common & land
                    )
                if name == "NDVI":
                    full = np.where(common, loaded[second][0][i] - loaded[first][0][i], 0.0)
                    difference_images.append(colourise(full, common, DIFFERENCE_RAMP))
                    difference_labels.append(f"NDVI {second} - {first}")
    render_grid(
        [("NDVI", [colourise(values[0], valid[0], NDVI_RAMP) for values, valid in loaded.values()])],
        sources,
        output_dir / "composite_ndvi.png",
        scale,
    )
    render_grid(
        [("difference", difference_images)],
        difference_labels,
        output_dir / "composite_ndvi_differences.png",
        scale,
    )
    return {
        "valid_pixel_pct": coverage,
        "pairs": pairs,
        "land_pairs_definition": "pixels valid in every compared source and in swissEO v200",
        "land_pairs": land_pairs,
    }


def _font(size: int):
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def render_grid(
    rows: list[tuple[str, list[np.ndarray]]],
    columns: list[str],
    output: Path,
    scale: int,
    *,
    legend: bool = False,
) -> None:
    height, width = rows[0][1][0].shape[:2]
    cell_w, cell_h = math.ceil(width / scale), math.ceil(height / scale)
    label_w, header_h, gap = 150, 54, 6
    legend_h = 44 if legend else 0
    canvas = Image.new(
        "RGB",
        (
            label_w + len(columns) * (cell_w + gap),
            header_h + len(rows) * (cell_h + gap) + legend_h,
        ),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    header, label = _font(22), _font(20)
    if legend:
        x, y = label_w, header_h + len(rows) * (cell_h + gap) + 10
        for key, text in LEGEND:
            draw.rectangle((x, y, x + 22, y + 22), fill=COLOURS[key])
            draw.text((x + 30, y), text, fill="black", font=label)
            x += 30 + int(draw.textlength(text, font=label)) + 34
    for index, name in enumerate(columns):
        draw.text((label_w + index * (cell_w + gap) + 6, 14), name, fill="black", font=header)
    for row, (acquired, cells) in enumerate(rows):
        top = header_h + row * (cell_h + gap)
        draw.text((10, top + cell_h // 2 - 12), acquired, fill="black", font=label)
        for index, cell in enumerate(cells):
            thumb = Image.fromarray(cell).resize((cell_w, cell_h), Image.Resampling.BOX)
            canvas.paste(thumb, (label_w + index * (cell_w + gap), top))
    canvas.save(output, optimize=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bbox", nargs=4, type=float, required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, required=True)
    parser.add_argument("--force-root", type=Path, required=True)
    parser.add_argument("--copernicus-db", type=Path, required=True)
    parser.add_argument("--swisseo-versions", nargs="+", default=["v200", "v100"])
    parser.add_argument("--resolution", type=float, default=10.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--figure-scale", type=int, default=5)
    parser.add_argument(
        "--indices",
        nargs=2,
        action="append",
        default=[],
        metavar=("SOURCE", "PATH"),
        help="A monthly *_indices.tif to include in the composite agreement (repeatable)",
    )
    args = parser.parse_args(argv)
    bbox = tuple(args.bbox)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    force = force_products(args.force_root)
    copernicus = copernicus_pieces(args.copernicus_db)
    swisseo = {
        version: swisseo_items(version, args.year, args.month, bbox)
        for version in args.swisseo_versions
    }
    dates = sorted(
        day for day in force if day.year == args.year and day.month == args.month
    )
    if not dates:
        print("no FORCE acquisitions found for the month", file=sys.stderr)
        return 1

    sources = ["copernicus", *(f"swisseo_{version}" for version in args.swisseo_versions)]
    sources += ["force", "force_no_buffer"]
    per_date: list[dict[str, object]] = []
    valid_counts: dict[str, np.ndarray] = {}
    observation_counts: dict[str, np.ndarray] = {}
    figure_rows: list[tuple[date, list[np.ndarray]]] = []
    pooled = {}
    pairs = [
        ("copernicus", "force"),
        ("copernicus", "force_no_buffer"),
        *(("copernicus", f"swisseo_{v}") for v in args.swisseo_versions),
        *((f"swisseo_{v}", "force") for v in args.swisseo_versions),
        *((f"swisseo_{v}", "force_no_buffer") for v in args.swisseo_versions),
    ]

    for acquired in dates:
        print(f"{acquired}: reading masks", flush=True)
        masks: dict[str, SourceMasks | None] = {}
        masks["force"] = force_masks(force[acquired], bbox, args.resolution)
        masks["force_no_buffer"] = SourceMasks(
            cloud=masks["force"].cloud,
            shadow=masks["force"].shadow,
            snow=masks["force"].snow,
            route_invalid=masks["force"].route_invalid_without_buffer,
            other=masks["force"].other,
        )
        masks["copernicus"] = (
            copernicus_masks(copernicus[acquired], bbox, args.resolution)
            if acquired in copernicus
            else None
        )
        for version in args.swisseo_versions:
            hrefs = swisseo[version].get(acquired)
            masks[f"swisseo_{version}"] = (
                None if hrefs is None else swisseo_masks(version, hrefs, bbox, args.resolution)
            )

        row: dict[str, object] = {"date": acquired.isoformat()}
        for source in sources:
            layer = masks[source]
            if layer is None:
                continue
            row[f"{source}_cloud_pct"] = percent(layer.cloud)
            row[f"{source}_shadow_pct"] = percent(layer.shadow)
            row[f"{source}_cloud_or_shadow_pct"] = percent(layer.cloud_or_shadow)
            row[f"{source}_route_invalid_pct"] = percent(layer.route_invalid)
            valid = ~layer.route_invalid
            valid_counts[source] = valid_counts.get(source, 0) + valid.astype(np.uint8)
            observation_counts[source] = observation_counts.get(source, 0) + 1
        row["force_buffer_pct"] = percent(masks["force"].buffer)
        for first, second in pairs:
            if masks.get(first) is None or masks.get(second) is None:
                continue
            first_mask = masks[first].cloud_or_shadow
            second_mask = masks[second].cloud_or_shadow
            if second == "force":
                second_mask = second_mask | masks["force"].buffer
            counts = confusion(first_mask, second_mask)
            pooled[(first, second)] = pooled.get((first, second), 0) + counts
            for key, value in agreement(counts).items():
                row[f"{first}_vs_{second}_{key}"] = value
        per_date.append(row)

        rgb = stretch(force_rgb(force[acquired], bbox, args.resolution))
        figure_rows.append(
            (
                acquired.isoformat(),
                [
                    rgb,
                    overlay(rgb, masks["copernicus"]),
                    *(overlay(rgb, masks[f"swisseo_{v}"]) for v in args.swisseo_versions),
                    overlay(rgb, masks["force"]),
                ],
            )
        )

    columns = sorted({key for row in per_date for key in row}, key=lambda k: (k != "date", k))
    with (args.output_dir / "per_date.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(per_date)

    render_grid(
        figure_rows,
        ["FORCE BOA true colour", "Copernicus SCL+CLD"]
        + [f"swissEO {v}" for v in args.swisseo_versions]
        + ["FORCE QAI"],
        args.output_dir / "per_date_masks.png",
        args.figure_scale,
        legend=True,
    )

    summary_sources: dict[str, dict[str, object]] = {}
    count_images = []
    maximum = max(int(np.max(counts)) for counts in valid_counts.values())
    for source, counts in valid_counts.items():
        summary_sources[source] = {
            "acquisitions": observation_counts[source],
            "mean_valid_observations": round(float(np.mean(counts)), 2),
            "pixels_without_valid_observation_pct": percent(counts == 0),
            "pixels_with_3_or_more_valid_observations_pct": percent(counts >= 3),
            "mean_route_invalid_pct": round(
                float(
                    np.mean(
                        [row[f"{source}_route_invalid_pct"] for row in per_date
                         if f"{source}_route_invalid_pct" in row]
                    )
                ),
                2,
            ),
            "mean_cloud_or_shadow_pct": round(
                float(
                    np.mean(
                        [row[f"{source}_cloud_or_shadow_pct"] for row in per_date
                         if f"{source}_cloud_or_shadow_pct" in row]
                    )
                ),
                2,
            ),
        }
        scaled = (np.clip(counts / max(maximum, 1), 0, 1) * 255).astype(np.uint8)
        # Dark = few clear looks, light = many.
        count_images.append((source, np.dstack([scaled, scaled, scaled])))
    render_grid(
        [("clear looks", [image for _, image in count_images])],
        [name for name, _ in count_images],
        args.output_dir / "valid_observation_counts.png",
        args.figure_scale,
    )
    for source, counts in valid_counts.items():
        histogram = np.bincount(counts.ravel(), minlength=maximum + 1)
        summary_sources[source]["valid_observation_histogram"] = histogram.tolist()

    # Sources publish different acquisitions (swissEO v100 skips the cloudiest
    # days), so also average over the dates that every source has.
    common_rows = [
        row for row in per_date
        if all(f"{source}_route_invalid_pct" in row for source in sources)
    ]
    for source in sources:
        summary_sources[source]["common_dates_mean_route_invalid_pct"] = round(
            float(np.mean([row[f"{source}_route_invalid_pct"] for row in common_rows])), 2
        )
        summary_sources[source]["common_dates_mean_cloud_or_shadow_pct"] = round(
            float(np.mean([row[f"{source}_cloud_or_shadow_pct"] for row in common_rows])), 2
        )

    pair_means: dict[str, dict[str, float]] = {}
    for first, second in pairs:
        prefix = f"{first}_vs_{second}_"
        rows = [row for row in per_date if f"{prefix}iou" in row]
        if rows:
            pair_means[f"{first}_vs_{second}"] = {
                key: round(float(np.mean([row[prefix + key] for row in rows])), 4)
                for key in ("agreement_pct", "iou", "kappa")
            } | {"dates": len(rows)}
    pooled_agreement = {
        f"{first}_vs_{second}": agreement(counts)
        | {"both_pct": round(100.0 * counts[0] / counts.sum(), 2),
           "first_only_pct": round(100.0 * counts[1] / counts.sum(), 2),
           "second_only_pct": round(100.0 * counts[2] / counts.sum(), 2)}
        for (first, second), counts in pooled.items()
    }

    summary = {
        "bbox_wgs84": list(bbox),
        "year": args.year,
        "month": args.month,
        "resolution": args.resolution,
        "grid_pixels": int(next(iter(valid_counts.values())).size),
        "acquisitions": [row["date"] for row in per_date],
        "common_dates": [row["date"] for row in common_rows],
        "definitions": {
            "copernicus": {
                "cloud": f"SCL in {SCL_CLOUD} or CLD > {CLD_THRESHOLD}",
                "shadow": f"SCL in {SCL_SHADOW}",
                "route_invalid": f"SCL in {SCL_ROUTE_INVALID} or CLD > {CLD_THRESHOLD}",
            },
            "swisseo_v200": {
                "cloud": "OmniCloudMask class 1 (thick) or 2 (thin)",
                "shadow": "OmniCloudMask class 3",
                "route_invalid": "cloud class != 0 or solar incidence >= 90 (incl. cast "
                "shadow 200 and nodata 255, which covers open water)",
            },
            "swisseo_v100": {
                "cloud": "cloud/cloud-shadow mask value 1 (not separable)",
                "route_invalid": "cloud mask 1, terrain shadow 100, or mask nodata 255",
            },
            "force": {
                "cloud": "QAI cloud state 2 (opaque) or 3 (cirrus)",
                "buffer": "QAI cloud state 1 (FORCE's 300 m cloud buffer)",
                "shadow": "QAI bit 3",
                "route_invalid": f"QAI & 0x{FORCE_QAI_ROUTE_MASK:04X} != 0",
                "agreement": "compared as cloud + buffer + shadow",
            },
            "force_no_buffer": {
                "route_invalid": "as force, but the 300 m buffer counted as clear",
                "agreement": "compared as cloud + shadow",
            },
        },
        "sources": summary_sources,
        "pairwise_cloud_or_shadow_mean": pair_means,
        "pairwise_cloud_or_shadow_pooled": pooled_agreement,
    }
    if args.indices:
        summary["composites"] = composite_agreement(
            [(source, Path(path)) for source, path in args.indices],
            bbox,
            args.resolution,
            args.output_dir,
            args.figure_scale,
        )
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"sources": summary_sources, "pooled": pooled_agreement}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
