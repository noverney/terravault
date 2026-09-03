from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPOSITORY_ROOT / "scripts" / "run_three_source_monthly_pilot.py"


FAKE_TERRAVAULT = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path


arguments = sys.argv[1:]
call_log = Path(os.environ["TERRAVAULT_TEST_CALL_LOG"])
with call_log.open("a", encoding="utf-8") as stream:
    stream.write(" ".join(arguments) + "\n")


def option(name):
    return arguments[arguments.index(name) + 1]


command = arguments[0]
if command in {"copernicus-monthly", "monthly", "swisseo-monthly"}:
    year = int(option("--year"))
    month = int(option("--months"))
    output_root = Path(option("--output-root"))
    month_root = output_root / f"year={year:04d}" / f"month={month:02d}"
    month_root.mkdir(parents=True, exist_ok=True)
    if "--dry-run" not in arguments:
        (month_root / "fixture_reflectance.tif").write_bytes(b"reflectance")
        (month_root / "fixture_indices.tif").write_bytes(b"indices")
    (month_root / "fixture.manifest.json").write_text("{}", encoding="utf-8")
elif command == "compare-monthly":
    year = int(option("--year"))
    month_index = arguments.index("--months") + 1
    months = [int(value) for value in arguments[month_index:month_index + 3]]
    report_root = Path(option("--output-root"))
    report_root.mkdir(parents=True, exist_ok=True)
    for name in (
        "comparison-atlas-9x12.png",
        "comparison-statistics.json",
        "comparison-statistics.csv",
        "report-manifest.json",
        "README.md",
    ):
        (report_root / name).write_bytes(b"report")
    for source in ("sentinel-copernicus", "force", "swisseo"):
        source_root = report_root / "sources" / source
        source_root.mkdir(parents=True, exist_ok=True)
        (source_root / "overview.png").write_bytes(b"overview")
        for month in months:
            month_root = source_root / f"year={year:04d}" / f"month={month:02d}"
            month_root.mkdir(parents=True, exist_ok=True)
            for index_name in ("ndvi", "gndvi", "ndre", "ndwi"):
                (month_root / f"{index_name}.tif").write_bytes(b"cog")
'''


def _pilot_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    network_root = tmp_path / "terravault"
    roi = network_root / "data" / "roi" / "switzerland-boundary.geojson"
    copernicus_db = network_root / "data" / "copernicus-l2a" / "dataset.duckdb"
    force_db = network_root / "data" / "force-native" / "force_images.duckdb"
    for path in (roi, copernicus_db, force_db):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    fake_terravault = tmp_path / "terravault-fixture"
    fake_terravault.write_text(FAKE_TERRAVAULT, encoding="utf-8")
    fake_terravault.chmod(0o755)
    call_log = tmp_path / "calls.log"
    return network_root, roi, fake_terravault, call_log


def _run_launcher(
    network_root: Path,
    roi: Path,
    fake_terravault: Path,
    call_log: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["TERRAVAULT_TEST_CALL_LOG"] = str(call_log)
    return subprocess.run(
        [
            sys.executable,
            str(LAUNCHER),
            "--year",
            "2024",
            "--months",
            "6",
            "7",
            "8",
            "--network-root",
            str(network_root),
            "--roi",
            str(roi),
            "--temporary-root",
            str(network_root.parent / "local-staging"),
            "--terravault-bin",
            str(fake_terravault),
            *extra,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_preflight_prints_commands_without_invoking_processors(tmp_path: Path) -> None:
    network_root, roi, fake_terravault, call_log = _pilot_fixture(tmp_path)

    result = _run_launcher(
        network_root,
        roi,
        fake_terravault,
        call_log,
        "--preflight",
    )

    assert result.returncode == 0, result.stderr
    assert "expected per-equation TIFFs: 36" in result.stdout
    assert "copernicus-monthly" in result.stdout
    assert "swisseo-monthly" in result.stdout
    assert "compare-monthly" in result.stdout
    assert "TerraVault was not invoked" in result.stdout
    assert not call_log.exists()


def test_execute_verifies_native_tiffs_and_second_run_resumes(tmp_path: Path) -> None:
    network_root, roi, fake_terravault, call_log = _pilot_fixture(tmp_path)

    first = _run_launcher(
        network_root,
        roi,
        fake_terravault,
        call_log,
        "--execute",
    )

    assert first.returncode == 0, first.stderr
    calls_after_first_run = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls_after_first_run) == 10
    assert sum(line.startswith("copernicus-monthly ") for line in calls_after_first_run) == 3
    assert sum(line.startswith("monthly ") for line in calls_after_first_run) == 3
    assert sum(line.startswith("swisseo-monthly ") for line in calls_after_first_run) == 3
    assert sum(line.startswith("compare-monthly ") for line in calls_after_first_run) == 1
    assert "VERIFIED 36 native-grid index TIFFs" in first.stdout

    report_root = network_root / "reports" / "switzerland-monthly-2024"
    tiffs = tuple((report_root / "sources").glob("*/year=2024/month=*/*.tif"))
    assert len(tiffs) == 36
    timing_paths = tuple((network_root / "logs" / "three-source-pilot").glob("**/*-timings.csv"))
    assert timing_paths

    second = _run_launcher(
        network_root,
        roi,
        fake_terravault,
        call_log,
        "--execute",
    )

    assert second.returncode == 0, second.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == calls_after_first_run
    assert "SKIP compare: verified report and native index TIFFs are complete" in second.stdout


def test_source_dry_run_supports_stage_selection_and_skips_comparison(tmp_path: Path) -> None:
    network_root, roi, fake_terravault, call_log = _pilot_fixture(tmp_path)

    result = _run_launcher(
        network_root,
        roi,
        fake_terravault,
        call_log,
        "--dry-run",
        "--stages",
        "swisseo",
        "compare",
    )

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 3
    assert all(line.startswith("swisseo-monthly ") for line in calls)
    assert all("--dry-run" in line for line in calls)
    assert "SKIP compare: compare-monthly has no dry-run mode" in result.stdout
