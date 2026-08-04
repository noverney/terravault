#!/usr/bin/env python3
"""Fetch and verify one complete CDSE Sentinel-2 L1C SAFE for CI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests

from terravault.l1c_download import L1CDownloadConfig, L1CProductDownloader
from terravault.s3_downloader import S3Config

STAC_ITEM_URL = (
    "https://stac.dataspace.copernicus.eu/v1/collections/"
    "sentinel-2-l1c/items/{item_id}"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args()


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Required environment variable is not set: {name}")
    return value


def main() -> int:
    args = parse_args()
    output_root = args.output_root.expanduser().resolve()
    metadata_dir = output_root / "_terravault" / "ci"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    item_path = metadata_dir / f"{args.item_id}.json"

    response = requests.get(STAC_ITEM_URL.format(item_id=args.item_id), timeout=60)
    response.raise_for_status()
    item = response.json()
    if item.get("id") != args.item_id:
        raise RuntimeError("CDSE returned a different STAC item than requested")
    item_path.write_text(json.dumps(item, indent=2, sort_keys=True), encoding="utf-8")

    result = L1CProductDownloader(
        L1CDownloadConfig(
            item_path=item_path,
            output_root=output_root,
            s3=S3Config(
                access_key=required_env("TERRAVAULT_CDSE_S3_ACCESS_KEY"),
                secret_key=required_env("TERRAVAULT_CDSE_S3_SECRET_KEY"),
                endpoint_url=os.environ.get(
                    "TERRAVAULT_CDSE_S3_ENDPOINT",
                    "https://eodata.dataspace.copernicus.eu",
                ),
                region_name=os.environ.get("TERRAVAULT_CDSE_S3_REGION", "default"),
            ),
        )
    ).run()

    if result.source_mode != "s3-tree":
        raise RuntimeError(f"Expected an S3 tree download, got {result.source_mode}")
    if not result.product_path.is_dir():
        raise RuntimeError(f"Downloaded SAFE directory is missing: {result.product_path}")
    if result.file_count < 1 or result.byte_count < 1:
        raise RuntimeError("Downloaded SAFE unexpectedly contains no data")

    required = [
        result.product_path / "manifest.safe",
        result.product_path / "MTD_MSIL1C.xml",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"Downloaded SAFE is incomplete; missing {missing}")

    summary = {
        "byte_count": result.byte_count,
        "file_count": result.file_count,
        "item_id": result.item_id,
        "product_name": result.product_name,
        "source_mode": result.source_mode,
        "status": result.status,
    }
    result_path = output_root / "fetch-result.json"
    result_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("The downloaded data is runner-temporary and no artifact upload step is configured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

