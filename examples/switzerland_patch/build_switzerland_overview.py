#!/usr/bin/env python3
"""Build a credential-free Switzerland overview from public STAC thumbnails."""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests


def _load_dependencies():
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "This overview helper requires Pillow. Install with: pip install -e '.[overview]'"
        ) from exc

    try:
        import pystac

        from terravault.catalog import CatalogClient, SWITZERLAND_BBOX
        from terravault.dataset_catalog import DatasetCatalog
        from terravault.force_pipeline import ForcePipelineDatabase
        from terravault.process_api import output_size_for_bbox
        from terravault.spatial import geometry_area
        from terravault.storage import tile_id_from_item
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root))
        import pystac

        from terravault.catalog import CatalogClient, SWITZERLAND_BBOX
        from terravault.dataset_catalog import DatasetCatalog
        from terravault.force_pipeline import ForcePipelineDatabase
        from terravault.process_api import output_size_for_bbox
        from terravault.spatial import geometry_area
        from terravault.storage import tile_id_from_item
    return (
        Image,
        pystac,
        CatalogClient,
        DatasetCatalog,
        ForcePipelineDatabase,
        SWITZERLAND_BBOX,
        output_size_for_bbox,
        tile_id_from_item,
        geometry_area,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble public Sentinel-2 L2A quicklooks into a country-wide "
            "single-day or rolling-latest visual overview."
        )
    )
    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument(
        "--date",
        default=None,
        help="UTC acquisition day (default: 2026-07-04 when --latest is absent)",
    )
    date_group.add_argument(
        "--latest",
        action="store_true",
        help="Select the latest near-full-footprint item per tile in a lookback window",
    )
    date_group.add_argument(
        "--dataset-db",
        default=None,
        metavar="DUCKDB",
        help=(
            "Build from completed STAC documents recorded in a TerraVault DuckDB "
            "instead of querying CDSE again"
        ),
    )
    parser.add_argument(
        "--lookback-days",
        type=float,
        default=14,
        help="Catalogue window used with --latest (default: 14)",
    )
    parser.add_argument(
        "--database-selection",
        choices=("latest-per-tile", "all"),
        default="latest-per-tile",
        help=(
            "When reading DuckDB, render the latest scene per tile or every "
            "stored scene (default: latest-per-tile)"
        ),
    )
    parser.add_argument(
        "--max-cloud-cover",
        type=float,
        default=20.0,
        help="Maximum granule cloud cover percentage (default: 20)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=2400,
        help="Mosaic width in pixels (default: 2400)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output image path (default: examples/switzerland_patch/data/...jpg)",
    )
    return parser


def _mercator_y(latitude: float) -> float:
    latitude = max(-85.05112878, min(85.05112878, latitude))
    radians = math.radians(latitude)
    return math.log(math.tan(math.pi / 4 + radians / 2))


def _bbox_pixels(
    item_bbox: list[float],
    country_bbox: list[float],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    west, south, east, north = country_bbox
    item_west, item_south, item_east, item_north = item_bbox
    north_y = _mercator_y(north)
    south_y = _mercator_y(south)

    x0 = round((item_west - west) / (east - west) * width)
    x1 = round((item_east - west) / (east - west) * width)
    y0 = round((north_y - _mercator_y(item_north)) / (north_y - south_y) * height)
    y1 = round((north_y - _mercator_y(item_south)) / (north_y - south_y) * height)
    return x0, y0, x1, y1


def _download_image(session: requests.Session, url: str, Image):
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=120)
            response.raise_for_status()
            return Image.open(io.BytesIO(response.content)).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def _paste_thumbnail(canvas, thumbnail, bounds, Image) -> None:
    x0, y0, x1, y1 = bounds
    target_width = max(1, x1 - x0)
    target_height = max(1, y1 - y0)
    resized = thumbnail.resize((target_width, target_height), Image.Resampling.BILINEAR)

    left = max(0, x0)
    top = max(0, y0)
    right = min(canvas.width, x1)
    bottom = min(canvas.height, y1)
    if left >= right or top >= bottom:
        return

    crop_box = (left - x0, top - y0, right - x0, bottom - y0)
    visible = resized.crop(crop_box)
    # CDSE quicklooks encode out-of-swath pixels as near-black JPEG pixels.
    # Mask those pixels so overlapping swaths can fill the gaps.
    mask = visible.convert("L").point(lambda pixel: 255 if pixel > 8 else 0)
    canvas.paste(visible, (left, top), mask)


