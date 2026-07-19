#!/usr/bin/env python3
"""Extract a Swiss TerraVault snapshot and import it into a FORCE datacube."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shlex
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from terravault import (
    FORCE_DOCKER_IMAGE,
    ExtractionConfig,
    ForceConfig,
    ForcePostprocessor,
    RasterExtractor,
)
from terravault.historical import parse_utc_date

DEFAULT_DATASET_DB = Path("satellite_data/switzerland_ndvi/dataset.duckdb")
DEFAULT_OUTPUT_ROOT = Path("satellite_data/switzerland_ndvi/force")
DEFAULT_STAGING_RASTER = Path(
    "satellite_data/switzerland_ndvi/staging/switzerland_latest_ndvi_inputs.tif"
)
SWITZERLAND_BBOX = (5.96, 45.82, 10.49, 47.81)
NDVI_INPUTS = ("B04_10m", "B08_10m", "SCL_20m", "CLD_20m")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream a latest-per-tile Swiss COG from local TerraVault pieces, "
            "then import it into a FORCE external-feature datacube."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Existing stitched COG; skips the DuckDB extraction step",
    )
    parser.add_argument(
        "--dataset-db",
        type=Path,
        default=DEFAULT_DATASET_DB,
        help=f"Local TerraVault DuckDB (default: {DEFAULT_DATASET_DB})",
    )
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        default=SWITZERLAND_BBOX,
        help="WGS84 extraction bbox (default: Switzerland convenience bbox)",
    )
    parser.add_argument(
        "--roi",
        type=Path,
        help="Optional polygon GeoJSON cutline; the bbox still limits the query",
    )
    parser.add_argument(
        "--asset-keys",
        nargs="+",
        default=list(NDVI_INPUTS),
        metavar="KEY",
        help="Features in output-band order (default: B04 B08 SCL CLD)",
    )
    parser.add_argument(
        "--start-date",
        help="Optional acquisition start (YYYY-MM-DD or ISO-8601)",
    )
    parser.add_argument(
        "--end-date",
        help="Optional inclusive acquisition end (YYYY-MM-DD or ISO-8601)",
    )
    parser.add_argument(
        "--staging-raster",
        type=Path,
        default=DEFAULT_STAGING_RASTER,
        help=f"Stitched COG path (default: {DEFAULT_STAGING_RASTER})",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"FORCE cube/state root (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--basename",
        help="FORCE feature basename; default derives a stable name from source items",
    )
    parser.add_argument("--target-crs", default="EPSG:2056")
    parser.add_argument("--resolution", type=float, default=10.0)
    parser.add_argument("--warp-memory-mib", type=int, default=256)
    parser.add_argument("--max-output-gib", type=float, default=16.0)
    parser.add_argument(
        "--runtime",
        choices=("auto", "native", "docker"),
        default="auto",
    )
    parser.add_argument("--docker-image", default=FORCE_DOCKER_IMAGE)
    parser.add_argument(
        "--mount-root",
        type=Path,
        help="Docker volume root containing the stitched input and FORCE output",
    )
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument(
        "--refresh-staging",
        action="store_true",
        help="Rebuild the staging COG even when its selected source items are unchanged",
    )
    parser.add_argument(
        "--overwrite-force",
        action="store_true",
        help="Replace FORCE chips when the chosen basename already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan extraction; with --input, also print the exact FORCE commands",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def setup_logging(output_root: Path, verbose: bool) -> Path:
    log_path = output_root / "_terravault" / "logs" / "force-switzerland.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    rotating = RotatingFileHandler(
        log_path,
        maxBytes=25 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=(stream, rotating),
        force=True,
    )
    return log_path.resolve()


def _stable_basename(manifest_path: Path, fallback: str) -> str:
    if not manifest_path.is_file():
        return fallback
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources = manifest.get("sources") or []
    if not sources:
        return fallback
    identity = sorted(
        (
            str(source.get("item_id") or ""),
            str(source.get("asset_key") or ""),
            str(source.get("acquisition_time") or ""),
        )
        for source in sources
    )
    digest = hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:10]
    latest = max(value[2] for value in identity)
    parsed_latest = datetime.fromisoformat(
        latest[:-1] + "+00:00" if latest.endswith("Z") else latest
    )
    timestamp = parsed_latest.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"switzerland_ndvi_{timestamp}_{digest}"


def _source_identity(sources: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    identity: list[tuple[str, str, str]] = []
    for source in sources:
        acquisition = source.get("acquisition_time")
        if hasattr(acquisition, "isoformat"):
            acquisition = acquisition.isoformat()
        identity.append(
            (
                str(source.get("item_id") or ""),
                str(source.get("asset_key") or ""),
                str(acquisition or ""),
            )
        )
    return sorted(identity)


def _manifest_identity(manifest_path: Path) -> list[tuple[str, str, str]]:
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return _source_identity(manifest.get("sources") or [])


def main() -> int:
    args = build_parser().parse_args()
    output_root = args.output_root.expanduser().resolve()
    log_path = setup_logging(output_root, args.verbose)

    if args.input is not None:
        input_path = args.input.expanduser().resolve()
        manifest_path = input_path.with_suffix(f"{input_path.suffix}.manifest.json")
    else:
        input_path = args.staging_raster.expanduser().resolve()
        manifest_path = input_path.with_suffix(f"{input_path.suffix}.manifest.json")
        extraction_config = ExtractionConfig(
            dataset_db=args.dataset_db,
            bbox=tuple(args.bbox),
            asset_keys=tuple(args.asset_keys),
            output_path=input_path,
            start_datetime=(
                None if args.start_date is None else parse_utc_date(args.start_date)
            ),
            end_datetime=(
                None
                if args.end_date is None
                else parse_utc_date(args.end_date, inclusive_end=True)
            ),
            selection="latest-per-tile",
            target_crs=args.target_crs,
            resolution=args.resolution,
            resampling="near",
            output_dtype="Int16",
            nodata="-9999",
            warp_memory_mib=args.warp_memory_mib,
            max_output_gib=args.max_output_gib,
            cutline_path=args.roi,
            overwrite=True,
            dry_run=args.dry_run,
        )
        extractor = RasterExtractor(extraction_config)
        selected_identity = _source_identity(extractor.query_sources())
        staging_is_current = (
            input_path.is_file()
            and selected_identity
            and selected_identity == _manifest_identity(manifest_path)
        )
        if args.dry_run or args.refresh_staging or not staging_is_current:
            extraction = extractor.extract()
            print(
                f"Extraction {'planned' if extraction.dry_run else 'complete'}: "
                f"{extraction.width}x{extraction.height}, "
                f"{extraction.band_count} bands, "
                f"{extraction.estimated_uncompressed_bytes / (1024**3):.2f} GiB "
                f"uncompressed"
            )
            if args.dry_run:
                print(f"Extraction manifest: {extraction.manifest_path}")
                print("FORCE import was not run because the planned COG does not exist yet.")
                print(f"Log: {log_path}")
                return 0
        else:
            print(f"Staging COG is current; reusing {input_path}")

    basename = args.basename or _stable_basename(manifest_path, input_path.stem)
    result = ForcePostprocessor(
        ForceConfig(
            input_path=input_path,
            output_root=output_root,
            basename=basename,
            runtime=args.runtime,
            docker_image=args.docker_image,
            mount_root=args.mount_root,
            target_crs=args.target_crs,
            resolution=args.resolution,
            output_nodata=-9999,
            output_dtype="Int16",
            jobs=args.jobs,
            overwrite=args.overwrite_force,
            dry_run=args.dry_run,
        )
    ).run()
    print(
        f"FORCE {result.status}: chips={len(result.chip_paths)} "
        f"mosaic={result.mosaic_path} manifest={result.manifest_path}"
    )
    if args.dry_run:
        print("Commands:")
        for command in result.commands:
            print(f"  {shlex.join(command)}")
    print(f"Log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
