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

    # Run a restartable native-resolution watcher for an explicit ROI
    terravault watch --bbox 5.96 45.82 10.49 47.81
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .auth import CDSEDownloadAuthConfig
from .pipeline import Pipeline, PipelineConfig
from .downloader import DownloadConfig
from .env import load_dotenv


def _setup_logging(verbose: bool, log_file: str | Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stream_handler]
    if log_file is not None:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=25 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(
        level=level,
        handlers=handlers,
        force=True,
    )
    if log_file is not None:
        logging.getLogger(__name__).info("Operational log file: %s", path.resolve())


def _default_log_file(args: argparse.Namespace) -> Path | None:
    configured = getattr(args, "log_file", None)
    if configured:
        return Path(configured)
    storage_root = getattr(args, "storage_root", None)
    if storage_root:
        return Path(storage_root) / "_terravault" / "logs" / f"{args.command}.log"
    dataset_db = getattr(args, "dataset_db", None)
    if dataset_db:
        return Path(dataset_db).parent / "_terravault" / "logs" / f"{args.command}.log"
    output_root = getattr(args, "output_root", None)
    if output_root:
        return Path(output_root) / "_terravault" / "logs" / f"{args.command}.log"
    return None


def cmd_run(args: argparse.Namespace) -> int:
    env_auth = CDSEDownloadAuthConfig.from_env(os.environ)
    auth = None
    access_token = args.cdse_access_token or (env_auth.access_token if env_auth else None)
    username = args.cdse_username or (env_auth.username if env_auth else None)
    password = args.cdse_password or (env_auth.password if env_auth else None)
    totp = args.cdse_totp or (env_auth.totp if env_auth else None)
    client_id = args.cdse_client_id or (env_auth.client_id if env_auth else None)
    if any((access_token, username, password, totp, client_id)):
        auth = CDSEDownloadAuthConfig(
            access_token=access_token,
            username=username,
            password=password,
            client_id=client_id or CDSEDownloadAuthConfig().client_id,
            totp=totp,
        )

    cfg = PipelineConfig(
        catalog_url=args.catalog_url,
        collections=args.collections or ["sentinel-2-l2a", "sentinel-2-l1c"],
        max_cloud_cover=args.max_cloud_cover,
        lookback_hours=args.lookback_hours,
        resume_overlap_hours=args.resume_overlap_hours,
        asset_keys=args.asset_keys or [],
        auth=auth,
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


def cmd_watch(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .rolling import RollingConfig, RollingIngestor, load_roi
    from .s3_downloader import S3Config

    try:
        roi = load_roi(bbox=args.bbox, geojson_path=args.roi)
        storage_root = Path(args.storage_root)
        state_db = (
            storage_root / "_terravault" / "state" / "rolling.db"
            if args.state_db is None
            else Path(args.state_db)
        )
        asset_profile = "custom" if args.asset_keys else args.asset_profile
        s3_config = None
        if asset_profile != "metadata-only":
            s3_config = S3Config(
                access_key=args.s3_access_key
                or os.environ.get("TERRAVAULT_CDSE_S3_ACCESS_KEY", ""),
                secret_key=args.s3_secret_key
                or os.environ.get("TERRAVAULT_CDSE_S3_SECRET_KEY", ""),
                endpoint_url=args.s3_endpoint
                or os.environ.get(
                    "TERRAVAULT_CDSE_S3_ENDPOINT",
                    "https://eodata.dataspace.copernicus.eu",
                ),
                region_name=args.s3_region
                or os.environ.get("TERRAVAULT_CDSE_S3_REGION", "default"),
                chunk_size=args.s3_chunk_mib * 1024 * 1024,
            )
        config = RollingConfig(
            roi=roi,
            catalog_url=args.catalog_url,
            collection=args.collection,
            max_cloud_cover=args.max_cloud_cover,
            lookback_hours=args.lookback_hours,
            bootstrap_lookback_days=args.bootstrap_lookback_days,
            poll_seconds=args.poll_seconds,
            max_items_per_cycle=args.max_items_per_cycle,
            max_jobs_per_cycle=args.max_jobs_per_cycle,
            asset_profile=asset_profile,
            asset_keys=args.asset_keys or [],
            require_all_assets=not args.allow_missing_assets,
            state_db=state_db,
            storage_root=storage_root,
            dataset_db=None if args.dataset_db is None else Path(args.dataset_db),
            lock_file=None if args.lock_file is None else Path(args.lock_file),
            max_attempts=args.max_attempts,
            retry_base_seconds=args.retry_base_seconds,
            retry_max_seconds=args.retry_max_seconds,
            quota_retry_seconds=args.quota_retry_seconds,
            retry_failed=args.retry_failed,
            s3=s3_config,
        )
        runner = RollingIngestor(config)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        result = runner.run(once=args.once)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        runner.close()

    print(
        f"Rolling cycle {result.run_id} – discovered={result.discovered}"
        f"  queued={result.queued}"
        f"  completed={result.completed}"
        f"  failed={result.failed}"
        f"  errors={len(result.errors)}"
        f"  stopped={result.stopped}"
    )
    for error in result.errors:
        print(f"  ERROR: {error}", file=sys.stderr)
    return 1 if result.errors or result.failed else 0


def cmd_historic(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone
    from pathlib import Path

    from .historical import HistoricalConfig, HistoricalIngestor, parse_utc_date
    from .rolling import RollingConfig, load_roi
    from .s3_downloader import S3Config

    try:
        roi = load_roi(bbox=args.bbox, geojson_path=args.roi)
        storage_root = Path(args.storage_root)
        state_db = (
            storage_root / "_terravault" / "state" / "history.db"
            if args.state_db is None
            else Path(args.state_db)
        )
        start = parse_utc_date(args.start_date)
        end = (
            datetime.now(timezone.utc)
            if args.end_date is None
            else parse_utc_date(args.end_date, inclusive_end=True)
        )
        asset_profile = "custom" if args.asset_keys else args.asset_profile
        s3_config = None
        if asset_profile != "metadata-only":
            s3_config = S3Config(
                access_key=args.s3_access_key
                or os.environ.get("TERRAVAULT_CDSE_S3_ACCESS_KEY", ""),
                secret_key=args.s3_secret_key
                or os.environ.get("TERRAVAULT_CDSE_S3_SECRET_KEY", ""),
                endpoint_url=args.s3_endpoint
                or os.environ.get(
                    "TERRAVAULT_CDSE_S3_ENDPOINT",
                    "https://eodata.dataspace.copernicus.eu",
                ),
                region_name=args.s3_region
                or os.environ.get("TERRAVAULT_CDSE_S3_REGION", "default"),
                chunk_size=args.s3_chunk_mib * 1024 * 1024,
            )
        rolling = RollingConfig(
            roi=roi,
            catalog_url=args.catalog_url,
            collection=args.collection,
            max_cloud_cover=args.max_cloud_cover,
            max_items_per_cycle=None,
            asset_profile=asset_profile,
            asset_keys=args.asset_keys or [],
            require_all_assets=not args.allow_missing_assets,
            state_db=state_db,
            storage_root=storage_root,
            dataset_db=None if args.dataset_db is None else Path(args.dataset_db),
            lock_file=None if args.lock_file is None else Path(args.lock_file),
            max_attempts=args.max_attempts,
            retry_base_seconds=args.retry_base_seconds,
            retry_max_seconds=args.retry_max_seconds,
            quota_retry_seconds=args.quota_retry_seconds,
            retry_failed=args.retry_retired,
            s3=s3_config,
        )
        config = HistoricalConfig(
            rolling=rolling,
            start_datetime=start,
            end_datetime=end,
            window_days=args.window_days,
            progress=not args.no_progress,
            max_jobs_per_batch=args.max_jobs_per_batch,
        )
        runner = HistoricalIngestor(config)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        result = runner.run_history()
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        runner.close()

    print(
        f"Historical run – windows={result.windows_completed}"
        f"  discovered={result.discovered}"
        f"  queued={result.queued}"
        f"  completed={result.completed}"
        f"  failed={result.failed}"
        f"  retired={result.retired}"
        f"  errors={len(result.errors)}"
        f"  stopped={result.stopped}"
    )
    for error in result.errors:
        print(f"  ERROR: {error}", file=sys.stderr)
    return 1 if result.errors or result.failed or result.retired else 0


def cmd_query(args: argparse.Namespace) -> int:
    from .dataset_catalog import DatasetCatalog
    from .historical import parse_utc_date

    try:
        catalog = DatasetCatalog(args.dataset_db)
        if args.summary:
            print(json.dumps(catalog.summary(), indent=2, default=str))
            return 0
        start = None if args.start_date is None else parse_utc_date(args.start_date)
        end = (
            None
            if args.end_date is None
            else parse_utc_date(args.end_date, inclusive_end=True)
        )
        bbox = None if args.bbox is None else tuple(args.bbox)
        pieces = catalog.query_raster_pieces(
            bbox=bbox,
            start_datetime=start,
            end_datetime=end,
            asset_keys=args.asset_keys,
            include_incomplete=args.include_incomplete,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.output == "json":
        print(json.dumps(pieces, indent=2, default=str))
    else:
        for piece in pieces:
            print(piece["local_path"])
    logging.getLogger(__name__).info(
        "Dataset query complete – database=%s pieces=%d",
        Path(args.dataset_db).resolve(),
        len(pieces),
    )
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    from .extractor import ExtractionConfig, RasterExtractor
    from .historical import parse_utc_date
    from .rolling import load_roi

    try:
        roi = load_roi(bbox=args.bbox, geojson_path=args.roi)
        start = None if args.start_date is None else parse_utc_date(args.start_date)
        end = (
            None
            if args.end_date is None
            else parse_utc_date(args.end_date, inclusive_end=True)
        )
        config = ExtractionConfig(
            dataset_db=Path(args.dataset_db),
            bbox=roi.bbox,
            asset_keys=tuple(args.asset_keys),
            output_path=Path(args.output),
            start_datetime=start,
            end_datetime=end,
            selection=args.selection,
            target_crs=args.target_crs,
            resolution=args.resolution,
            resampling=args.resampling,
            output_dtype=args.output_dtype,
            compression=args.compression,
            nodata=args.nodata,
            warp_memory_mib=args.warp_memory_mib,
            max_output_gib=args.max_output_gib,
            cutline_path=None if args.roi is None else Path(args.roi),
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
        result = RasterExtractor(config).extract()
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    action = "planned" if result.dry_run else "written"
    print(
        f"Extraction {action} – output={result.output_path}"
        f"  size={result.width}x{result.height}"
        f"  bands={result.band_count}"
        f"  dtype={result.output_dtype}"
        f"  sources={result.source_count}"
        f"  estimated_uncompressed_gib="
        f"{result.estimated_uncompressed_bytes / (1024**3):.2f}"
        f"  manifest={result.manifest_path}"
    )
    return 0


def cmd_force(args: argparse.Namespace) -> int:
    import shlex

    from .force import ForceConfig, ForcePostprocessor

    try:
        result = ForcePostprocessor(
            ForceConfig(
                input_path=Path(args.input),
                output_root=Path(args.output_root),
                basename=args.basename,
                runtime=args.runtime,
                docker_image=args.docker_image,
                docker_platform=args.docker_platform,
                mount_root=None if args.mount_root is None else Path(args.mount_root),
                target_crs=args.target_crs,
                origin_lon=args.origin_lon,
                origin_lat=args.origin_lat,
                tile_size=args.tile_size,
                resolution=args.resolution,
                resampling=args.resampling,
                output_nodata=args.output_nodata,
                output_dtype=args.output_dtype,
                jobs=args.jobs,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        ).run()
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    action = "planned" if result.status == "planned" else "complete"
    duplicate = " (already complete; skipped)" if result.skipped else ""
    print(
        f"FORCE postprocessing {action}{duplicate} – runtime={result.runtime}"
        f"  cube={result.cube_root}"
        f"  chips={len(result.chip_paths)}"
        f"  mosaic={result.mosaic_path}"
        f"  manifest={result.manifest_path}"
    )
    if result.status == "planned":
        print("Commands:")
        for command in result.commands:
            print(f"  {shlex.join(command)}")
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
        "--env-file",
        default=".env",
        metavar="PATH",
        help="Optional .env file to load before resolving credentials (default: .env)",
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
        "--resume-overlap-hours",
        type=int,
        default=72,
        metavar="H",
        help="Hours to re-query when resuming, to catch delayed products (default: 72)",
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
        "--cdse-access-token",
        default=None,
        metavar="TOKEN",
        help="Bearer token for CDSE downloads (prefer TERRAVAULT_CDSE_ACCESS_TOKEN env var)",
    )
    run_p.add_argument(
        "--cdse-username",
        default=None,
        metavar="USERNAME",
        help="CDSE username for token generation (prefer TERRAVAULT_CDSE_USERNAME env var)",
    )
    run_p.add_argument(
        "--cdse-password",
        default=None,
        metavar="PASSWORD",
        help="CDSE password for token generation (prefer TERRAVAULT_CDSE_PASSWORD env var)",
    )
    run_p.add_argument(
        "--cdse-totp",
        default=None,
        metavar="CODE",
        help="Optional TOTP code for CDSE accounts with MFA enabled",
    )
    run_p.add_argument(
        "--cdse-client-id",
        default=None,
        metavar="CLIENT_ID",
        help="OAuth client ID for download token requests (default: cdse-public)",
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
    run_p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Rotating log path (default: STORAGE_ROOT/_terravault/logs/run.log)",
    )
    run_p.set_defaults(func=cmd_run)

    # ---------------------------------------------------------------- watch
    watch_p = sub.add_parser(
        "watch",
        help="Run restartable rolling ingestion for an explicit region",
    )
    watch_p.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="Optional .env file to load before resolving credentials (default: .env)",
    )
    roi_group = watch_p.add_mutually_exclusive_group(required=True)
    roi_group.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="WGS84 bounding box for the region of interest",
    )
    roi_group.add_argument(
        "--roi",
        metavar="GEOJSON",
        help="Path to a WGS84 Polygon/MultiPolygon GeoJSON ROI",
    )
    watch_p.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (recommended under cron)",
    )
    watch_p.add_argument(
        "--poll-seconds",
        type=float,
        default=900,
        metavar="S",
        help="Continuous-mode polling interval (default: 900)",
    )
    watch_p.add_argument(
        "--lookback-hours",
        type=float,
        default=72,
        metavar="H",
        help="Acquisition-time reconciliation overlap on every cycle (default: 72)",
    )
    watch_p.add_argument(
        "--bootstrap-lookback-days",
        type=float,
        default=14,
        metavar="D",
        help=(
            "First-run window for the latest near-full-footprint product per tile "
            "(default: 14)"
        ),
    )
    watch_p.add_argument(
        "--catalog-url",
        default="https://stac.dataspace.copernicus.eu/v1",
        help="STAC API root URL",
    )
    watch_p.add_argument(
        "--collection",
        default="sentinel-2-l2a",
        metavar="COLLECTION",
        help="STAC collection ID (default: sentinel-2-l2a)",
    )
    watch_p.add_argument(
        "--max-cloud-cover",
        type=float,
        default=None,
        metavar="PCT",
        help="Optional cloud-cover ceiling; default archives every scene",
    )
    watch_p.add_argument(
        "--asset-profile",
        choices=("native", "ndvi", "metadata-only"),
        default="native",
        help=(
            "native downloads all canonical L2A layers; ndvi downloads B04, B08, "
            "SCL and CLD (default: native)"
        ),
    )
    watch_p.add_argument(
        "--asset-keys",
        nargs="+",
        metavar="KEY",
        help="Override the profile with custom STAC asset keys",
    )
    watch_p.add_argument(
        "--allow-missing-assets",
        action="store_true",
        help="Complete jobs even if requested keys are absent from the STAC item",
    )
    watch_p.add_argument(
        "--state-db",
        default=None,
        metavar="PATH",
        help="SQLite queue path (default: STORAGE_ROOT/_terravault/state/rolling.db)",
    )
    watch_p.add_argument(
        "--storage-root",
        default="satellite_data",
        metavar="DIR",
        help="Root directory for metadata and native assets",
    )
    watch_p.add_argument(
        "--dataset-db",
        default=None,
        metavar="PATH",
        help="DuckDB catalogue path (default: STORAGE_ROOT/dataset.duckdb)",
    )
    watch_p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Rotating log path (default: STORAGE_ROOT/_terravault/logs/watch.log)",
    )
    watch_p.add_argument(
        "--lock-file",
        default=None,
        metavar="PATH",
        help="Worker lock path (default: STATE_DB.lock)",
    )
    watch_p.add_argument(
        "--max-items-per-cycle",
        type=int,
        default=None,
        metavar="N",
        help="Optional STAC discovery cap for each cycle",
    )
    watch_p.add_argument(
        "--max-jobs-per-cycle",
        type=int,
        default=100,
        metavar="N",
        help="Maximum due jobs processed in one cycle (default: 100)",
    )
    watch_p.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        metavar="N",
        help="Terminal failure threshold per job (default: 5)",
    )
    watch_p.add_argument(
        "--retry-base-seconds",
        type=float,
        default=60,
        metavar="S",
        help="Initial retry delay (default: 60)",
    )
    watch_p.add_argument(
        "--retry-max-seconds",
        type=float,
        default=86400,
        metavar="S",
        help="Maximum retry delay (default: 86400)",
    )
    watch_p.add_argument(
        "--quota-retry-seconds",
        type=float,
        default=900,
        metavar="S",
        help="Fallback wait when a quota response has no Retry-After (default: 900)",
    )
    watch_p.add_argument(
        "--retry-failed",
        action="store_true",
        help="Explicitly reset failed/retired jobs before this worker starts",
    )
    watch_p.add_argument(
        "--s3-access-key",
        default=None,
        metavar="KEY",
        help="CDSE S3 access key (prefer TERRAVAULT_CDSE_S3_ACCESS_KEY)",
    )
    watch_p.add_argument(
        "--s3-secret-key",
        default=None,
        metavar="SECRET",
        help="CDSE S3 secret key (prefer TERRAVAULT_CDSE_S3_SECRET_KEY)",
    )
    watch_p.add_argument(
        "--s3-endpoint",
        default=None,
        metavar="URL",
        help="CDSE S3 endpoint (default: https://eodata.dataspace.copernicus.eu)",
    )
    watch_p.add_argument(
        "--s3-region",
        default=None,
        metavar="REGION",
        help="S3 region name (default: default)",
    )
    watch_p.add_argument(
        "--s3-chunk-mib",
        type=int,
        default=8,
        metavar="MIB",
        help="Streaming/resume chunk size in MiB (default: 8)",
    )
    watch_p.set_defaults(func=cmd_watch)

    # -------------------------------------------------------------- historic
    historic_p = sub.add_parser(
        "historic",
        help="Gradually backfill complete native data from a start date",
    )
    historic_p.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="Optional .env file to load before resolving credentials (default: .env)",
    )
    historic_roi = historic_p.add_mutually_exclusive_group(required=True)
    historic_roi.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="WGS84 bounding box for the region of interest",
    )
    historic_roi.add_argument(
        "--roi",
        metavar="GEOJSON",
        help="Path to a WGS84 Polygon/MultiPolygon GeoJSON ROI",
    )
    historic_p.add_argument(
        "--start-date",
        required=True,
        metavar="DATE",
        help="Required historical start date (YYYY-MM-DD or ISO-8601)",
    )
    historic_p.add_argument(
        "--end-date",
        default=None,
        metavar="DATE",
        help="Optional inclusive end date (default: worker start time)",
    )
    historic_p.add_argument(
        "--window-days",
        type=float,
        default=1,
        metavar="D",
        help="Discovery/download window size in days (default: 1)",
    )
    historic_p.add_argument(
        "--catalog-url",
        default="https://stac.dataspace.copernicus.eu/v1",
        help="STAC API root URL",
    )
    historic_p.add_argument(
        "--collection",
        default="sentinel-2-l2a",
        metavar="COLLECTION",
        help="STAC collection ID (default: sentinel-2-l2a)",
    )
    historic_p.add_argument(
        "--max-cloud-cover",
        type=float,
        default=None,
        metavar="PCT",
        help="Optional cloud-cover ceiling; default archives every scene",
    )
    historic_p.add_argument(
        "--asset-profile",
        choices=("native", "ndvi", "metadata-only"),
        default="native",
        help=(
            "native downloads the complete L2A profile; ndvi downloads B04, B08, "
            "SCL and CLD (default: native)"
        ),
    )
    historic_p.add_argument(
        "--asset-keys",
        nargs="+",
        metavar="KEY",
        help="Override the complete profile with custom STAC asset keys",
    )
    historic_p.add_argument(
        "--allow-missing-assets",
        action="store_true",
        help="Complete jobs even if requested keys are absent",
    )
    historic_p.add_argument(
        "--state-db",
        default=None,
        metavar="PATH",
        help="SQLite queue path (default: STORAGE_ROOT/_terravault/state/history.db)",
    )
    historic_p.add_argument(
        "--storage-root",
        default="satellite_data",
        metavar="DIR",
        help="Root directory for metadata and native assets",
    )
    historic_p.add_argument(
        "--dataset-db",
        default=None,
        metavar="PATH",
        help="DuckDB catalogue path (default: STORAGE_ROOT/dataset.duckdb)",
    )
    historic_p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Rotating log path (default: STORAGE_ROOT/_terravault/logs/historic.log)",
    )
    historic_p.add_argument(
        "--lock-file",
        default=None,
        metavar="PATH",
        help="Worker lock path (default: STATE_DB.lock)",
    )
    historic_p.add_argument(
        "--max-jobs-per-batch",
        type=int,
        default=100,
        metavar="N",
        help="Due jobs processed before refreshing queue state (default: 100)",
    )
    historic_p.add_argument(
        "--max-attempts",
        type=int,
        default=8,
        metavar="N",
        help="Retire a quota-limited job after this many attempts (default: 8)",
    )
    historic_p.add_argument(
        "--retry-base-seconds",
        type=float,
        default=60,
        metavar="S",
        help="Initial transient-error retry delay (default: 60)",
    )
    historic_p.add_argument(
        "--retry-max-seconds",
        type=float,
        default=86400,
        metavar="S",
        help="Maximum transient-error retry delay (default: 86400)",
    )
    historic_p.add_argument(
        "--quota-retry-seconds",
        type=float,
        default=900,
        metavar="S",
        help="Fallback quota wait when Retry-After is absent (default: 900)",
    )
    historic_p.add_argument(
        "--retry-retired",
        action="store_true",
        help="Explicitly reset and revisit failed/retired historical jobs",
    )
    historic_p.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable interactive progress bars (useful for cron logs)",
    )
    historic_p.add_argument(
        "--s3-access-key",
        default=None,
        metavar="KEY",
        help="CDSE S3 access key (prefer TERRAVAULT_CDSE_S3_ACCESS_KEY)",
    )
    historic_p.add_argument(
        "--s3-secret-key",
        default=None,
        metavar="SECRET",
        help="CDSE S3 secret key (prefer TERRAVAULT_CDSE_S3_SECRET_KEY)",
    )
    historic_p.add_argument(
        "--s3-endpoint",
        default=None,
        metavar="URL",
        help="CDSE S3 endpoint",
    )
    historic_p.add_argument(
        "--s3-region",
        default=None,
        metavar="REGION",
        help="S3 region name (default: default)",
    )
    historic_p.add_argument(
        "--s3-chunk-mib",
        type=int,
        default=8,
        metavar="MIB",
        help="Streaming/resume chunk size in MiB (default: 8)",
    )
    historic_p.set_defaults(func=cmd_historic)

    # ---------------------------------------------------------------- query
    query_p = sub.add_parser(
        "query",
        help="Query local georeferenced raster pieces from dataset DuckDB",
    )
    query_p.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="Optional .env file (default: .env)",
    )
    query_p.add_argument(
        "--dataset-db",
        default="satellite_data/dataset.duckdb",
        metavar="PATH",
        help="Top-level DuckDB catalogue path",
    )
    query_p.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Optional WGS84 intersection filter",
    )
    query_p.add_argument(
        "--start-date",
        default=None,
        metavar="DATE",
        help="Optional acquisition start date",
    )
    query_p.add_argument(
        "--end-date",
        default=None,
        metavar="DATE",
        help="Optional inclusive acquisition end date",
    )
    query_p.add_argument(
        "--asset-keys",
        nargs="+",
        metavar="KEY",
        help="Optional asset-key filter, e.g. B04_10m SCL_20m",
    )
    query_p.add_argument(
        "--include-incomplete",
        action="store_true",
        help="Include queued/retrying/retired raster records",
    )
    query_p.add_argument(
        "--output",
        choices=("paths", "json"),
        default="paths",
        help="Output file paths or complete JSON metadata (default: paths)",
    )
    query_p.add_argument(
        "--summary",
        action="store_true",
        help="Print dataset counts and stored bytes instead of piece rows",
    )
    query_p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Rotating query log path",
    )
    query_p.set_defaults(func=cmd_query)

    # -------------------------------------------------------------- extract
    extract_p = sub.add_parser(
        "extract",
        help="Stream intersecting local pieces into one multiband Cloud Optimized GeoTIFF",
    )
    extract_p.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="Optional .env file (default: .env)",
    )
    extract_p.add_argument(
        "--dataset-db",
        default="satellite_data/dataset.duckdb",
        metavar="PATH",
        help="Top-level DuckDB catalogue path",
    )
    extract_roi = extract_p.add_mutually_exclusive_group(required=True)
    extract_roi.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="WGS84 output bounding box",
    )
    extract_roi.add_argument(
        "--roi",
        metavar="GEOJSON",
        help="WGS84 Polygon/MultiPolygon GeoJSON; pixels outside it become nodata",
    )
    extract_p.add_argument(
        "--asset-keys",
        nargs="+",
        required=True,
        metavar="KEY",
        help="Features in output band order, e.g. B04_10m B03_10m B02_10m",
    )
    extract_p.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="Output .tif path",
    )
    extract_p.add_argument(
        "--start-date",
        default=None,
        metavar="DATE",
        help="Optional acquisition start date",
    )
    extract_p.add_argument(
        "--end-date",
        default=None,
        metavar="DATE",
        help="Optional inclusive acquisition end date",
    )
    extract_p.add_argument(
        "--selection",
        choices=("latest-per-tile", "all"),
        default="latest-per-tile",
        help="Time selection for overlapping pieces (default: latest-per-tile)",
    )
    extract_p.add_argument(
        "--target-crs",
        default="auto",
        metavar="CRS",
        help="Output CRS, e.g. EPSG:2056; auto uses a common source CRS or EPSG:3857",
    )
    extract_p.add_argument(
        "--resolution",
        type=float,
        default=None,
        metavar="UNITS",
        help="Output pixel size in target-CRS units (default: finest selected feature)",
    )
    extract_p.add_argument(
        "--resampling",
        default="near",
        metavar="METHOD",
        help="GDAL resampling method (default: near, safe for categorical features)",
    )
    extract_p.add_argument(
        "--output-dtype",
        default="auto",
        metavar="TYPE",
        help="GDAL output scalar type (default: safe automatic promotion)",
    )
    extract_p.add_argument(
        "--compression",
        default="DEFLATE",
        metavar="CODEC",
        help="COG compression codec (default: DEFLATE)",
    )
    extract_p.add_argument(
        "--nodata",
        default="0",
        metavar="VALUE",
        help="Common destination nodata value used while mosaicking (default: 0)",
    )
    extract_p.add_argument(
        "--warp-memory-mib",
        type=int,
        default=256,
        metavar="MIB",
        help="Per-GDAL-operation memory budget (default: 256)",
    )
    extract_p.add_argument(
        "--max-output-gib",
        type=float,
        default=16,
        metavar="GIB",
        help="Uncompressed-size safety limit (default: 16)",
    )
    extract_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build virtual mosaics and size/validate the result without writing pixels",
    )
    extract_p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output",
    )
    extract_p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Rotating extraction log path",
    )
    extract_p.set_defaults(func=cmd_extract)

    # ---------------------------------------------------------------- force
    from .force import FORCE_DOCKER_IMAGE, FORCE_DOCKER_PLATFORM

    force_p = sub.add_parser(
        "force",
        help="Import a local stitched raster into a FORCE external-feature datacube",
    )
    force_p.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="Optional .env file (default: .env)",
    )
    force_p.add_argument(
        "--input",
        required=True,
        metavar="TIFF",
        help="Local georeferenced raster with nodata defined on every band",
    )
    force_p.add_argument(
        "--output-root",
        required=True,
        metavar="DIR",
        help="Root for the FORCE datacube, durable job state and logs",
    )
    force_p.add_argument(
        "--basename",
        default=None,
        metavar="NAME",
        help="FORCE feature basename (default: sanitized input filename)",
    )
    force_p.add_argument(
        "--runtime",
        choices=("auto", "native", "docker"),
        default="auto",
        help="FORCE runtime; auto uses native only on Linux, otherwise Docker",
    )
    force_p.add_argument(
        "--docker-image",
        default=FORCE_DOCKER_IMAGE,
        metavar="IMAGE",
        help=f"Pinned FORCE container image (default: {FORCE_DOCKER_IMAGE})",
    )
    force_p.add_argument(
        "--docker-platform",
        default=FORCE_DOCKER_PLATFORM,
        metavar="PLATFORM",
        help=f"Pinned FORCE container platform (default: {FORCE_DOCKER_PLATFORM})",
    )
    force_p.add_argument(
        "--mount-root",
        default=None,
        metavar="DIR",
        help="Docker volume root containing both input and output",
    )
    force_p.add_argument(
        "--target-crs",
        default="EPSG:2056",
        metavar="CRS",
        help="FORCE datacube CRS (default: EPSG:2056)",
    )
    force_p.add_argument(
        "--origin-lon",
        type=float,
        default=5.5,
        metavar="DEG",
        help="WGS84 longitude of grid origin (default: 5.5)",
    )
    force_p.add_argument(
        "--origin-lat",
        type=float,
        default=48.0,
        metavar="DEG",
        help="WGS84 latitude of grid origin (default: 48.0)",
    )
    force_p.add_argument(
        "--tile-size",
        type=int,
        default=30_000,
        metavar="UNITS",
        help="Square FORCE tile size in target-CRS units (default: 30000)",
    )
    force_p.add_argument(
        "--resolution",
        type=float,
        default=10,
        metavar="UNITS",
        help="FORCE feature resolution in target-CRS units (default: 10)",
    )
    force_p.add_argument(
        "--resampling",
        default="near",
        metavar="METHOD",
        help="GDAL resampling used by force-cube (default: near)",
    )
    force_p.add_argument(
        "--output-nodata",
        type=int,
        default=-9999,
        metavar="VALUE",
        help="Nodata written to FORCE feature chips (default: -9999)",
    )
    force_p.add_argument(
        "--output-dtype",
        choices=("Byte", "Int16"),
        default="Int16",
        metavar="TYPE",
        help="FORCE-compatible feature type (default: Int16)",
    )
    force_p.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="Parallel FORCE cube/mosaic jobs (default: 1)",
    )
    force_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input and print the FORCE commands without running them",
    )
    force_p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace chips for an existing basename",
    )
    force_p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Rotating log path (default: OUTPUT_ROOT/_terravault/logs/force.log)",
    )
    force_p.set_defaults(func=cmd_force)

    # ------------------------------------------------------------ collections
    col_p = sub.add_parser("collections", help="List collections available in the STAC catalog")
    col_p.add_argument(
        "--catalog-url",
        default="https://stac.dataspace.copernicus.eu/v1",
        help="STAC API root URL",
    )
    col_p.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="Optional .env file to load before resolving credentials (default: .env)",
    )
    col_p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Optional rotating log path",
    )
    col_p.set_defaults(func=cmd_collections)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    _setup_logging(args.verbose, _default_log_file(args))

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(func(args))


if __name__ == "__main__":
    main()
