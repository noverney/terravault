#!/usr/bin/env python3
"""Fetch the latest Sentinel-2 patch centred on Basel."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASEL_CENTER_LON = 7.5886
BASEL_CENTER_LAT = 47.5596


def _load_terravault() -> tuple[object, object, object, object, object, object]:
    try:
        from terravault.auth import SentinelHubAuthConfig
        from terravault.catalog import CatalogClient
        from terravault.env import load_dotenv
        from terravault.process_api import ProcessPatchConfig, bbox_from_center, fetch_sentinel2_patch
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root))
        from terravault.auth import SentinelHubAuthConfig
        from terravault.catalog import CatalogClient
        from terravault.env import load_dotenv
        from terravault.process_api import ProcessPatchConfig, bbox_from_center, fetch_sentinel2_patch
    return (
        SentinelHubAuthConfig,
        CatalogClient,
        ProcessPatchConfig,
        bbox_from_center,
        fetch_sentinel2_patch,
        load_dotenv,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover the latest Sentinel-2 scene over Basel and fetch a high-resolution patch "
            "using the CDSE Sentinel Hub Process API."
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional .env file to load before resolving credentials",
    )
    parser.add_argument(
        "--center-lon",
        type=float,
        default=BASEL_CENTER_LON,
        help="Patch center longitude in WGS-84 (default: Basel center)",
    )
    parser.add_argument(
        "--center-lat",
        type=float,
        default=BASEL_CENTER_LAT,
        help="Patch center latitude in WGS-84 (default: Basel center)",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=1024,
        help="Output width and height in pixels (default: 1024)",
    )
    parser.add_argument(
        "--resolution-m",
        type=float,
        default=10.0,
        help="Target ground sampling distance in meters per pixel (default: 10)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="How many days back to search for the latest scene (default: 30)",
    )
    parser.add_argument(
        "--collection",
        default="sentinel-2-l2a",
        help="Collection to query (default: sentinel-2-l2a)",
    )
    parser.add_argument(
        "--max-cloud-cover",
        type=float,
        default=20.0,
        help="Maximum cloud cover percentage for candidate scenes (default: 20)",
    )
    parser.add_argument(
        "--bands",
        nargs="+",
        default=["B04", "B03", "B02"],
        help="Bands to include in the patch (default: true color B04 B03 B02)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output patch path (default: examples/switzerland_patch/data/basel_latest_patch.tif)",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Skip the rendered PNG quicklook output",
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
    return parser


def _format_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _preview_config(
    ProcessPatchConfig: object,
    *,
    bbox: list[float],
    time_from: str,
    time_to: str,
    size: int,
    collection: str,
    max_cloud_cover: float | None,
    bands: list[str],
    gain: float = 2.5,
):
    return ProcessPatchConfig(
        bbox=bbox,
        time_from=time_from,
        time_to=time_to,
        bands=bands,
        width=size,
        height=size,
        collection=collection,
        output_format="image/png",
        units="REFLECTANCE",
        sample_type="AUTO",
        gain=gain,
        harmonize_values=True,
        max_cloud_cover=max_cloud_cover,
    )


def main() -> int:
    args = build_parser().parse_args()
    (
        SentinelHubAuthConfig,
        CatalogClient,
        ProcessPatchConfig,
        bbox_from_center,
        fetch_sentinel2_patch,
        load_dotenv,
    ) = _load_terravault()
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

    bbox = bbox_from_center(
        center_lon=args.center_lon,
        center_lat=args.center_lat,
        width=args.size,
        height=args.size,
        resolution_m=args.resolution_m,
    )

    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=args.lookback_days)
    client = CatalogClient(
        collections=[args.collection],
        bbox=bbox,
        max_cloud_cover=args.max_cloud_cover,
    )
    latest = client.latest_item(start_datetime=start, end_datetime=now)
    if latest is None:
        print(
            "No Sentinel-2 scene found for the Basel patch in the requested lookback window.",
            file=sys.stderr,
        )
        return 1

    item_dt = client.item_datetime(latest)
    time_from = _format_utc(item_dt - timedelta(minutes=30))
    time_to = _format_utc(item_dt + timedelta(minutes=30))

    root = Path(__file__).resolve().parent
    output_path = (
        Path(args.output)
        if args.output
        else (root / "data" / "basel_latest_patch.tif")
    )
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()

    raw_cfg = ProcessPatchConfig(
        bbox=bbox,
        time_from=time_from,
        time_to=time_to,
        bands=args.bands,
        width=args.size,
        height=args.size,
        collection=args.collection,
        output_format="image/tiff",
        units="DN",
        sample_type="UINT16",
        gain=1.0,
        harmonize_values=False,
        max_cloud_cover=args.max_cloud_cover,
    )
    path = fetch_sentinel2_patch(raw_cfg, output_path=output_path, auth=auth)

    preview_path = output_path.with_suffix(".png")
    false_color_path = output_path.with_name(f"{output_path.stem}_false_color.png")
    metadata_path = output_path.with_suffix(".json")
    if not args.no_preview:
        preview_cfg = _preview_config(
            ProcessPatchConfig,
            bbox=bbox,
            time_from=time_from,
            time_to=time_to,
            size=args.size,
            collection=args.collection,
            max_cloud_cover=args.max_cloud_cover,
            bands=["B04", "B03", "B02"],
        )
        fetch_sentinel2_patch(preview_cfg, output_path=preview_path, auth=auth)

        false_color_cfg = _preview_config(
            ProcessPatchConfig,
            bbox=bbox,
            time_from=time_from,
            time_to=time_to,
            size=args.size,
            collection=args.collection,
            max_cloud_cover=args.max_cloud_cover,
            bands=["B08", "B04", "B03"],
        )
        fetch_sentinel2_patch(false_color_cfg, output_path=false_color_path, auth=auth)

    metadata = {
        "item_id": latest.id,
        "item_time": _format_utc(item_dt),
        "collection": args.collection,
        "bbox": bbox,
        "raw_output": str(path),
        "raw_bands": args.bands,
        "preview_outputs": {
            "true_color": None if args.no_preview else str(preview_path),
            "false_color": None if args.no_preview else str(false_color_path),
        },
        "preview_bands": {
            "true_color": ["B04", "B03", "B02"],
            "false_color": ["B08", "B04", "B03"],
        },
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(f"Latest item : {latest.id}")
    print(f"Item time   : {_format_utc(item_dt)}")
    print(f"BBox        : {bbox}")
    print(f"Raw output  : {path}")
    print(f"Metadata    : {metadata_path}")
    if not args.no_preview:
        print(f"Preview RGB : {preview_path}")
        print(f"Preview FCI : {false_color_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
