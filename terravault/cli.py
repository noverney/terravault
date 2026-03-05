"""Command-line interface for TerraVault.

Usage examples::

    # Run the pipeline once (metadata only – no download)
    terravault run --no-download

    # Run with specific asset keys
    terravault run --asset-keys B04 B08

    # Override the STAC catalog URL
    terravault run --catalog-url https://stac.dataspace.copernicus.eu/v1

    # List available collections
    terravault collections
"""

from __future__ import annotations

import argparse
import logging
import sys

from .pipeline import Pipeline, PipelineConfig
from .downloader import DownloadConfig


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        level=level,
        stream=sys.stderr,
    )


def cmd_run(args: argparse.Namespace) -> int:
    cfg = PipelineConfig(
        catalog_url=args.catalog_url,
        collections=args.collections or ["sentinel-2-l2a", "sentinel-2-l1c"],
        max_cloud_cover=args.max_cloud_cover,
        lookback_hours=args.lookback_hours,
        asset_keys=args.asset_keys or [],
        state_db=args.state_db,
        storage_root=args.storage_root,
        download=DownloadConfig(max_workers=args.workers),
    )

    pipeline = Pipeline(cfg)
    result = pipeline.run(download=not args.no_download)

    print(
        f"Run complete – discovered={result.items_discovered}"
        f"  processed={result.items_processed}"
        f"  duplicates={result.items_skipped_duplicate}"
        f"  downloads_ok={result.downloads_ok}"
        f"  downloads_failed={result.downloads_failed}"
        f"  errors={len(result.errors)}"
    )

    if result.errors:
        for err in result.errors:
            print(f"  ERROR: {err}", file=sys.stderr)
        return 1

    return 0


def cmd_collections(args: argparse.Namespace) -> int:
    from .catalog import CatalogClient

    client = CatalogClient(catalog_url=args.catalog_url)
    try:
        cols = client.list_collections()
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for col in sorted(cols):
        print(col)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terravault",
        description="Satellite data ingestion pipeline – STAC-based discovery and download.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------------ run
    run_p = sub.add_parser("run", help="Execute one pipeline ingestion run")
    run_p.add_argument(
        "--catalog-url",
        default="https://stac.dataspace.copernicus.eu/v1",
        help="STAC API root URL",
    )
    run_p.add_argument(
        "--collections",
        nargs="+",
        metavar="COLLECTION",
        help="STAC collection IDs to query (default: sentinel-2-l2a sentinel-2-l1c)",
    )
    run_p.add_argument(
        "--max-cloud-cover",
        type=float,
        default=20.0,
        metavar="PCT",
        help="Maximum cloud cover percentage (default: 20)",
    )
    run_p.add_argument(
        "--lookback-hours",
        type=int,
        default=72,
        metavar="H",
        help="Hours to look back when no prior state exists (default: 72)",
    )
    run_p.add_argument(
        "--asset-keys",
        nargs="+",
        metavar="KEY",
        help="Asset keys to download, e.g. B04 B08 (default: all assets)",
    )
    run_p.add_argument(
        "--no-download",
        action="store_true",
        help="Persist metadata only; do not download assets",
    )
    run_p.add_argument(
        "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Maximum parallel download threads (default: 4)",
    )
    run_p.add_argument(
        "--state-db",
        default="terravault_state.db",
        metavar="PATH",
        help="SQLite state database path",
    )
    run_p.add_argument(
        "--storage-root",
        default="satellite_data",
        metavar="DIR",
        help="Root directory for downloaded data",
    )
    run_p.set_defaults(func=cmd_run)

    # ------------------------------------------------------------ collections
    col_p = sub.add_parser("collections", help="List collections available in the STAC catalog")
    col_p.add_argument(
        "--catalog-url",
        default="https://stac.dataspace.copernicus.eu/v1",
        help="STAC API root URL",
    )
    col_p.set_defaults(func=cmd_collections)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(func(args))


if __name__ == "__main__":
    main()
