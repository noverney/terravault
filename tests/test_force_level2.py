"""Tests for native FORCE L2PS orchestration without invoking FORCE."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from terravault.cli import _default_log_file, build_parser
from terravault.force_level2 import (
    ForceLevel2Cancelled,
    ForceLevel2Config,
    ForceLevel2Processor,
    _RunLock,
    default_force_threads,
    inspect_force_level2_status,
)


PRODUCT_NAME = "S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T142404.SAFE"
BANDS = ("01", "02", "03", "04", "05", "06", "07", "08", "8A", "09", "10", "11", "12")


def _safe_members(root: str = PRODUCT_NAME) -> dict[str, bytes]:
    granule = f"{root}/GRANULE/L1C_T32TMT_A000001_20260717T103029"
    members = {
        f"{root}/manifest.safe": b"manifest",
        f"{root}/MTD_MSIL1C.xml": b"product metadata",
        f"{granule}/MTD_TL.xml": b"granule metadata",
    }
    members.update(
        {f"{granule}/IMG_DATA/T32TMT_20260717T103029_B{band}.jp2": band.encode() for band in BANDS}
    )
    return members


def _make_safe_directory(
    tmp_path: Path,
    *,
    include_b10: bool = True,
    product_name: str = PRODUCT_NAME,
) -> Path:
    safe = tmp_path / product_name
    for member, data in _safe_members(product_name).items():
        relative = Path(member).relative_to(product_name)
        if not include_b10 and relative.name.endswith("_B10.jp2"):
            continue
        destination = safe / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return safe


def _make_safe_zip(tmp_path: Path, *, root: str = PRODUCT_NAME) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive_path = tmp_path / f"{PRODUCT_NAME}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for member, data in _safe_members(root).items():
            archive.writestr(member, data)
    return archive_path


_FAKE_WKT = 'PROJCS["Swiss",\nUNIT["metre",1]]\n'


def _fake_gdalinfo(path: str) -> dict[str, object]:
    overview = "_OVV." in path
    metadata: dict[str, object] = {
        "size": [200, 200] if overview else [3000, 3000],
        "geoTransform": (
            [2600000, 150, 0, 1200000, 0, -150]
            if overview
            else [2600000, 10, 0, 1200000, 0, -10]
        ),
        "coordinateSystem": {"wkt": 'PROJCS["Swiss"]'},
    }
    if "_BOA." in path:
        domains = (
            "BLUE",
            "GREEN",
            "RED",
            "REDEDGE1",
            "REDEDGE2",
            "REDEDGE3",
            "BROADNIR",
            "NIR",
            "SWIR1",
            "SWIR2",
        )
        metadata["bands"] = [
            {
                "type": "Int16",
                "noDataValue": -9999,
                "description": domain,
                "metadata": {"FORCE": {"Domain": domain}},
            }
            for domain in domains
        ]
    elif "_QAI." in path:
        metadata["bands"] = [
            {
                "type": "Int16",
                "noDataValue": 1,
                "description": "Quality assurance information",
                "metadata": {"FORCE": {"Domain": "QAI"}},
            }
        ]
    else:
        metadata["bands"] = [
            {
                "type": "UInt16",
                "colorInterpretation": color,
            }
            for color in ("Red", "Green", "Blue")
        ]
        metadata["driverShortName"] = "JPEG"
    return metadata


def _fake_inspection_batch(arguments, metadata_hook=None) -> str:
    """Emulate the batched ``sh -c`` gdalinfo/gdalsrsinfo inspection output."""

    assert arguments[1] == "-c"
    marker = arguments[3]
    lines: list[str] = []
    for request in arguments[4:]:
        kind, _, path = request.partition(":")
        metadata = _fake_gdalinfo(path)
        if metadata_hook is not None:
            metadata = metadata_hook(path, metadata)
        lines.extend(("", f"{marker} INFO {path}", json.dumps(metadata)))
        if kind == "s":
            lines.extend(("", f"{marker} SRS {path}", _FAKE_WKT))
    return "\n".join(lines) + "\n"


def _fake_force_runtime(
    processor: ForceLevel2Processor,
    *,
    queue_status: str = "DONE",
    returncode: int = 0,
    metadata_hook=None,
):
    def run(
        arguments: list[str] | tuple[str, ...],
        *,
        check: bool = True,
        log_path: Path | None = None,
        container_cid_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, container_cid_path
        executable = Path(arguments[0]).name
        stdout = ""
        stderr = ""
        if executable == "force-info":
            stdout = "Hello, running FORCE v. 3.10.04\n"
        elif executable == "gdalsrsinfo":
            stdout = _FAKE_WKT
        elif executable == "sh":
            stdout = _fake_inspection_batch(arguments, metadata_hook)
        elif executable == "force-level2":
            processor.queue_path.write_text(
                f"{processor.config.input_path} {queue_status}\n",
                encoding="utf-8",
            )
            if queue_status == "DONE" and returncode == 0:
                chip_root = processor.attempt_level2_root / "X0000_Y0000"
                chip_root.mkdir(parents=True, exist_ok=True)
                (processor.attempt_level2_root / "datacube-definition.prj").write_text(
                    'PROJECTION = PROJCS["Swiss", UNIT["metre",1]]\n'
                    f"ORIGIN_GEO_X = {processor.config.origin_lon:.6f}\n"
                    f"ORIGIN_GEO_Y = {processor.config.origin_lat:.6f}\n"
                    "ORIGIN_MAP_X = 2600000.000000\n"
                    "ORIGIN_MAP_Y = 1200000.000000\n"
                    f"TILE_SIZE_X = {processor.config.tile_size:.6f}\n"
                    f"TILE_SIZE_Y = {processor.config.tile_size:.6f}\n",
                    encoding="utf-8",
                )
                (chip_root / f"{processor.product_tag}_BOA.tif").write_bytes(b"boa")
                (chip_root / f"{processor.product_tag}_QAI.tif").write_bytes(b"qai")
                if processor.config.output_overview:
                    (chip_root / f"{processor.product_tag}_OVV.jpg").write_bytes(b"overview")
            stderr = "simulated child failure" if returncode else ""
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(stderr or "simulated FORCE output\n", encoding="utf-8")
            return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)
        elif executable == "force-mosaic":
            mosaic = processor.attempt_level2_root / "mosaic"
            mosaic.mkdir(parents=True, exist_ok=True)
            for suffix in ("BOA", "QAI"):
                source = f"../X0000_Y0000/{processor.product_tag}_{suffix}.tif"
                (mosaic / f"{processor.product_tag}_{suffix}.vrt").write_text(
                    "<VRTDataset><VRTRasterBand><SimpleSource>"
                    f'<SourceFilename relativeToVRT="1">{source}</SourceFilename>'
                    "</SimpleSource></VRTRasterBand></VRTDataset>",
                    encoding="utf-8",
                )
        return subprocess.CompletedProcess(arguments, 0, stdout, stderr)

    return run


def test_force_level2_accepts_complete_safe_directory_and_zip(tmp_path):
    safe = _make_safe_directory(tmp_path / "directory")
    directory_processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=safe,
            output_root=tmp_path / "directory-output",
            runtime="native",
            dry_run=True,
        )
    )
    directory_processor._validate_input()

    archive = _make_safe_zip(tmp_path / "archive")
    archive_processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=archive,
            output_root=tmp_path / "archive-output",
            runtime="native",
            dry_run=True,
        )
    )
    archive_processor._validate_input()


def test_force_level2_terminates_the_native_process_group_when_interrupted(monkeypatch):
    class FakeProcess:
        pid = 9876
        returncode = -signal.SIGTERM

        def __init__(self) -> None:
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            assert timeout == 10
            return "", ""

        @staticmethod
        def poll():
            return None

    process = FakeProcess()
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, signum: killed.append((pid, signum)))

    with pytest.raises(KeyboardInterrupt):
        ForceLevel2Processor._run(["force-info"])

    assert killed == [(process.pid, signal.SIGTERM)]
    assert process.calls == 2


def test_force_level2_streams_child_output_to_durable_log(tmp_path):
    log_path = tmp_path / "force.batch.log"

    completed = ForceLevel2Processor._run(
        [
            sys.executable,
            "-c",
            "import sys; print('force stdout'); print('force stderr', file=sys.stderr)",
        ],
        log_path=log_path,
    )

    contents = log_path.read_text(encoding="utf-8")
    assert completed.returncode == 0
    assert "# started_at=" in contents
    assert "# command=" in contents
    assert "force stdout" in contents
    assert "force stderr" in contents
    assert "# exit_code=0" in contents


def test_force_level2_rejects_incomplete_safe_and_l2a(tmp_path):
    incomplete = _make_safe_directory(tmp_path / "incomplete", include_b10=False)
    processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=incomplete,
            output_root=tmp_path / "output",
            runtime="native",
            dry_run=True,
        )
    )
    with pytest.raises(ValueError, match="all 13 L1C bands"):
        processor.run()
    preflight = json.loads(processor.manifest_path.read_text(encoding="utf-8"))
    assert preflight["status"] == "failed"
    assert preflight["phase"] == "preflight"
    assert preflight["attempts"] == 0

    l2a = tmp_path / PRODUCT_NAME.replace("MSIL1C", "MSIL2A")
    l2a.mkdir()
    with pytest.raises(ValueError, match="complete Sentinel-2 L1C"):
        ForceLevel2Processor(
            ForceLevel2Config(
                input_path=l2a,
                output_root=tmp_path / "l2a-output",
                dry_run=True,
            )
        )


def test_force_level2_done_writes_products_parameters_and_skips(tmp_path, monkeypatch):
    safe = _make_safe_directory(tmp_path / "input")
    dem = tmp_path / "dem.tif"
    dem.write_bytes(b"dem")
    config = ForceLevel2Config(
        input_path=safe,
        output_root=tmp_path / "output",
        runtime="native",
        dem_path=dem,
        cloud_buffer=240,
        cloud_threshold=0.31,
        shadow_threshold=0.04,
        resolution_merge="NONE",
    )
    processor = ForceLevel2Processor(config)
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(processor, "_run", _fake_force_runtime(processor))

    result = processor.run()

    assert result.status == "complete"
    assert not result.skipped
    assert len(result.boa_paths) == 1
    assert len(result.qai_paths) == 1
    assert len(result.overview_paths) == 1
    assert result.boa_mosaic_path is not None
    assert result.qai_mosaic_path is not None
    assert processor._queue_status() == "DONE"
    parameters = processor.parameter_path.read_text(encoding="utf-8")
    assert parameters.startswith("++PARAM_LEVEL2_START++\n")
    assert parameters.endswith("++PARAM_LEVEL2_END++\n")
    assert f"FILE_QUEUE = {processor.queue_path}" in parameters
    assert f"DIR_LEVEL2 = {processor.attempt_level2_root}" in parameters
    assert f"FILE_DEM = {dem.resolve()}" in parameters
    assert "DO_ATMO = TRUE" in parameters
    assert "DO_TOPO = TRUE" in parameters
    assert "ERASE_CLOUDS = FALSE" in parameters
    assert "CLOUD_BUFFER = 240" in parameters
    assert "CLOUD_THRESHOLD = 0.31" in parameters
    assert "SHADOW_THRESHOLD = 0.04" in parameters
    assert "RES_MERGE = NONE" in parameters
    assert "OUTPUT_FORMAT = COG" in parameters
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["queue_status"] == "DONE"
    assert manifest["attempts"] == 1
    assert manifest["started_at"] is not None
    assert manifest["progress_path"] == str(processor.progress_path)
    progress = json.loads(processor.progress_path.read_text(encoding="utf-8"))
    assert progress["status"] == "complete"
    assert progress["phase"] == "complete"
    assert progress["within_scene_percent"] is None
    statuses = inspect_force_level2_status(config.output_root, job_stem=processor.job_stem)
    assert len(statuses) == 1
    assert statuses[0].phase == "complete"
    assert statuses[0].boa_tiles == 1
    assert statuses[0].qai_tiles == 1
    assert statuses[0].overview_tiles == 1
    assert statuses[0].progress_path == processor.progress_path.resolve()
    assert result.level2_root.parent.name == "products"
    assert not processor.attempt_root.exists()
    cube = json.loads(processor.cube_config_path.read_text(encoding="utf-8"))
    assert cube["target_crs"] == "EPSG:2056"

    assert manifest["output_signature"] == processor._output_signature(result.level2_root)
    assert {entry["path"] for entry in manifest["output_signature"]} >= {
        f"X0000_Y0000/{processor.product_tag}_BOA.tif",
        f"mosaic/{processor.product_tag}_QAI.vrt",
    }

    repeated = ForceLevel2Processor(config)
    repeated_calls: list[str] = []

    def unexpected_run(arguments, **_kwargs):
        repeated_calls.append(Path(arguments[0]).name)
        raise AssertionError("unchanged complete publication should skip without commands")

    monkeypatch.setattr(repeated, "_run", unexpected_run)
    repeated_result = repeated.run()
    assert repeated_result.skipped
    assert repeated_result.boa_paths == result.boa_paths
    assert repeated_result.qai_paths == result.qai_paths
    assert repeated_calls == []


def test_force_level2_changed_publication_is_revalidated_in_one_batch(tmp_path, monkeypatch):
    config = ForceLevel2Config(
        input_path=_make_safe_directory(tmp_path / "input"),
        output_root=tmp_path / "output",
        runtime="native",
    )
    first = ForceLevel2Processor(config)
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(first, "_run", _fake_force_runtime(first))
    result = first.run()
    boa = result.boa_paths[0]
    stat = boa.stat()
    os.utime(boa, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    repeated = ForceLevel2Processor(config)
    validation_runtime = _fake_force_runtime(repeated)
    calls: list[str] = []

    def validation_run(arguments, **kwargs):
        executable = Path(arguments[0]).name
        calls.append(executable)
        if executable in {"force-level2", "force-mosaic"}:
            raise AssertionError("a valid publication must not be reprocessed")
        return validation_runtime(arguments, **kwargs)

    monkeypatch.setattr(repeated, "_run", validation_run)
    revalidated = repeated.run()

    assert revalidated.skipped
    # Version, target CRS, then one batch for the chips and one for the mosaics.
    assert calls == ["force-info", "gdalsrsinfo", "sh", "sh"]
    manifest = json.loads(repeated.manifest_path.read_text(encoding="utf-8"))
    assert manifest["output_signature"] == repeated._output_signature(result.level2_root)

    third = ForceLevel2Processor(config)
    monkeypatch.setattr(third, "_run", lambda *_args, **_kwargs: pytest.fail("no commands"))
    assert third.run().skipped


def test_force_level2_thread_settings_do_not_invalidate_completed_jobs(tmp_path, monkeypatch):
    safe = _make_safe_directory(tmp_path / "input")
    output_root = tmp_path / "output"
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    first = ForceLevel2Processor(
        ForceLevel2Config(input_path=safe, output_root=output_root, runtime="native", nthread=2)
    )
    monkeypatch.setattr(first, "_run", _fake_force_runtime(first))
    first.run()

    # Rewrite the manifest as an older TerraVault would have: thread settings in
    # the fingerprint and no output signature.
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    input_signature = manifest["input_signature"]
    auxiliary_signature = manifest["auxiliary_signature"]
    manifest["fingerprint"] = first._fingerprint(
        input_signature,
        auxiliary_signature,
        performance={"nproc": 1, "nthread": 2, "parallel_reads": False},
    )
    del manifest["output_signature"]
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    retuned = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=safe,
            output_root=output_root,
            runtime="native",
            nthread=8,
            parallel_reads=True,
        )
    )
    validation_runtime = _fake_force_runtime(retuned)

    def validation_run(arguments, **kwargs):
        if Path(arguments[0]).name in {"force-level2", "force-mosaic"}:
            raise AssertionError("changing threads must not reprocess a completed SAFE")
        return validation_runtime(arguments, **kwargs)

    monkeypatch.setattr(retuned, "_run", validation_run)
    assert retuned.run().skipped
    upgraded = json.loads(retuned.manifest_path.read_text(encoding="utf-8"))
    assert upgraded["fingerprint"] == retuned._fingerprint(input_signature, auxiliary_signature)
    assert upgraded["settings"]["nthread"] == 2
    assert upgraded["output_signature"]

    changed = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=safe,
            output_root=output_root,
            runtime="native",
            cloud_buffer=150,
        )
    )
    monkeypatch.setattr(changed, "_run", _fake_force_runtime(changed))
    with pytest.raises(FileExistsError, match="settings or input changed"):
        changed.run()


def test_force_level2_inspection_script_batches_gdal_in_one_shell(tmp_path, monkeypatch):
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    calls = tmp_path / "calls.txt"
    (bin_root / "gdalinfo").write_text(
        "#!/bin/sh\n"
        f'echo "gdalinfo $*" >> "{calls}"\n'
        'case "$4" in *broken*) echo partial; exit 1;; esac\n'
        'printf \'{\\n  "path": "%s"\\n}\' "$4"\n',
        encoding="utf-8",
    )
    (bin_root / "gdalsrsinfo").write_text(
        "#!/bin/sh\n"
        f'echo "gdalsrsinfo $*" >> "{calls}"\n'
        "printf 'PROJCS[\"Swiss\",\\n    UNIT[\"metre\",1]]\\n'\n",
        encoding="utf-8",
    )
    for executable in bin_root.iterdir():
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_root}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "input"),
            output_root=tmp_path / "output",
            runtime="native",
        )
    )
    chip = tmp_path / "X0000_Y0000" / "BOA.tif"
    overview = tmp_path / "X0000_Y0000" / "OVV.jpg"

    inspections = processor._inspect_force_rasters([(chip, True), (overview, False)])

    assert inspections[chip]["metadata"] == {"path": str(chip)}
    assert inspections[chip]["projection_wkt"] == 'PROJCS["Swiss", UNIT["metre",1]]'
    assert inspections[overview] == {"metadata": {"path": str(overview)}, "projection_wkt": None}
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"gdalinfo -json -mdd all {chip}",
        f"gdalsrsinfo -o wkt1 {chip}",
        f"gdalinfo -json -mdd all {overview}",
    ]
    assert [command[0] for command in processor._commands] == ["sh"]

    with pytest.raises(RuntimeError, match="GDAL could not inspect FORCE output: .*broken"):
        processor._inspect_force_rasters([(tmp_path / "broken.tif", False), (chip, True)])


def test_force_level2_accepts_force_six_decimal_origin_serialization(tmp_path, monkeypatch):
    processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "input"),
            output_root=tmp_path / "output",
            runtime="native",
            origin_lon=5.1234567,
            origin_lat=47.9876543,
        )
    )
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(processor, "_run", _fake_force_runtime(processor))

    result = processor.run()

    assert result.status == "complete"


def test_force_level2_sanitizes_non_utf8_vrt_metadata(tmp_path):
    processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "input"),
            output_root=tmp_path / "output",
            runtime="native",
            dry_run=True,
        )
    )
    mosaic = processor.attempt_level2_root / "mosaic" / "test.vrt"
    mosaic.parent.mkdir(parents=True)
    mosaic.write_bytes(b"<VRTDataset><Metadata>bad-\x9e</Metadata></VRTDataset>")

    processor._sanitize_mosaic_xml(root=processor.attempt_level2_root)

    text = mosaic.read_text(encoding="utf-8")
    assert "bad-\ufffd" in text
    ET.fromstring(text)


def test_force_level2_recovers_a_publication_backup_before_reprocessing(tmp_path, monkeypatch):
    config = ForceLevel2Config(
        input_path=_make_safe_directory(tmp_path / "input"),
        output_root=tmp_path / "output",
        runtime="native",
    )
    first = ForceLevel2Processor(config)
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(first, "_run", _fake_force_runtime(first))
    original = first.run()
    os.replace(first.level2_root, first.publication_backup)
    assert not first.level2_root.exists()

    repeated = ForceLevel2Processor(config)
    monkeypatch.setattr(repeated, "_run", _fake_force_runtime(repeated))
    recovered = repeated.run()

    assert recovered.skipped
    assert recovered.boa_paths == original.boa_paths
    assert first.level2_root.is_dir()
    assert not first.publication_backup.exists()


def test_force_level2_recovers_publication_completed_before_manifest(tmp_path, monkeypatch):
    config = ForceLevel2Config(
        input_path=_make_safe_directory(tmp_path / "input"),
        output_root=tmp_path / "output",
        runtime="native",
    )
    first = ForceLevel2Processor(config)
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(first, "_run", _fake_force_runtime(first))
    first.run()
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "processing"
    manifest["queue_status"] = "QUEUED"
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    repeated = ForceLevel2Processor(config)
    validation_runtime = _fake_force_runtime(repeated)

    def validation_run(arguments, **kwargs):
        if Path(arguments[0]).name in {"force-level2", "force-mosaic"}:
            raise AssertionError("published complete output must not be reprocessed")
        return validation_runtime(arguments, **kwargs)

    monkeypatch.setattr(repeated, "_run", validation_run)
    recovered = repeated.run()

    assert recovered.skipped
    repaired = json.loads(repeated.manifest_path.read_text(encoding="utf-8"))
    assert repaired["status"] == "complete"
    assert repaired["queue_status"] == "DONE"


def test_force_level2_resumes_done_attempt_at_validation(tmp_path, monkeypatch):
    config = ForceLevel2Config(
        input_path=_make_safe_directory(tmp_path / "input"),
        output_root=tmp_path / "output",
        runtime="native",
        retry_failed=True,
    )
    first = ForceLevel2Processor(config)
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(first, "_run", _fake_force_runtime(first))
    first.run()
    first.attempt_root.mkdir(parents=True)
    os.replace(first.level2_root, first.attempt_level2_root)
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "failed"
    manifest["error"] = "validation decoder failed"
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    repeated = ForceLevel2Processor(config)
    validation_runtime = _fake_force_runtime(repeated)
    calls: list[str] = []

    def validation_run(arguments, **kwargs):
        executable = Path(arguments[0]).name
        calls.append(executable)
        if executable == "force-level2":
            raise AssertionError("DONE attempt must resume after core processing")
        return validation_runtime(arguments, **kwargs)

    monkeypatch.setattr(repeated, "_run", validation_run)
    recovered = repeated.run()

    assert recovered.status == "complete"
    assert "force-level2" not in calls
    assert recovered.level2_root.is_dir()
    repaired = json.loads(repeated.manifest_path.read_text(encoding="utf-8"))
    assert repaired["attempts"] == manifest["attempts"]


@pytest.mark.parametrize(
    ("failure", "message"),
    (("metadata", "OVV band metadata"), ("dimensions", "OVV chip dimensions")),
)
def test_force_level2_rejects_invalid_overview_contract(
    tmp_path,
    monkeypatch,
    failure,
    message,
):
    processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "input"),
            output_root=tmp_path / "output",
            runtime="native",
        )
    )
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    def invalid_overview(path, metadata):
        if "_OVV." not in path:
            return metadata
        if failure == "metadata":
            metadata["bands"][0]["colorInterpretation"] = "Undefined"
        else:
            metadata["size"][0] += 1
        return metadata

    monkeypatch.setattr(
        processor,
        "_run",
        _fake_force_runtime(processor, metadata_hook=invalid_overview),
    )

    with pytest.raises(RuntimeError, match=message):
        processor.run()

    manifest = json.loads(processor.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"


def test_force_level2_fail_queue_is_recorded_durably(tmp_path, monkeypatch):
    safe = _make_safe_directory(tmp_path / "input")
    processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=safe,
            output_root=tmp_path / "output",
            runtime="native",
        )
    )
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(
        processor,
        "_run",
        _fake_force_runtime(processor, queue_status="FAIL"),
    )

    with pytest.raises(RuntimeError, match="queue=FAIL"):
        processor.run()

    manifest = json.loads(processor.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["queue_status"] == "FAIL"
    assert manifest["attempts"] == 1
    assert "queue=FAIL" in manifest["error"]


def test_force_level2_rejects_a_changed_output_root_grid(tmp_path, monkeypatch):
    output_root = tmp_path / "output"
    first = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "first"),
            output_root=output_root,
            runtime="native",
        )
    )
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(first, "_run", _fake_force_runtime(first))
    first.run()

    second_name = PRODUCT_NAME.replace("20260717", "20260718")
    second = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(
                tmp_path / "second",
                product_name=second_name,
            ),
            output_root=output_root,
            runtime="native",
            target_crs="EPSG:4326",
        )
    )
    monkeypatch.setattr(second, "_run", _fake_force_runtime(second))

    with pytest.raises(FileExistsError, match="immutable cube grid"):
        second.run()


def test_force_level2_keeps_same_date_sensor_products_isolated(tmp_path, monkeypatch):
    output_root = tmp_path / "output"
    first = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "first"),
            output_root=output_root,
            runtime="native",
        )
    )
    second_name = PRODUCT_NAME.replace("20260717T142404", "20260717T155959")
    second = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(
                tmp_path / "second",
                product_name=second_name,
            ),
            output_root=output_root,
            runtime="native",
        )
    )
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(first, "_run", _fake_force_runtime(first))
    monkeypatch.setattr(second, "_run", _fake_force_runtime(second))

    first_result = first.run()
    second_result = second.run()

    assert first.product_tag == second.product_tag
    assert first_result.level2_root != second_result.level2_root
    assert first_result.boa_paths[0].is_file()
    assert second_result.boa_paths[0].is_file()
    assert first_result.manifest_path != second_result.manifest_path


def test_force_level2_cli_parser_and_default_log(tmp_path):
    output_root = tmp_path / "force-native"
    args = build_parser().parse_args(
        [
            "force-level2",
            "--input",
            f"{PRODUCT_NAME}.zip",
            "--output-root",
            str(output_root),
            "--runtime",
            "docker",
            "--force-cloud-threshold",
            "0.3",
            "--resolution-merge",
            "NONE",
        ]
    )

    assert args.runtime == "docker"
    assert args.force_cloud_threshold == 0.3
    assert args.force_shadow_threshold == 0.02
    assert args.resolution == 10
    assert args.resolution_merge == "NONE"
    assert args.download_max_retries == 5
    assert args.download_retry_base_seconds == 2.0
    assert args.download_quota_wait_seconds == 900.0
    assert _default_log_file(args) == (output_root / "_terravault/logs/force-level2.log")


def test_force_level2_default_threads_share_host_cpus(tmp_path, monkeypatch):
    monkeypatch.setattr("terravault.force_level2.os.cpu_count", lambda: 12)

    config = ForceLevel2Config(
        input_path=_make_safe_directory(tmp_path / "input"),
        output_root=tmp_path / "output",
        dry_run=True,
    )

    assert config.nthread == 12
    assert default_force_threads(4) == 3
    assert default_force_threads(24) == 1
    with pytest.raises(ValueError, match="scene_workers"):
        default_force_threads(0)


def test_force_level2_locks_each_safe_separately(tmp_path):
    output_root = tmp_path / "output"
    first = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "first"),
            output_root=output_root,
            dry_run=True,
        )
    )
    second_name = PRODUCT_NAME.replace("20260717T142404", "20260717T155959")
    second = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "second", product_name=second_name),
            output_root=output_root,
            dry_run=True,
        )
    )
    same_as_first = ForceLevel2Processor(first.config)

    with _RunLock(first.lock_path), _RunLock(second.lock_path):
        with pytest.raises(RuntimeError, match="Another FORCE L2PS worker holds"):
            with _RunLock(same_as_first.lock_path):
                pass


def test_force_level2_cancel_terminates_the_running_command(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "input"),
            output_root=tmp_path / "output",
            runtime="native",
        )
    )
    outcome: list[BaseException] = []

    def execute() -> None:
        try:
            processor._execute_force(sys.executable, ["-c", "import time; time.sleep(30)"])
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    worker = threading.Thread(target=execute)
    started = time.monotonic()
    worker.start()
    time.sleep(0.3)
    processor.cancel()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert time.monotonic() - started < 5
    assert len(outcome) == 1 and isinstance(outcome[0], ForceLevel2Cancelled)
    with pytest.raises(ForceLevel2Cancelled):
        processor._execute_force("force-info", [])


def test_force_level2_cancelled_job_is_recorded_as_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "terravault.force_level2.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    processor = ForceLevel2Processor(
        ForceLevel2Config(
            input_path=_make_safe_directory(tmp_path / "input"),
            output_root=tmp_path / "output",
            runtime="native",
        )
    )
    fake_runtime = _fake_force_runtime(processor, queue_status="QUEUED", returncode=143)
    force_started = threading.Event()

    def cancellable_run(arguments, **kwargs):
        if Path(arguments[0]).name == "force-level2":
            force_started.set()
            assert processor._cancelled.wait(5)
        return fake_runtime(arguments, **kwargs)

    monkeypatch.setattr(processor, "_run", cancellable_run)
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            processor.run()
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert force_started.wait(5)
    processor.cancel()
    worker.join(timeout=10)

    assert isinstance(outcome[0], ForceLevel2Cancelled)
    manifest = json.loads(processor.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "interrupted"
    assert manifest["output_signature"] is None
