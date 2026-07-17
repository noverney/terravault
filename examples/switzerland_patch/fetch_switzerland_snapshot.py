#!/usr/bin/env python3
"""Fetch a country-wide Sentinel-2 L2A test snapshot for Switzerland.

The Process API stitches source granules on the server. The raw export is
split into type-compatible GeoTIFF groups so categorical and angle layers do
not lose precision. A separate PNG is generated for visual inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path


SPECTRAL_BANDS = [
    "B01",
    "B02",
    "B03",
    "B04",
    "B05",
    "B06",
    "B07",
    "B08",
    "B8A",
    "B09",
    "B11",
    "B12",
    "AOT",
]
QUALITY_BANDS = ["SCL", "SNW", "CLD", "dataMask"]
ANGLE_BANDS = [
    "sunAzimuthAngles",
    "sunZenithAngles",
    "viewAzimuthMean",
    "viewZenithMean",
]


def _load_terravault():
    try:
        from terravault.auth import SentinelHubAuthConfig
        from terravault.catalog import SWITZERLAND_BBOX
        from terravault.env import load_dotenv
        from terravault.process_api import (
            ProcessPatchConfig,
            build_process_request,
            fetch_sentinel2_patch,
            output_size_for_bbox,
        )
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root))
        from terravault.auth import SentinelHubAuthConfig
        from terravault.catalog import SWITZERLAND_BBOX
        from terravault.env import load_dotenv
        from terravault.process_api import (
            ProcessPatchConfig,
            build_process_request,
            fetch_sentinel2_patch,
            output_size_for_bbox,
        )
    return (
        SentinelHubAuthConfig,
        SWITZERLAND_BBOX,
        load_dotenv,
        ProcessPatchConfig,
        build_process_request,
        fetch_sentinel2_patch,
        output_size_for_bbox,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a server-side stitched, country-wide Sentinel-2 L2A snapshot "
            "with every Process API data layer and a true-colour overview."
        )
    )
    parser.add_argument(
        "--date",
        default="2026-07-04",
        help="UTC acquisition day to mosaic (default: 2026-07-04, a verified clear test day)",
    )
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        default=None,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="WGS-84 bounds (default: TerraVault Switzerland bbox)",
    )
    parser.add_argument(
        "--max-dimension",
        type=int,
        default=2000,
        help="Longest output dimension in pixels, at most 2500 (default: 2000)",
    )
    parser.add_argument(
        "--max-cloud-cover",
        type=float,
        default=20.0,
        help="Maximum source-tile cloud cover percentage (default: 20)",
    )
    parser.add_argument(
        "--mosaicking-order",
        choices=("mostRecent", "leastRecent", "leastCC"),
        default="leastCC",
        help="Process API source priority (default: leastCC)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (default: examples/switzerland_patch/data/switzerland_DATE)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional .env file containing Sentinel Hub OAuth credentials",
    )
    parser.add_argument("--client-id", default=None, help="Sentinel Hub OAuth client ID")
    parser.add_argument("--client-secret", default=None, help="Sentinel Hub OAuth client secret")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write request JSON and manifest without authenticating or processing imagery",
    )
    return parser


def _utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request_configs(
    ProcessPatchConfig,
    *,
    bbox: list[float],
    time_from: str,
    time_to: str,
    width: int,
    height: int,
    max_cloud_cover: float,
    mosaicking_order: str,
) -> dict[str, object]:
    common = {
        "bbox": bbox,
        "time_from": time_from,
        "time_to": time_to,
        "width": width,
        "height": height,
        "collection": "sentinel-2-l2a",
        "max_cloud_cover": max_cloud_cover,
        "mosaicking_order": mosaicking_order,
    }
    return {
        "spectral": ProcessPatchConfig(
            **common,
            bands=SPECTRAL_BANDS,
            units=["DN"] * len(SPECTRAL_BANDS),
            sample_type="UINT16",
            output_format="image/tiff",
            harmonize_values=False,
        ),
        "quality": ProcessPatchConfig(
            **common,
            bands=QUALITY_BANDS,
            units=["DN", "PERCENT", "PERCENT", "DN"],
            sample_type="UINT8",
            output_format="image/tiff",
            harmonize_values=False,
        ),
        "angles": ProcessPatchConfig(
            **common,
            bands=ANGLE_BANDS,
            units=["DEGREES"] * len(ANGLE_BANDS),
            sample_type="FLOAT32",
            output_format="image/tiff",
            harmonize_values=True,
        ),
        "overview": ProcessPatchConfig(
            **common,
            bands=["B04", "B03", "B02"],
            units=["REFLECTANCE"] * 3,
            sample_type="AUTO",
            output_format="image/png",
            gain=2.5,
            harmonize_values=True,
        ),
    }


def main() -> int:
    args = build_parser().parse_args()
    (
        SentinelHubAuthConfig,
        SWITZERLAND_BBOX,
        load_dotenv,
        ProcessPatchConfig,
        build_process_request,
        fetch_sentinel2_patch,
        output_size_for_bbox,
    ) = _load_terravault()

    try:
        snapshot_date = date.fromisoformat(args.date)
    except ValueError as exc:
        raise SystemExit("--date must use YYYY-MM-DD") from exc

    bbox = list(args.bbox or SWITZERLAND_BBOX)
    width, height = output_size_for_bbox(bbox, max_dimension=args.max_dimension)
    start = datetime.combine(snapshot_date, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    time_from = _utc_string(start)
    time_to = _utc_string(end)

    root = Path(__file__).resolve().parent
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else root / "data" / f"switzerland_{snapshot_date.isoformat()}"
    )
    if not output_dir.is_absolute():
        output_dir = (Path.cwd() / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    configs = _request_configs(
        ProcessPatchConfig,
        bbox=bbox,
        time_from=time_from,
        time_to=time_to,
        width=width,
        height=height,
        max_cloud_cover=args.max_cloud_cover,
        mosaicking_order=args.mosaicking_order,
    )
    output_paths = {
        "spectral": output_dir / "switzerland_spectral.tif",
        "quality": output_dir / "switzerland_quality.tif",
        "angles": output_dir / "switzerland_angles.tif",
        "overview": output_dir / "switzerland_overview.png",
    }

    manifest = {
        "status": "planned" if args.dry_run else "processing",
        "snapshot_date": snapshot_date.isoformat(),
        "time_from": time_from,
        "time_to": time_to,
        "bbox_wgs84": bbox,
        "width": width,
        "height": height,
        "server_side_stitched": True,
        "note": (
            "A UTC day is a same-day mosaic, not a single simultaneous exposure; "
            "Switzerland spans multiple Sentinel-2 swaths."
        ),
        "outputs": {},
    }

    for name, config in configs.items():
        request_path = output_dir / f"{name}_request.json"
        request_path.write_text(
            json.dumps(build_process_request(config), indent=2),
            encoding="utf-8",
        )
        manifest["outputs"][name] = {
            "path": str(output_paths[name]),
            "request": str(request_path),
            "bands": list(config.bands),
            "units": config.units,
            "sample_type": config.sample_type,
            "status": "planned",
        }

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if args.dry_run:
        print(f"Dry-run manifest: {manifest_path}")
        print(f"Output size     : {width} x {height}")
        return 0

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

    try:
        for name, config in configs.items():
            path = fetch_sentinel2_patch(config, output_path=output_paths[name], auth=auth)
            manifest["outputs"][name]["status"] = "complete"
            manifest["outputs"][name]["bytes"] = path.stat().st_size
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if status_code == 401:
            print(
                "CDSE rejected the Sentinel Hub OAuth client (HTTP 401). Create a new "
                "OAuth client in the CDSE Dashboard and update the two *_SH_* values in .env.",
                file=sys.stderr,
            )
        else:
            print(f"Snapshot failed: {exc}", file=sys.stderr)
        return 1

    manifest["status"] = "complete"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Snapshot complete: {output_dir}")
    print(f"Manifest         : {manifest_path}")
    print(f"Overview         : {output_paths['overview']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
