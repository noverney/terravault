"""Tests for the restartable FORCE external-feature bridge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from terravault.cli import _default_log_file, build_parser
from terravault.force import (
    FORCE_DOCKER_IMAGE,
    FORCE_DOCKER_PLATFORM,
    FORCE_VERSION,
    ForceConfig,
    ForcePostprocessor,
)


def _fake_force_runtime(
    processor: ForcePostprocessor,
    *,
    make_chip: bool = True,
):
    def run(
        arguments: list[str] | tuple[str, ...],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del check
        command = Path(arguments[0]).name
        stdout = ""
        stderr = ""
        if command == "gdalinfo":
            stdout = json.dumps(
                {
                    "size": [20, 10],
                    "coordinateSystem": {"wkt": 'PROJCRS["test"]'},
                    "bands": [
                        {"band": 1, "type": "Int16", "noDataValue": -9999},
                        {"band": 2, "type": "Int16", "noDataValue": -9999},
                    ],
                }
            )
        elif command == "gdalsrsinfo":
            stdout = 'PROJCS["Swiss",\nUNIT["metre",1]]\n'
        elif command == "force-cube-init":
            cube_root = Path(arguments[arguments.index("-d") + 1])
            cube_root.mkdir(parents=True, exist_ok=True)
            (cube_root / "datacube-definition.prj").write_text(
                "\n".join(
                    (
                        'PROJECTION = PROJCS["Swiss",UNIT["metre",1]]',
                        "ORIGIN_GEO_X = 5.500000",
                        "ORIGIN_GEO_Y = 48.000000",
                        "ORIGIN_MAP_X = 2455319.562593",
                        "ORIGIN_MAP_Y = 1318416.952794",
                        "TILE_SIZE_X = 30000.000000",
                        "TILE_SIZE_Y = 30000.000000",
                    )
                ),
                encoding="utf-8",
            )
        elif command == "force-info":
            stdout = f"FORCE v{FORCE_VERSION}\n"
        elif command == "force-cube":
            if make_chip:
                cube_root = Path(arguments[arguments.index("-o") + 1])
                basename = arguments[arguments.index("-b") + 1]
                chip = cube_root / "X0000_Y0000" / f"{basename}.tif"
                chip.parent.mkdir(parents=True, exist_ok=True)
                chip.write_bytes(b"force-chip")
            else:
                stderr = "parallel worker failed"
        elif command == "force-mosaic":
            mosaic_root = Path(arguments[arguments.index("-m") + 1])
            mosaic_root.mkdir(parents=True, exist_ok=True)
            (mosaic_root / f"{processor.config.basename}.vrt").write_text(
                "<VRTDataset/>",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(arguments, 0, stdout, stderr)

    return run


def test_force_config_rejects_incompatible_cube_values(tmp_path):
    input_path = tmp_path / "input.tif"
    with pytest.raises(ValueError, match="Byte or Int16"):
        ForceConfig(
            input_path=input_path,
            output_root=tmp_path / "force",
            output_dtype="UInt16",
        )
    with pytest.raises(ValueError, match="must fit Byte"):
        ForceConfig(
            input_path=input_path,
            output_root=tmp_path / "force",
            output_dtype="Byte",
            output_nodata=-9999,
        )
    with pytest.raises(ValueError, match="exact multiple"):
        ForceConfig(
            input_path=input_path,
            output_root=tmp_path / "force",
            tile_size=30_000,
            resolution=7,
        )
    with pytest.raises(ValueError, match="basename"):
        ForceConfig(
            input_path=input_path,
            output_root=tmp_path / "force",
            basename="../escape",
        )


def test_force_native_import_writes_state_and_skips_unchanged(
    tmp_path,
    monkeypatch,
):
    input_path = tmp_path / "input.tif"
    input_path.write_bytes(b"local-raster")
    config = ForceConfig(
        input_path=input_path,
        output_root=tmp_path / "force",
        basename="switzerland_test",
        runtime="native",
    )
    processor = ForcePostprocessor(config)
    monkeypatch.setattr(
        "terravault.force.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(processor, "_run", _fake_force_runtime(processor))

    result = processor.run()

    assert result.status == "complete"
    assert not result.skipped
    assert len(result.chip_paths) == 1
    assert result.mosaic_path is not None
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["attempts"] == 1
    assert manifest["input_sha256"]
    assert manifest["input_mtime_ns"] == input_path.stat().st_mtime_ns
    assert manifest["force_info"] == f"FORCE v{FORCE_VERSION}"

    repeated = ForcePostprocessor(config)
    monkeypatch.setattr(repeated, "_run", _fake_force_runtime(repeated))
    repeated_result = repeated.run()
    assert repeated_result.skipped
    assert repeated_result.chip_paths == result.chip_paths


def test_force_records_child_worker_failure(tmp_path, monkeypatch):
    input_path = tmp_path / "input.tif"
    input_path.write_bytes(b"local-raster")
    config = ForceConfig(
        input_path=input_path,
        output_root=tmp_path / "force",
        runtime="native",
    )
    processor = ForcePostprocessor(config)
    monkeypatch.setattr(
        "terravault.force.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(
        processor,
        "_run",
        _fake_force_runtime(processor, make_chip=False),
    )

    with pytest.raises(RuntimeError, match="parallel worker failed"):
        processor.run()

    manifest = json.loads(processor.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["attempts"] == 1
    assert "parallel worker failed" in manifest["error"]


def test_force_docker_command_uses_scoped_mount_user_and_writable_home(
    tmp_path,
    monkeypatch,
):
    input_path = tmp_path / "dataset" / "input.tif"
    input_path.parent.mkdir()
    input_path.write_bytes(b"raster")
    config = ForceConfig(
        input_path=input_path,
        output_root=tmp_path / "dataset" / "force",
        runtime="docker",
        dry_run=True,
    )
    processor = ForcePostprocessor(config)
    monkeypatch.setattr("terravault.force.shutil.which", lambda _command: "/usr/bin/docker")

    command = processor._force_command("force-info", [])

    assert command[:3] == ["docker", "run", "--rm"]
    assert f"{input_path.parent.resolve()}:/data" in command
    assert "HOME=/tmp" in command
    assert "PARALLEL_HOME=/tmp/.parallel" in command
    assert "SHELL=/bin/bash" in command
    assert command[3:5] == ["--platform", FORCE_DOCKER_PLATFORM]
    assert "--user" in command
    assert FORCE_DOCKER_IMAGE in command


def test_force_auto_never_selects_unsupported_native_macos(tmp_path, monkeypatch):
    input_path = tmp_path / "input.tif"
    input_path.write_bytes(b"raster")
    processor = ForcePostprocessor(
        ForceConfig(
            input_path=input_path,
            output_root=tmp_path / "force",
            runtime="auto",
            dry_run=True,
        )
    )
    monkeypatch.setattr("terravault.force.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "terravault.force.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )

    assert processor._select_runtime() == "docker"


def test_force_cli_parser_and_default_log(tmp_path):
    args = build_parser().parse_args(
        [
            "force",
            "--input",
            "input.tif",
            "--output-root",
            str(tmp_path / "force"),
            "--runtime",
            "docker",
            "--dry-run",
        ]
    )

    assert args.runtime == "docker"
    assert args.docker_platform == FORCE_DOCKER_PLATFORM
    assert args.resolution == 10
    assert args.output_dtype == "Int16"
    assert _default_log_file(args) == tmp_path / "force/_terravault/logs/force.log"
