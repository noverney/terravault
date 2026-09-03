#!/usr/bin/env python3
"""Launch the resumable Copernicus/FORCE/swissEO monthly comparison pilot.

The launcher intentionally starts from already prepared local Copernicus L2A
and FORCE catalogues. It never embeds credentials and does not run either raw
Copernicus retrieval or the upstream FORCE Level-2 pipeline.
"""

from __future__ import annotations

import argparse
import csv
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


STAGES = ("copernicus", "force", "swisseo", "compare")
INDEX_NAMES = ("ndvi", "gndvi", "ndre", "ndwi")
REPORT_SOURCE_DIRECTORIES = {
    "copernicus": "sentinel-copernicus",
    "force": "force",
    "swisseo": "swisseo",
}


@dataclass(frozen=True)
class PilotPaths:
    network_root: Path
    roi: Path
    env_file: Path
    copernicus_db: Path
    copernicus_root: Path
    force_db: Path
    force_root: Path
    swisseo_root: Path
    report_root: Path
    temporary_root: Path
    log_root: Path


class LauncherError(RuntimeError):
    """An actionable launcher validation or execution failure."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or launch the three-source monthly pilot and verify the native-grid "
            "NDVI/GNDVI/NDRE/NDWI TIFF exports."
        )
    )
    parser.add_argument("--year", type=int, required=True, metavar="YYYY")
    parser.add_argument(
        "--months",
        nargs="+",
        type=int,
        default=[6, 7, 8],
        metavar="MM",
        help="Calendar months (default: 6 7 8); comparison requires exactly three",
    )
    parser.add_argument(
        "--roi",
        type=Path,
        default=None,
        metavar="GEOJSON",
        help="Exact Swiss cutline (default: NETWORK_ROOT/data/roi/switzerland-boundary.geojson)",
    )
    parser.add_argument(
        "--network-root",
        type=Path,
        default=Path("/Volumes/upscale/working_package_4/terravault"),
        metavar="DIR",
        help="Existing mounted working root; it is never created by this launcher",
    )
    parser.add_argument(
        "--temporary-root",
        type=Path,
        default=Path("/private/tmp/terravault-swisseo"),
        metavar="DIR",
        help="Local swissEO staging directory (never use the SMB share)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        metavar="PATH",
        help="Optional TerraVault environment file; its contents are never printed",
    )
    parser.add_argument(
        "--terravault-bin",
        default="terravault",
        metavar="COMMAND",
        help="TerraVault executable path/name (default: terravault)",
    )
    parser.add_argument(
        "--stages",
        nargs="+",
        choices=STAGES,
        default=list(STAGES),
        metavar="STAGE",
        help="Selected ordered stages (default: all four)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="Validate paths and print commands without invoking TerraVault (default)",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Invoke source processors with --dry-run; comparison remains plan-only",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Run the selected processors and comparison",
    )
    parser.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Do not skip source months or a report that already pass completeness checks",
    )
    parser.set_defaults(resume=True)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Pass --overwrite and run selected stages even when outputs are complete",
    )
    parser.add_argument("--allow-incomplete-month", action="store_true")

    parser.add_argument("--target-crs", default="EPSG:2056", metavar="CRS")
    parser.add_argument(
        "--copernicus-resolution", type=_positive_float, default=20.0, metavar="METRES"
    )
    parser.add_argument(
        "--force-resolution", type=_positive_float, default=10.0, metavar="METRES"
    )
    parser.add_argument(
        "--swisseo-resolution", type=_positive_float, default=20.0, metavar="METRES"
    )
    parser.add_argument(
        "--analysis-resolution", type=_positive_float, default=20.0, metavar="METRES"
    )
    parser.add_argument(
        "--max-cloud-cover", type=float, default=100.0, metavar="PCT"
    )
    parser.add_argument(
        "--cloud-probability-threshold", type=float, default=30.0, metavar="PCT"
    )
    parser.add_argument("--warp-memory-mib", type=int, default=512, metavar="MIB")
    parser.add_argument(
        "--max-output-gib", type=_positive_float, default=64.0, metavar="GIB"
    )
    parser.add_argument(
        "--max-temporary-gib", type=_positive_float, default=128.0, metavar="GIB"
    )
    parser.add_argument(
        "--publication-wait-seconds",
        type=_nonnegative_float,
        default=21_600.0,
        metavar="SECONDS",
    )
    return parser


def _mode(args: argparse.Namespace) -> str:
    if args.execute:
        return "execute"
    if args.dry_run:
        return "dry-run"
    return "preflight"


def _resolve_existing_directory(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise LauncherError(f"{label} must be an absolute path: {expanded}")
    try:
        resolved = expanded.resolve(strict=True)
    except FileNotFoundError as exc:
        raise LauncherError(
            f"{label} is not available: {expanded}. Reconnect the network share first."
        ) from exc
    if not resolved.is_dir():
        raise LauncherError(f"{label} is not a directory: {resolved}")
    if resolved == Path("/"):
        raise LauncherError(f"Refusing to use the filesystem root as {label}")
    return resolved


def _paths(args: argparse.Namespace) -> PilotPaths:
    network_root = _resolve_existing_directory(args.network_root, "network root")
    roi_input = args.roi or network_root / "data" / "roi" / "switzerland-boundary.geojson"
    roi = roi_input.expanduser().resolve()
    env_file = args.env_file.expanduser().resolve()
    return PilotPaths(
        network_root=network_root,
        roi=roi,
        env_file=env_file,
        copernicus_db=network_root / "data" / "copernicus-l2a" / "dataset.duckdb",
        copernicus_root=network_root / "data" / "copernicus-monthly",
        force_db=network_root / "data" / "force-native" / "force_images.duckdb",
        force_root=network_root / "data" / "force-monthly",
        swisseo_root=network_root / "data" / "swisseo-monthly",
        report_root=network_root / "reports" / f"switzerland-monthly-{args.year:04d}",
        temporary_root=args.temporary_root.expanduser().resolve(),
        log_root=network_root / "logs" / "three-source-pilot" / f"year={args.year:04d}",
    )


def _validate_command(command: str) -> str:
    candidate = Path(command).expanduser()
    if candidate.parent != Path(".") or os.sep in command:
        resolved = candidate.resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise LauncherError(f"TerraVault executable is missing or not executable: {resolved}")
        return str(resolved)
    resolved_command = shutil.which(command)
    if resolved_command is None:
        raise LauncherError(
            f"TerraVault executable is not on PATH: {command}. Activate the project environment "
            "or pass --terravault-bin /absolute/path/to/terravault."
        )
    return resolved_command


def _validate(args: argparse.Namespace, paths: PilotPaths) -> str:
    if not 2015 <= args.year <= 2100:
        raise LauncherError("year must be between 2015 and 2100")
    if any(month < 1 or month > 12 for month in args.months):
        raise LauncherError("months must be between 1 and 12")
    if len(set(args.months)) != len(args.months):
        raise LauncherError("months must not contain duplicates")
    if "compare" in args.stages and len(args.months) != 3:
        raise LauncherError("the compare stage requires exactly three months")
    if not paths.roi.is_file():
        raise LauncherError(f"Swiss ROI GeoJSON does not exist: {paths.roi}")
    if "copernicus" in args.stages and not paths.copernicus_db.is_file():
        raise LauncherError(
            "Copernicus DatasetCatalog is missing: "
            f"{paths.copernicus_db}. Retrieve the vegetation asset profile first."
        )
    if "force" in args.stages and not paths.force_db.is_file():
        raise LauncherError(
            "FORCE image catalogue is missing: "
            f"{paths.force_db}. Complete the upstream native FORCE pipeline first."
        )
    if not 0 <= args.max_cloud_cover <= 100:
        raise LauncherError("max-cloud-cover must be between 0 and 100")
    if not 0 <= args.cloud_probability_threshold <= 100:
        raise LauncherError("cloud-probability-threshold must be between 0 and 100")
    if args.warp_memory_mib < 16:
        raise LauncherError("warp-memory-mib must be at least 16")
    return _validate_command(args.terravault_bin)


def _common_month_flags(args: argparse.Namespace, paths: PilotPaths, month: int) -> list[str]:
    flags = [
        "--env-file",
        str(paths.env_file),
        "--year",
        str(args.year),
        "--months",
        str(month),
        "--roi",
        str(paths.roi),
        "--target-crs",
        args.target_crs,
        "--warp-memory-mib",
        str(args.warp_memory_mib),
        "--max-output-gib",
        str(args.max_output_gib),
    ]
    if args.allow_incomplete_month:
        flags.append("--allow-incomplete-month")
    if args.overwrite:
        flags.append("--overwrite")
    if _mode(args) == "dry-run":
        flags.append("--dry-run")
    return flags


def _stage_log(paths: PilotPaths, run_id: str, stage: str, month: int | None) -> Path:
    suffix = "all-months" if month is None else f"month={month:02d}"
    return paths.log_root / f"{run_id}-{stage}-{suffix}.log"


def _source_command(
    args: argparse.Namespace,
    paths: PilotPaths,
    executable: str,
    stage: str,
    month: int,
    run_id: str,
) -> list[str]:
    common = _common_month_flags(args, paths, month)
    log_file = _stage_log(paths, run_id, stage, month)
    if stage == "copernicus":
        return [
            executable,
            "copernicus-monthly",
            *common,
            "--dataset-db",
            str(paths.copernicus_db),
            "--output-root",
            str(paths.copernicus_root),
            "--resolution",
            str(args.copernicus_resolution),
            "--max-cloud-cover",
            str(args.max_cloud_cover),
            "--cloud-probability-threshold",
            str(args.cloud_probability_threshold),
            "--log-file",
            str(log_file),
        ]
    if stage == "force":
        return [
            executable,
            "monthly",
            *common,
            "--force-db",
            str(paths.force_db),
            "--output-root",
            str(paths.force_root),
            "--resolution",
            str(args.force_resolution),
            "--log-file",
            str(log_file),
        ]
    if stage == "swisseo":
        return [
            executable,
            "swisseo-monthly",
            *common,
            "--output-root",
            str(paths.swisseo_root),
            "--temporary-root",
            str(paths.temporary_root),
            "--resolution",
            str(args.swisseo_resolution),
            "--max-temporary-gib",
            str(args.max_temporary_gib),
            "--publication-wait-seconds",
            str(args.publication_wait_seconds),
            "--log-file",
            str(log_file),
        ]
    raise LauncherError(f"Unknown source stage: {stage}")


def _comparison_command(
    args: argparse.Namespace,
    paths: PilotPaths,
    executable: str,
    run_id: str,
) -> list[str]:
    command = [
        executable,
        "compare-monthly",
        "--env-file",
        str(paths.env_file),
        "--year",
        str(args.year),
        "--months",
        *(str(month) for month in args.months),
        "--roi",
        str(paths.roi),
        "--copernicus-root",
        str(paths.copernicus_root),
        "--force-root",
        str(paths.force_root),
        "--swisseo-root",
        str(paths.swisseo_root),
        "--output-root",
        str(paths.report_root),
        "--target-crs",
        args.target_crs,
        "--analysis-resolution",
        str(args.analysis_resolution),
        "--log-file",
        str(_stage_log(paths, run_id, "compare", None)),
    ]
    if args.overwrite:
        command.append("--overwrite")
    return command


def _nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _month_complete(root: Path, year: int, month: int) -> bool:
    directory = root / f"year={year:04d}" / f"month={month:02d}"
    return (
        any(_nonempty(path) for path in directory.glob("*_reflectance.tif"))
        and any(_nonempty(path) for path in directory.glob("*_indices.tif"))
        and any(_nonempty(path) for path in directory.glob("*.manifest.json"))
    )


def _source_root(paths: PilotPaths, stage: str) -> Path:
    return {
        "copernicus": paths.copernicus_root,
        "force": paths.force_root,
        "swisseo": paths.swisseo_root,
    }[stage]


def _missing_report_outputs(paths: PilotPaths, year: int, months: Sequence[int]) -> list[Path]:
    required = [
        paths.report_root / "comparison-atlas-9x12.png",
        paths.report_root / "comparison-statistics.json",
        paths.report_root / "comparison-statistics.csv",
        paths.report_root / "report-manifest.json",
        paths.report_root / "README.md",
    ]
    for source_directory in REPORT_SOURCE_DIRECTORIES.values():
        required.append(paths.report_root / "sources" / source_directory / "overview.png")
        for month in months:
            month_root = (
                paths.report_root
                / "sources"
                / source_directory
                / f"year={year:04d}"
                / f"month={month:02d}"
            )
            required.extend(month_root / f"{index_name}.tif" for index_name in INDEX_NAMES)
    return [path for path in required if not _nonempty(path)]


def _report_complete(paths: PilotPaths, year: int, months: Sequence[int]) -> bool:
    return not _missing_report_outputs(paths, year, months)


def _log(message: str, launcher_log: Path | None = None) -> None:
    line = f"[{_utc_text()}] {message}"
    print(line, flush=True)
    if launcher_log is not None:
        with launcher_log.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def _append_timing(
    timing_path: Path,
    *,
    stage: str,
    month: int | None,
    mode: str,
    status: str,
    started: datetime,
    ended: datetime,
    elapsed: float,
) -> None:
    first_write = not timing_path.exists()
    with timing_path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        if first_write:
            writer.writerow(
                ("stage", "year_month", "mode", "status", "started_utc", "ended_utc", "elapsed_seconds")
            )
        writer.writerow(
            (
                stage,
                "all" if month is None else f"{started.year:04d}-{month:02d}",
                mode,
                status,
                _utc_text(started),
                _utc_text(ended),
                f"{elapsed:.3f}",
            )
        )


def _run_command(
    command: Sequence[str],
    *,
    stage: str,
    month: int | None,
    mode: str,
    launcher_log: Path,
    timing_path: Path,
) -> None:
    started = _utc_now()
    monotonic_started = time.monotonic()
    _log(f"RUN {stage} month={month or 'all'}: {shlex.join(command)}", launcher_log)
    status = "failed"
    try:
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                with launcher_log.open("a", encoding="utf-8") as stream:
                    stream.write(line)
            return_code = process.wait()
        if return_code != 0:
            raise LauncherError(f"{stage} month={month or 'all'} exited with status {return_code}")
        status = "complete"
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    finally:
        ended = _utc_now()
        elapsed = time.monotonic() - monotonic_started
        _append_timing(
            timing_path,
            stage=stage,
            month=month,
            mode=mode,
            status=status,
            started=started,
            ended=ended,
            elapsed=elapsed,
        )
        _log(
            f"END {stage} month={month or 'all'} status={status} elapsed_seconds={elapsed:.1f}",
            launcher_log,
        )


def _print_preflight(
    args: argparse.Namespace,
    paths: PilotPaths,
    executable: str,
    run_id: str,
) -> None:
    free_gib = shutil.disk_usage(paths.network_root).free / (1024**3)
    print("Three-source monthly pilot preflight")
    print(f"  mode: {_mode(args)}")
    print(f"  network root: {paths.network_root}")
    print(f"  free space: {free_gib:.1f} GiB")
    print(f"  ROI: {paths.roi}")
    print(f"  year/months: {args.year} {' '.join(str(value) for value in args.months)}")
    print(f"  stages: {' '.join(args.stages)}")
    print(f"  report: {paths.report_root}")
    print(
        "  expected per-equation TIFFs: "
        f"{3 * len(args.months) * len(INDEX_NAMES)} "
        "(native stored grid; no comparison-grid downsampling)"
    )
    print("\nCommands:")
    for stage in args.stages:
        if stage == "compare":
            command = _comparison_command(args, paths, executable, run_id)
            print(f"  [PLAN] {shlex.join(command)}")
            continue
        for month in args.months:
            command = _source_command(args, paths, executable, stage, month, run_id)
            print(f"  [PLAN] {shlex.join(command)}")
    if _mode(args) == "preflight":
        print("\nPreflight complete: TerraVault was not invoked and no output was created.")


def run(args: argparse.Namespace) -> int:
    paths = _paths(args)
    executable = _validate(args, paths)
    mode = _mode(args)
    run_id = _utc_now().strftime("%Y%m%dT%H%M%SZ")

    _print_preflight(args, paths, executable, run_id)
    if mode == "preflight":
        return 0

    paths.log_root.mkdir(parents=True, exist_ok=True)
    paths.temporary_root.mkdir(parents=True, exist_ok=True)
    launcher_log = paths.log_root / f"{run_id}-launcher.log"
    timing_path = paths.log_root / f"{run_id}-timings.csv"
    _log(f"Launcher log: {launcher_log}", launcher_log)
    _log(f"Timing table: {timing_path}", launcher_log)

    for stage in args.stages:
        if not paths.network_root.is_dir():
            raise LauncherError(f"Network root disconnected before {stage}: {paths.network_root}")
        if stage == "compare":
            if mode == "dry-run":
                _log(
                    "SKIP compare: compare-monthly has no dry-run mode; command was printed only",
                    launcher_log,
                )
                continue
            if args.resume and not args.overwrite and _report_complete(
                paths, args.year, args.months
            ):
                _log("SKIP compare: verified report and native index TIFFs are complete", launcher_log)
                continue
            command = _comparison_command(args, paths, executable, run_id)
            _run_command(
                command,
                stage=stage,
                month=None,
                mode=mode,
                launcher_log=launcher_log,
                timing_path=timing_path,
            )
            missing = _missing_report_outputs(paths, args.year, args.months)
            if missing:
                preview = ", ".join(str(path) for path in missing[:5])
                remainder = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
                raise LauncherError(
                    "Comparison returned successfully but required report files are missing: "
                    f"{preview}{remainder}"
                )
            _log(
                f"VERIFIED {3 * len(args.months) * len(INDEX_NAMES)} native-grid index TIFFs",
                launcher_log,
            )
            continue

        root = _source_root(paths, stage)
        for month in args.months:
            if args.resume and not args.overwrite and _month_complete(root, args.year, month):
                _log(f"SKIP {stage} month={month:02d}: complete monthly product exists", launcher_log)
                continue
            command = _source_command(args, paths, executable, stage, month, run_id)
            _run_command(
                command,
                stage=stage,
                month=month,
                mode=mode,
                launcher_log=launcher_log,
                timing_path=timing_path,
            )
            if mode == "execute" and not _month_complete(root, args.year, month):
                raise LauncherError(
                    f"{stage} month={month:02d} returned successfully but its reflectance, "
                    "four-band index COG, or manifest is missing"
                )

    _log(f"Pilot {mode} finished", launcher_log)
    if mode == "execute" and "compare" in args.stages:
        print(f"Report: {paths.report_root}")
        print("Per-equation TIFFs: REPORT/sources/SOURCE/year=YYYY/month=MM/{ndvi,gndvi,ndre,ndwi}.tif")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except LauncherError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; completed monthly products remain resumable.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
