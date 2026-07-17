#!/usr/bin/env python3
"""Fetch a raw Sentinel-2 patch through the CDSE Sentinel Hub Process API."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Zurich-area patch [west, south, east, north] in WGS-84
DEFAULT_SWISS_PATCH_BBOX = [8.47, 47.33, 8.62, 47.44]


def _load_terravault() -> tuple[object, object, object, object]:
    try:
        from terravault.auth import SentinelHubAuthConfig
        from terravault.env import load_dotenv
        from terravault.process_api import ProcessPatchConfig, fetch_sentinel2_patch
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root))
        from terravault.auth import SentinelHubAuthConfig
        from terravault.env import load_dotenv
        from terravault.process_api import ProcessPatchConfig, fetch_sentinel2_patch
    return SentinelHubAuthConfig, ProcessPatchConfig, fetch_sentinel2_patch, load_dotenv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch a raw Sentinel-2 patch as GeoTIFF using CDSE Sentinel Hub Process API."
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
        "--env-file",
        default=".env",
        help="Optional .env file to load before resolving credentials",
    )
    parser.add_argument(
        "--time-from",
        default="2024-06-01T00:00:00Z",
        help="Inclusive start of the time range",
    )
    parser.add_argument(
        "--time-to",
        default="2024-06-30T23:59:59Z",
        help="Inclusive end of the time range",
    )
    parser.add_argument(
        "--bands",
        nargs="+",
        default=["B04", "B08"],
        help="Band names to extract (default: B04 B08)",
    )
    parser.add_argument("--width", type=int, default=512, help="Output raster width in pixels")
    parser.add_argument("--height", type=int, default=512, help="Output raster height in pixels")
    parser.add_argument(
        "--harmonize-values",
        action="store_true",
        help="Enable harmonized output values instead of raw DN",
    )
    parser.add_argument(
        "--max-cloud-cover",
        type=float,
        default=20.0,
        help="Maximum tile cloud cover percentage",
    )
    parser.add_argument(
        "--client-id",
        default=None,
        help="Sentinel Hub OAuth client ID (prefer TERRAVAULT_CDSE_SH_CLIENT_ID env var)",
    )
    parser.add_argument(
        "--client-secret",
        default=None,
        help="Sentinel Hub OAuth client secret (prefer TERRAVAULT_CDSE_SH_CLIENT_SECRET env var)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output GeoTIFF path (default: examples/switzerland_patch/data/raw_patch.tif)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    SentinelHubAuthConfig, ProcessPatchConfig, fetch_sentinel2_patch, load_dotenv = _load_terravault()
    load_dotenv(args.env_file)

    env_auth = SentinelHubAuthConfig.from_env(os.environ)
    auth = SentinelHubAuthConfig(
        client_id=args.client_id or (env_auth.client_id if env_auth else None),
        client_secret=args.client_secret or (env_auth.client_secret if env_auth else None),
    )

    if not auth.client_id or not auth.client_secret:
        print(
            "Missing Sentinel Hub OAuth client credentials. Set "
            "TERRAVAULT_CDSE_SH_CLIENT_ID and TERRAVAULT_CDSE_SH_CLIENT_SECRET.",
            file=sys.stderr,
        )
        return 2

    root = Path(__file__).resolve().parent
    output_path = Path(args.output) if args.output else (root / "data" / "raw_patch.tif")
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()

    cfg = ProcessPatchConfig(
        bbox=args.bbox,
        time_from=args.time_from,
        time_to=args.time_to,
        bands=args.bands,
        width=args.width,
        height=args.height,
        harmonize_values=args.harmonize_values,
        max_cloud_cover=args.max_cloud_cover,
    )
    path = fetch_sentinel2_patch(cfg, output_path=output_path, auth=auth)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
