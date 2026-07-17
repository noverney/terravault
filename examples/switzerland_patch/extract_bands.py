#!/usr/bin/env python3
"""Extract individual bands from a multiband Sentinel-2 GeoTIFF."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Split a multiband GeoTIFF into per-band GeoTIFFs. Optionally emit per-band "
            "validation PNGs with percentile stretching."
        )
    )
    parser.add_argument(
        "--input",
        default="examples/switzerland_patch/data/basel_latest_multispectral.tif",
        help="Input multiband GeoTIFF path",
    )
    parser.add_argument(
        "--metadata-json",
        default=None,
        help="Optional metadata JSON with raw_bands; defaults to input path with .json suffix",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory; defaults to <input_stem>_bands next to the input file",
    )
    parser.add_argument(
        "--bands",
        nargs="+",
        default=None,
        help="Explicit band names if no metadata JSON is available",
    )
    parser.add_argument(
        "--validate-pngs",
        action="store_true",
        help="Also create a stretched grayscale PNG preview for each band",
    )
    return parser


def _load_dependencies():
    try:
        import numpy as np
        from PIL import Image
        import tifffile
    except ModuleNotFoundError as exc:  # noqa: BLE001
        raise SystemExit(
            "This script requires numpy, Pillow, and tifffile in the active Python environment."
        ) from exc
    return np, Image, tifffile


def _load_band_names(
    metadata_path: Path | None,
    band_count: int,
    explicit_bands: list[str] | None,
) -> list[str]:
    if explicit_bands:
        if len(explicit_bands) != band_count:
            raise SystemExit(
                f"--bands provided {len(explicit_bands)} names but raster has {band_count} bands."
            )
        return explicit_bands

    if metadata_path and metadata_path.exists():
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        raw_bands = data.get("raw_bands")
        if isinstance(raw_bands, list) and len(raw_bands) == band_count:
            return [str(name) for name in raw_bands]

    return [f"band_{index:02d}" for index in range(1, band_count + 1)]


def _ensure_hwc(array, np):
    if array.ndim == 2:
        return array[:, :, np.newaxis]
    if array.ndim != 3:
        raise SystemExit(f"Unsupported raster array shape: {array.shape}")
    if array.shape[0] <= 32 and array.shape[0] < array.shape[1] and array.shape[0] < array.shape[2]:
        return np.moveaxis(array, 0, -1)
    return array


def _stretch_to_uint8(band, np):
    values = band[band > 0]
    if values.size == 0:
        values = band.reshape(-1)

    low = float(np.percentile(values, 2))
    high = float(np.percentile(values, 98))
    if high <= low:
        low = float(values.min())
        high = float(values.max())

    if high <= low:
        stretched = np.zeros(band.shape, dtype=np.uint8)
    else:
        scaled = np.clip((band.astype(np.float32) - low) / (high - low), 0.0, 1.0)
        stretched = (scaled * 255.0).astype(np.uint8)

    return stretched, {"stretch_p2": low, "stretch_p98": high}


def _run_gdal_translate(input_path: Path, output_path: Path, band_index: int) -> None:
    gdal_translate = shutil.which("gdal_translate")
    if not gdal_translate:
        raise SystemExit("gdal_translate was not found on PATH.")

    result = subprocess.run(
        [
            gdal_translate,
            "-q",
            "-b",
            str(band_index),
            str(input_path),
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"gdal_translate failed for band {band_index}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def main() -> int:
    args = build_parser().parse_args()
    np, Image, tifffile = _load_dependencies()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    metadata_path = (
        Path(args.metadata_json).resolve()
        if args.metadata_json
        else input_path.with_suffix(".json")
    )
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else input_path.parent / f"{input_path.stem}_bands"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    array = _ensure_hwc(tifffile.imread(str(input_path)), np)
    band_count = int(array.shape[2])
    band_names = _load_band_names(metadata_path, band_count, args.bands)

    manifest = {
        "input": str(input_path),
        "metadata_json": str(metadata_path) if metadata_path.exists() else None,
        "band_count": band_count,
        "bands": [],
    }

    for index, band_name in enumerate(band_names, start=1):
        band_tif = output_dir / f"{index:02d}_{band_name}.tif"
        _run_gdal_translate(input_path, band_tif, index)

        entry = {
            "index": index,
            "name": band_name,
            "tif": str(band_tif),
        }
        if args.validate_pngs:
            preview_png = output_dir / f"{index:02d}_{band_name}.png"
            stretched, stretch_stats = _stretch_to_uint8(array[:, :, index - 1], np)
            Image.fromarray(stretched, mode="L").save(preview_png)
            entry["validation_png"] = str(preview_png)
            entry["preview_stretch"] = stretch_stats

        manifest["bands"].append(entry)

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Input       : {input_path}")
    print(f"Output dir  : {output_dir}")
    print(f"Band count  : {band_count}")
    print(f"Manifest    : {manifest_path}")
    if args.validate_pngs:
        print("Validation  : per-band PNG previews created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
