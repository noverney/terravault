#!/usr/bin/env python
"""Render a single-source, single-month vegetation report.

This produces the deliverable a user asked for when only one data source is
ready: per-index overview PNGs clipped to the Swiss boundary, copies of the
full-resolution single-band index COGs, and a summary.json capturing the
measured processing timings from the source manifest.

Unlike the 9x12 three-source atlas (which requires exactly three months and
all three sources), this runs for whatever is present.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

INDEX_NAMES = ("NDVI", "GNDVI", "NDRE", "NDWI")

COLOR_TABLE = """nv 0 0 0 0
-1.0 49 54 149 255
-0.5 69 117 180 255
0.0 190 77 47 255
0.2 245 216 120 255
0.4 166 217 106 255
0.6 72 161 78 255
0.8 0 104 55 255
1.0 0 68 27 255
"""


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{proc.stderr[-2000:]}")


def publish_file_sequential(source: Path, destination: Path) -> None:
    """Copy a finished local artifact to SMB without random-seek writes."""

    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    try:
        with source.open("rb") as reader, partial.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=8 * 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def write_json_atomic(destination: Path, payload: dict[str, object]) -> None:
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.partial"
    )
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reflectance", required=True, type=Path)
    ap.add_argument("--indices", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--source", default="swisseo")
    ap.add_argument("--label", default="swissEO S2-SR")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--month", type=int, required=True)
    ap.add_argument("--roi", type=Path, default=None, help="Swiss boundary GeoJSON for clipping")
    ap.add_argument("--preview-width", type=int, default=1600)
    ap.add_argument("--output-root", required=True, type=Path)
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    timings = manifest.get("timings_seconds", {})

    month_dir = (
        args.output_root
        / "sources"
        / args.source
        / f"year={args.year:04d}"
        / f"month={args.month:02d}"
    )
    month_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tv-report-") as td:
        temp = Path(td)
        color_table = temp / "color.txt"
        color_table.write_text(COLOR_TABLE)

        # Clip the index raster to the ROI once (so PNGs show the Swiss shape).
        clipped = temp / "indices-clipped.tif"
        clip_cmd = [
            "gdalwarp", "-overwrite", "-of", "GTiff",
            "-co", "COMPRESS=DEFLATE",
        ]
        if args.roi is not None:
            clip_cmd += ["-cutline", str(args.roi), "-crop_to_cutline"]
        clip_cmd += [str(args.indices), str(clipped)]
        run(clip_cmd)

        clipped_stats: list[dict[str, object]] = []
        stats = subprocess.run(
            ["gdalinfo", "-json", "-stats", str(clipped)],
            capture_output=True,
            text=True,
        )
        if stats.returncode == 0:
            clipped_stats = json.loads(stats.stdout).get("bands", [])

        summary_indices = {}
        for band_number, index_name in enumerate(INDEX_NAMES, start=1):
            # Full-resolution single-band COG (scientific product).
            hi_tif = month_dir / f"{index_name.lower()}.tif"
            staged_tif = temp / f"{index_name.lower()}.tif"
            run([
                "gdal_translate", "-of", "COG", "-b", str(band_number),
                "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=IF_SAFER",
                "-co", "BLOCKSIZE=512", "-mo", f"TERRAVAULT_INDEX={index_name}",
                str(args.indices), str(staged_tif),
            ])
            publish_file_sequential(staged_tif, hi_tif)

            # Downsampled preview of the clipped band -> color PNG.
            preview = temp / f"{index_name.lower()}-preview.tif"
            run([
                "gdal_translate", "-of", "GTiff", "-b", str(band_number),
                "-outsize", str(args.preview_width), "0", "-r", "average",
                str(clipped), str(preview),
            ])
            png = month_dir / f"{index_name.lower()}.png"
            staged_png = temp / f"{index_name.lower()}.png"
            run([
                "gdaldem", "color-relief", "-alpha", "-of", "PNG",
                str(preview), str(color_table), str(staged_png),
            ])
            publish_file_sequential(staged_png, png)

            band_stats = {}
            if len(clipped_stats) >= band_number:
                b = clipped_stats[band_number - 1]
                band_stats = {
                    "min": b.get("minimum"), "max": b.get("maximum"),
                    "mean": b.get("mean"), "stddev": b.get("stdDev"),
                }
            summary_indices[index_name] = {
                "png": png.name, "tiff": hi_tif.name, **band_stats,
            }

    manifest_outputs = manifest.get("outputs", {})
    summary = {
        "source": args.source,
        "source_label": args.label,
        "year": args.year,
        "month": args.month,
        "formula_reference": manifest.get("formula_reference"),
        "composite_method": manifest.get("composite_method"),
        "item_count": manifest.get("item_count", manifest.get("scene_count")),
        "average_source_percent_masked": manifest.get("average_source_percent_masked"),
        "grid": {"width": manifest.get("width"), "height": manifest.get("height"),
                 "resolution_m": manifest.get("resolution")},
        "timings_seconds": timings,
        "timings_human": {
            k: f"{v/60:.1f} min" for k, v in timings.items()
        },
        "indices": summary_indices,
        "source_reflectance_cog": str(
            manifest_outputs.get("reflectance", args.reflectance)
        ),
        "source_indices_cog": str(manifest_outputs.get("indices", args.indices)),
    }
    write_json_atomic(month_dir / "summary.json", summary)
    print(f"Report written to {month_dir}")
    print(json.dumps(summary["timings_human"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
