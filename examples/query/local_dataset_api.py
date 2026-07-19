#!/usr/bin/env python3
"""Query and optionally stitch a locally downloaded TerraVault dataset.

This example talks only to the local DuckDB catalogue and raster pieces. It
does not contact Copernicus and does not require API or S3 credentials.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from terravault import DatasetCatalog, ExtractionConfig, RasterExtractor

DEFAULT_DATASET_DB = Path("satellite_data/switzerland_ndvi/dataset.duckdb")
DEFAULT_BBOX = (8.45, 47.20, 8.65, 47.35)
DEFAULT_ASSET_KEYS = ("B04_10m", "B08_10m", "SCL_20m", "CLD_20m")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Query TerraVault's local DuckDB catalogue and optionally stream "
            "the intersecting raster pieces into one stitched COG."
        )
    )
    parser.add_argument(
        "--dataset-db",
        type=Path,
        default=DEFAULT_DATASET_DB,
        help=f"Local dataset DuckDB path (default: {DEFAULT_DATASET_DB})",
    )
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        default=DEFAULT_BBOX,
        help="WGS84 query bounding box (default: a Zurich-area example)",
    )
    parser.add_argument(
        "--asset-keys",
        nargs="+",
        default=list(DEFAULT_ASSET_KEYS),
        metavar="KEY",
        help="Local raster features to return, in desired output-band order",
    )
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum paths printed in text mode; 0 prints every path (default: 20)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete query result as JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional stitched COG path; omit for a metadata-only query",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan and size --output without writing its raster pixels",
    )
    parser.add_argument(
        "--selection",
        choices=("latest-per-tile", "all"),
        default="latest-per-tile",
        help="Temporal selection for stitched output (default: latest-per-tile)",
    )
    parser.add_argument(
        "--target-crs",
        default="EPSG:2056",
        help="Stitched output CRS (default: EPSG:2056 for Switzerland)",
    )
    parser.add_argument(
        "--resolution",
        type=float,
        default=None,
        help="Output pixel size; default uses the finest requested native resolution",
    )
    parser.add_argument(
        "--resampling",
        default="near",
        help="GDAL resampling method (default: near, safe for masks)",
    )
    parser.add_argument(
        "--warp-memory-mib",
        type=int,
        default=256,
        help="Memory budget for each GDAL operation (default: 256)",
    )
    parser.add_argument(
        "--max-output-gib",
        type=float,
        default=16.0,
        help="Uncompressed stitched-output safety limit (default: 16)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing stitched output",
    )
    return parser


def _date_boundary(value: date | None, *, inclusive_end: bool) -> datetime | None:
    if value is None:
        return None
    boundary = time.max if inclusive_end else time.min
    return datetime.combine(value, boundary, tzinfo=timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot encode {type(value).__name__} as JSON")


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be zero or positive")
    if args.dry_run and args.output is None:
        parser.error("--dry-run requires --output")

    dataset_db = args.dataset_db.expanduser().resolve()
    if not dataset_db.is_file():
        parser.error(f"dataset database does not exist: {dataset_db}")

    bbox = tuple(args.bbox)
    asset_keys = tuple(dict.fromkeys(args.asset_keys))
    start_datetime = _date_boundary(args.start_date, inclusive_end=False)
    end_datetime = _date_boundary(args.end_date, inclusive_end=True)

    catalog = DatasetCatalog(dataset_db)
    summary = catalog.summary()
    pieces = catalog.query_raster_pieces(
        bbox=bbox,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        asset_keys=asset_keys,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "dataset": summary,
                    "query": {
                        "bbox": bbox,
                        "start_datetime": start_datetime,
                        "end_datetime": end_datetime,
                        "asset_keys": asset_keys,
                    },
                    "pieces": pieces,
                },
                indent=2,
                default=_json_value,
            )
        )
    else:
        by_asset = Counter(piece["asset_key"] for piece in pieces)
        selected_bytes = sum(piece["byte_count"] or 0 for piece in pieces)
        missing_files = sum(
            not Path(piece["local_path"]).is_file() for piece in pieces
        )
        print(f"Dataset: {summary['path']}")
        print(
            f"Indexed: {summary['items']} items, "
            f"{summary['completed_assets']} completed assets, "
            f"{_format_bytes(summary['completed_bytes'])}"
        )
        print(
            f"Query: {len(pieces)} pieces, {_format_bytes(selected_bytes)}, "
            f"missing local files={missing_files}"
        )
        print(
            "By asset: "
            + ", ".join(f"{key}={by_asset.get(key, 0)}" for key in asset_keys)
        )
        displayed = pieces if args.limit == 0 else pieces[: args.limit]
        for piece in displayed:
            print(
                f"{piece['acquisition_time'].isoformat()} "
                f"tile={piece['tile_id']} asset={piece['asset_key']} "
                f"path={piece['local_path']}"
            )
        if len(displayed) < len(pieces):
            print(f"... {len(pieces) - len(displayed)} additional paths omitted")

    if args.output is None:
        return 0

    result = RasterExtractor(
        ExtractionConfig(
            dataset_db=dataset_db,
            bbox=bbox,
            asset_keys=asset_keys,
            output_path=args.output,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            selection=args.selection,
            target_crs=args.target_crs,
            resolution=args.resolution,
            resampling=args.resampling,
            warp_memory_mib=args.warp_memory_mib,
            max_output_gib=args.max_output_gib,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
    ).extract()
    action = "planned" if result.dry_run else "written"
    print(
        f"Stitched output {action}: {result.output_path} "
        f"({result.width}x{result.height}, {result.band_count} bands, "
        f"{result.source_count} source pieces)"
    )
    print(
        "Estimated uncompressed size: "
        f"{_format_bytes(result.estimated_uncompressed_bytes)}"
    )
    print(f"Manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
