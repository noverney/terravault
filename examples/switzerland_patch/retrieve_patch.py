#!/usr/bin/env python3
"""Retrieve a small Sentinel-2 patch over Switzerland using TerraVault."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Zurich-area patch [west, south, east, north] in WGS-84
DEFAULT_SWISS_PATCH_BBOX = [8.47, 47.33, 8.62, 47.44]


def _load_terravault() -> tuple[object, object, object, object]:
    try:
        from terravault import Pipeline
        from terravault.downloader import DownloadConfig
        from terravault.env import load_dotenv
        from terravault.pipeline import PipelineConfig
    except ModuleNotFoundError:
        # Allow running directly from the repository without installing first.
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root))
        from terravault import Pipeline
        from terravault.downloader import DownloadConfig
        from terravault.env import load_dotenv
        from terravault.pipeline import PipelineConfig
    return Pipeline, DownloadConfig, PipelineConfig, load_dotenv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Retrieve Sentinel-2 data for a small Switzerland patch and store "
            "outputs in this folder's data/ directory."
        )
    )
    parser.add_argument(
        "--catalog-url",
        default="https://stac.dataspace.copernicus.eu/v1",
        help="STAC catalog URL",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional .env file to load before resolving credentials",
    )
    parser.add_argument(
        "--collections",
        nargs="+",
        default=["sentinel-2-l2a"],
        help="Collections to query (default: sentinel-2-l2a)",
    )
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        default=DEFAULT_SWISS_PATCH_BBOX,
        help="Bounding box in WGS-84 (default: Zurich-area patch)",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=72,
        help="Hours to look back on first run (default: 72)",
    )
    parser.add_argument(
        "--max-cloud-cover",
        type=float,
        default=20.0,
        help="Max cloud cover percentage (default: 20)",
    )
    parser.add_argument(
        "--asset-keys",
        nargs="+",
        default=["thumbnail"],
        help="Asset keys to download (default: thumbnail)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Concurrent download workers (default: 2)",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Only fetch/store metadata; skip asset downloads",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--state-db",
        default=None,
        help="State DB path (default: examples/switzerland_patch/data/terravault_state.db)",
    )
    parser.add_argument(
        "--storage-root",
        default=None,
        help="Storage root path (default: examples/switzerland_patch/data/satellite_data)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    Pipeline, DownloadConfig, PipelineConfig, load_dotenv = _load_terravault()
    load_dotenv(args.env_file)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    root = Path(__file__).resolve().parent
    data_dir = root / "data"
    storage_root = Path(args.storage_root) if args.storage_root else (data_dir / "satellite_data")
    state_db = Path(args.state_db) if args.state_db else (data_dir / "terravault_state.db")
    if not storage_root.is_absolute():
        storage_root = (Path.cwd() / storage_root).resolve()
    if not state_db.is_absolute():
        state_db = (Path.cwd() / state_db).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    storage_root.mkdir(parents=True, exist_ok=True)
    state_db.parent.mkdir(parents=True, exist_ok=True)

    cfg = PipelineConfig(
        catalog_url=args.catalog_url,
        collections=args.collections,
        bbox=args.bbox,
        max_cloud_cover=args.max_cloud_cover,
        lookback_hours=args.lookback_hours,
        download=DownloadConfig(max_workers=args.workers),
        asset_keys=args.asset_keys,
        state_db=str(state_db),
        storage_root=str(storage_root),
    )

    result = Pipeline(cfg).run(download=not args.no_download)

    print(f"Data directory : {data_dir}")
    print(f"Storage root   : {storage_root}")
    print(f"State DB       : {state_db}")
    print(f"Discovered     : {result.items_discovered}")
    print(f"Processed      : {result.items_processed}")
    print(f"Duplicates     : {result.items_skipped_duplicate}")
    print(f"Downloads ok   : {result.downloads_ok}")
    print(f"Downloads fail : {result.downloads_failed}")
    print(f"Errors         : {len(result.errors)}")

    if result.errors:
        for err in result.errors:
            print(f"  ERROR: {err}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