def _select_latest_per_tile(items, catalog, tile_id_from_item, geometry_area):
    items_by_tile = {}
    for item in items:
        items_by_tile.setdefault(tile_id_from_item(item), []).append(item)
    selected = []
    for tile_items in items_by_tile.values():
        largest_footprint = max(geometry_area(item.geometry) for item in tile_items)
        candidates = [
            item
            for item in tile_items
            if largest_footprint <= 0
            or geometry_area(item.geometry) >= largest_footprint * 0.9
        ]
        selected.append(
            max(
                candidates,
                key=lambda item: (
                    catalog.item_datetime(item),
                    str(
                        item.properties.get("updated")
                        or item.properties.get("created")
                        or item.properties.get("published")
                        or ""
                    ),
                    item.id,
                ),
            )
        )
    return selected


def main() -> int:
    args = build_parser().parse_args()
    (
        Image,
        pystac,
        CatalogClient,
        DatasetCatalog,
        ForcePipelineDatabase,
        SWITZERLAND_BBOX,
        output_size_for_bbox,
        tile_id_from_item,
        geometry_area,
    ) = _load_dependencies()
    if args.lookback_days <= 0:
        raise SystemExit("--lookback-days must be positive")
    if not 0 <= args.max_cloud_cover <= 100:
        raise SystemExit("--max-cloud-cover must be between 0 and 100")

    if args.width < 100:
        raise SystemExit("--width must be at least 100 pixels")
    _, height = output_size_for_bbox(
        list(SWITZERLAND_BBOX),
        max_dimension=min(args.width, 2500),
    )
    if args.width > 2500:
        height = round(height * args.width / 2500)

    catalog = CatalogClient(
        collections=["sentinel-2-l2a"],
        bbox=list(SWITZERLAND_BBOX),
        max_cloud_cover=args.max_cloud_cover,
    )
    dataset_db = None
    dataset_kind = None
    register_overview = None
    failures = []
    if args.dataset_db is not None:
        dataset_db = Path(args.dataset_db).expanduser().resolve()
        if not dataset_db.is_file():
            raise SystemExit(f"Dataset DuckDB does not exist: {dataset_db}")
        import duckdb

        with duckdb.connect(str(dataset_db), read_only=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT table_name
                      FROM information_schema.tables
                     WHERE table_schema = 'main'
                    """
                ).fetchall()
            }
        if "scenes" in tables:
            dataset = ForcePipelineDatabase(dataset_db)
            rows = dataset.scenes()
            dataset_kind = "force-pipeline"
            register_overview = dataset.set_info
        elif "items" in tables:
            dataset = DatasetCatalog(dataset_db)
            rows = dataset.query_items(bbox=tuple(SWITZERLAND_BBOX))
            dataset_kind = "raster-dataset"

            def register_dataset_overview(key, value):
                dataset.set_dataset_info(
                    key,
                    value if isinstance(value, str) else json.dumps(value),
                )

            register_overview = register_dataset_overview
        else:
            raise SystemExit(
                f"DuckDB has neither a TerraVault scenes nor items table: {dataset_db}"
            )
        catalogue_item_count = len(rows)
        items = []
        for row in rows:
            metadata_path = Path(row["metadata_path"])
            try:
                document = json.loads(metadata_path.read_text(encoding="utf-8"))
                item = pystac.Item.from_dict(document)
                if item.id != row["item_id"]:
                    raise ValueError("saved STAC id differs from the DuckDB item id")
                cloud_cover = item.properties.get("eo:cloud_cover")
                if cloud_cover is None or float(cloud_cover) <= args.max_cloud_cover:
                    items.append(item)
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {
                        "id": row["item_id"],
                        "metadata_path": str(metadata_path),
                        "error": str(exc),
                    }
                )
        if args.database_selection == "latest-per-tile":
            items = _select_latest_per_tile(
                items,
                catalog,
                tile_id_from_item,
                geometry_area,
            )
        if not items:
            print("No completed thumbnail scenes were found in DuckDB.", file=sys.stderr)
            return 1
        acquisition_times = [catalog.item_datetime(item) for item in items]
        start = min(acquisition_times)
        end = max(acquisition_times)
        acquisition_date = end.date()
        selection_label = args.database_selection.replace("-", "_")
        output_label = f"database_{selection_label}_{acquisition_date.isoformat()}"
        mode = f"duckdb-{args.database_selection}"
        source = (
            f"saved STAC thumbnail assets selected from TerraVault {dataset_kind} DuckDB"
        )
        lookback_days = None
    else:
        if args.latest:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=args.lookback_days)
            acquisition_date = end.date()
            output_label = f"latest_{acquisition_date.isoformat()}"
            mode = "latest-per-tile"
            lookback_days = args.lookback_days
        else:
            try:
                acquisition_date = date.fromisoformat(args.date or "2026-07-04")
            except ValueError as exc:
                raise SystemExit("--date must use YYYY-MM-DD") from exc
            start = datetime.combine(
                acquisition_date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            end = start + timedelta(days=1)
            output_label = acquisition_date.isoformat()
            mode = "single-day"
            lookback_days = None
        items = list(catalog.search(start_datetime=start, end_datetime=end))
        catalogue_item_count = len(items)
        if args.latest:
            items = _select_latest_per_tile(
                items,
                catalog,
                tile_id_from_item,
                geometry_area,
            )
        source = "public CDSE STAC thumbnail assets"

    items = [item for item in items if "thumbnail" in item.assets and item.bbox]
    if not items:
        print("No public Sentinel-2 thumbnails matched the requested day.", file=sys.stderr)
        return 1

    # Paint cloudier granules first so clearer overlaps win.
    items.sort(
        key=lambda item: float(item.properties.get("eo:cloud_cover") or 0.0),
        reverse=True,
    )
    canvas = Image.new("RGB", (args.width, height), color=(0, 0, 0))
    used_items = []

    with requests.Session() as session:
        for item in items:
            try:
                thumbnail = _download_image(session, item.assets["thumbnail"].href, Image)
                bounds = _bbox_pixels(
                    list(item.bbox),
                    list(SWITZERLAND_BBOX),
                    args.width,
                    height,
                )
                _paste_thumbnail(canvas, thumbnail, bounds, Image)
                used_items.append(
                    {
                        "id": item.id,
                        "datetime": catalog.item_datetime(item).isoformat(),
                        "cloud_cover": item.properties.get("eo:cloud_cover"),
                        "bbox": list(item.bbox),
                        "thumbnail": item.assets["thumbnail"].href,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                failures.append({"id": item.id, "error": str(exc)})

    root = Path(__file__).resolve().parent
    output_path = (
        Path(args.output)
        if args.output
        else root / "data" / f"switzerland_{output_label}_overview.jpg"
    )
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, quality=92, optimize=True, progressive=True)

    manifest = {
        "output": str(output_path),
        "mode": mode,
        "date": acquisition_date.isoformat(),
        "time_from": start.isoformat(),
        "time_to": end.isoformat(),
        "lookback_days": lookback_days,
        "dataset_db": None if dataset_db is None else str(dataset_db),
        "dataset_kind": dataset_kind,
        "max_cloud_cover": args.max_cloud_cover,
        "catalogue_item_count": catalogue_item_count,
        "selected_item_count": len(items),
        "used_item_count": len(used_items),
        "bbox_wgs84": list(SWITZERLAND_BBOX),
        "width": args.width,
        "height": height,
        "source": source,
        "purpose": "visual overview only; not an analysis-ready geospatial raster",
        "items": used_items,
        "failures": failures,
    }
    manifest_path = output_path.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if register_overview is not None:
        item_ids = [entry["id"] for entry in used_items]
        register_overview("latest_overview_path", str(output_path))
        register_overview("latest_overview_manifest_path", str(manifest_path))
        register_overview("latest_overview_item_ids", item_ids)
        register_overview("latest_overview_max_cloud_cover", args.max_cloud_cover)
        selection_key = args.database_selection.replace("-", "_")
        register_overview(f"overview_{selection_key}_path", str(output_path))
        register_overview(
            f"overview_{selection_key}_manifest_path",
            str(manifest_path),
        )
        register_overview(f"overview_{selection_key}_item_ids", item_ids)
        if dataset_kind == "force-pipeline":
            dataset.close()

    print(f"Overview : {output_path}")
    print(f"Manifest : {manifest_path}")
    print(f"Granules : {len(used_items)} used, {len(failures)} failed")
    return 0 if used_items else 1


if __name__ == "__main__":
    raise SystemExit(main())
