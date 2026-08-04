"""Signal handling for the native FORCE Level-2 CLI command."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from terravault.cli import build_parser, cmd_force_level2
from terravault.force_level2 import ForceLevel2Processor
from terravault.l1c_download import L1CProductDownloader


PRODUCT_NAME = "S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T142404.SAFE"
PRODUCT_STEM = PRODUCT_NAME.removesuffix(".SAFE")
BANDS = ("01", "02", "03", "04", "05", "06", "07", "08", "8A", "09", "10", "11", "12")


def _make_safe(tmp_path: Path) -> Path:
    safe = tmp_path / PRODUCT_NAME
    granule = safe / "GRANULE" / "L1C_T32TMT_A000001_20260717T103029"
    (safe / "manifest.safe").parent.mkdir(parents=True, exist_ok=True)
    (safe / "manifest.safe").write_bytes(b"manifest")
    (safe / "MTD_MSIL1C.xml").write_bytes(b"product metadata")
    (granule / "MTD_TL.xml").parent.mkdir(parents=True, exist_ok=True)
    (granule / "MTD_TL.xml").write_bytes(b"granule metadata")
    image_root = granule / "IMG_DATA"
    image_root.mkdir()
    for band in BANDS:
        (image_root / f"T32TMT_20260717T103029_B{band}.jp2").write_bytes(band.encode("ascii"))
    return safe


def _force_args(input_path: Path, output_root: Path):
    return build_parser().parse_args(
        [
            "force-level2",
            "--input",
            str(input_path),
            "--output-root",
            str(output_root),
            "--runtime",
            "native",
        ]
    )


def _stac_args(item_path: Path, output_root: Path):
    return build_parser().parse_args(
        [
            "force-level2",
            "--stac-item",
            str(item_path),
            "--output-root",
            str(output_root),
            "--runtime",
            "native",
        ]
    )


def test_sigterm_marks_active_force_job_interrupted_and_returns_143(
    tmp_path,
    monkeypatch,
    capsys,
):
    safe = _make_safe(tmp_path / "input")
    output_root = tmp_path / "output"
    original_handler = signal.getsignal(signal.SIGTERM)
    previous_calls: list[int] = []

    def previous_handler(signum, _frame):
        previous_calls.append(signum)

    signal.signal(signal.SIGTERM, previous_handler)
    observed_handler: list[object] = []

    def fake_run(arguments, *, check=True, log_path=None, container_cid_path=None):
        del check, log_path, container_cid_path
        executable = Path(arguments[0]).name
        if executable == "force-info":
            return subprocess.CompletedProcess(
                arguments,
                0,
                "Hello, running FORCE v. 3.10.04\n",
                "",
            )
        if executable == "gdalsrsinfo":
            return subprocess.CompletedProcess(
                arguments,
                0,
                'PROJCS["Swiss",\nUNIT["metre",1]]\n',
                "",
            )
        if executable == "force-level2":
            observed_handler.append(signal.getsignal(signal.SIGTERM))
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("SIGTERM handler did not interrupt FORCE processing")
        raise AssertionError(f"Unexpected executable before interruption: {executable}")

    monkeypatch.setattr(
        "terravault.force_level2.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(ForceLevel2Processor, "_run", staticmethod(fake_run))
    try:
        exit_code = cmd_force_level2(_force_args(safe, output_root))
        restored_handler = signal.getsignal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, original_handler)

    assert exit_code == 128 + signal.SIGTERM
    assert observed_handler and observed_handler[0] is not previous_handler
    assert restored_handler is previous_handler
    assert previous_calls == []
    manifest_path = output_root / "_terravault" / "force-l2" / "jobs" / f"{PRODUCT_STEM}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "interrupted"
    assert manifest["attempts"] == 1
    assert manifest["queue_status"] == "QUEUED"
    assert "Received signal" in manifest["error"]
    assert Path(manifest["parameter_path"]).is_file()
    assert Path(manifest["queue_path"]).is_file()
    assert Path(manifest["attempt_level2_root"]).is_dir()
    assert "resumable download and job state were retained" in capsys.readouterr().err


def test_sigterm_marks_product_download_interrupted_and_keeps_partial_bytes(
    tmp_path,
    monkeypatch,
):
    output_root = tmp_path / "output"
    item_path = tmp_path / "item.json"
    item_path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "id": PRODUCT_STEM,
                "properties": {"_private": {"product_name": PRODUCT_NAME}},
                "assets": {
                    "Product": {
                        "href": f"https://example.test/Products({PRODUCT_STEM})/$value",
                        "file:size": 100,
                        "file:local_path": f"{PRODUCT_NAME}.zip",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TERRAVAULT_CDSE_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("TERRAVAULT_CDSE_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("TERRAVAULT_CDSE_S3_SECRET_KEY", raising=False)

    class InterruptingResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        def iter_content(self, *, chunk_size):
            del chunk_size
            yield b"resumable-bytes"
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("SIGTERM handler did not interrupt the download")

    class InterruptingSession:
        def get(self, *_args, **_kwargs):
            return InterruptingResponse()

        def close(self):
            return None

    monkeypatch.setattr(
        L1CProductDownloader,
        "_http_session",
        lambda _self: InterruptingSession(),
    )
    original_handler = signal.getsignal(signal.SIGTERM)

    def previous_handler(_signum, _frame):
        return None

    signal.signal(signal.SIGTERM, previous_handler)
    try:
        exit_code = cmd_force_level2(_stac_args(item_path, output_root))
        restored_handler = signal.getsignal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, original_handler)

    assert exit_code == 128 + signal.SIGTERM
    assert restored_handler is previous_handler
    manifest_path = output_root / "_terravault" / "force-l2" / "downloads" / f"{PRODUCT_STEM}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "interrupted"
    assert manifest["completed_bytes"] == len(b"resumable-bytes")
    assert manifest["fingerprint"]
    partial = output_root / "level1" / f"{PRODUCT_NAME}.zip.part"
    assert partial.read_bytes() == b"resumable-bytes"
