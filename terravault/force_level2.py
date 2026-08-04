"""Native FORCE Level-2 processing for complete Sentinel-2 L1C products.

This module is deliberately separate from :mod:`terravault.force`, which
imports already processed TerraVault rasters as external features.  Here FORCE
L2PS owns atmospheric correction and creates genuine BOA and bit-packed QAI
products, including its native cloud, cirrus, shadow and snow classification.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import platform
import re
import shlex
import signal
import shutil
import subprocess
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .force import FORCE_DOCKER_IMAGE, FORCE_DOCKER_PLATFORM, FORCE_VERSION

logger = logging.getLogger(__name__)

_L1C_NAME = re.compile(
    r"^(?P<platform>S2[ABC])_MSIL1C_"
    r"(?P<sensing>\d{8}T\d{6})_"
    r"(?P<baseline>N\d{4})_"
    r"(?P<orbit>R\d{3})_"
    r"(?P<tile>T\d{2}[A-Z]{3})_"
    r"(?P<production>\d{8}T\d{6})\.SAFE(?:\.zip)?$"
)
_RESOLUTION_MERGES = {"IMPROPHE", "REGRESSION", "STARFM", "NONE"}
_L1C_BANDS = {
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
    "B10",
    "B11",
    "B12",
}
_L1C_BAND_NAME = re.compile(r"_(B(?:0[1-9]|1[0-2]|8A))\.jp2$")
_TILE_NAME = re.compile(r"^X(?P<x>-?\d+)_Y(?P<y>-?\d+)$")


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class ForceLevel2Config:
    """Configuration for one complete Sentinel-2 L1C → FORCE BOA/QAI job."""

    input_path: Path
    output_root: Path
    runtime: str = "auto"
    docker_image: str = FORCE_DOCKER_IMAGE
    docker_platform: str | None = FORCE_DOCKER_PLATFORM
    mount_root: Path | None = None
    target_crs: str = "EPSG:2056"
    origin_lon: float = 5.5
    origin_lat: float = 48.0
    tile_size: int = 30_000
    resolution: float = 10.0
    aoi_path: Path | None = None
    dem_path: Path | None = None
    dem_nodata: int = -32767
    cloud_buffer: float = 300.0
    cirrus_buffer: float = 0.0
    shadow_buffer: float = 90.0
    snow_buffer: float = 30.0
    cloud_threshold: float = 0.225
    shadow_threshold: float = 0.02
    max_cloud_cover_frame: int = 100
    max_cloud_cover_tile: int = 100
    resolution_merge: str = "IMPROPHE"
    nproc: int = 1
    nthread: int = 2
    parallel_reads: bool = False
    output_overview: bool = True
    overwrite: bool = False
    retry_failed: bool = False
    dry_run: bool = False
    progress_interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_path", _absolute(self.input_path))
        object.__setattr__(self, "output_root", _absolute(self.output_root))
        if self.mount_root is not None:
            object.__setattr__(self, "mount_root", _absolute(self.mount_root))
        if self.aoi_path is not None:
            object.__setattr__(self, "aoi_path", _absolute(self.aoi_path))
        if self.dem_path is not None:
            object.__setattr__(self, "dem_path", _absolute(self.dem_path))
        if self.runtime not in {"auto", "native", "docker"}:
            raise ValueError("runtime must be 'auto', 'native' or 'docker'")
        if not self.docker_image.strip():
            raise ValueError("docker_image cannot be empty")
        if self.docker_platform is not None and not self.docker_platform.strip():
            raise ValueError("docker_platform cannot be empty; use None to omit it")
        if not (-180 <= self.origin_lon <= 180 and -90 <= self.origin_lat <= 90):
            raise ValueError("FORCE grid origin is outside valid longitude/latitude bounds")
        if self.tile_size <= 0 or self.resolution <= 0:
            raise ValueError("tile_size and resolution must be positive")
        if not math.isclose(
            self.tile_size / self.resolution,
            round(self.tile_size / self.resolution),
            abs_tol=1e-9,
        ):
            raise ValueError("tile_size must be an exact multiple of resolution")
        for name in ("cloud_buffer", "cirrus_buffer", "shadow_buffer", "snow_buffer"):
            value = getattr(self, name)
            if not 0 <= value <= 10_000:
                raise ValueError(f"{name} must be between 0 and 10000 metres")
        for name in ("cloud_threshold", "shadow_threshold"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in ("max_cloud_cover_frame", "max_cloud_cover_tile"):
            value = getattr(self, name)
            if not 1 <= value <= 100:
                raise ValueError(f"{name} must be between 1 and 100")
        if not -32768 <= self.dem_nodata <= 32767:
            raise ValueError("dem_nodata must fit a signed 16-bit integer")
        merge = self.resolution_merge.upper()
        if merge not in _RESOLUTION_MERGES:
            raise ValueError("resolution_merge must be IMPROPHE, REGRESSION, STARFM or NONE")
        object.__setattr__(self, "resolution_merge", merge)
        if self.nproc < 1 or self.nthread < 1:
            raise ValueError("nproc and nthread must be at least 1")
        if self.progress_interval_seconds < 1:
            raise ValueError("progress_interval_seconds must be at least 1")
        configured_paths = (
            self.input_path,
            self.output_root,
            self.mount_root,
            self.aoi_path,
            self.dem_path,
        )
        if any(
            path is not None and any(character.isspace() for character in str(path))
            for path in configured_paths
        ):
            raise ValueError("FORCE Level-2 paths cannot contain whitespace")


@dataclass(frozen=True)
class ForceLevel2Result:
    """Outcome of a native FORCE L2PS job."""

    status: str
    runtime: str
    input_path: Path
    level2_root: Path
    parameter_path: Path
    queue_path: Path
    manifest_path: Path
    boa_paths: tuple[Path, ...]
    qai_paths: tuple[Path, ...]
    overview_paths: tuple[Path, ...]
    boa_mosaic_path: Path | None
    qai_mosaic_path: Path | None
    commands: tuple[tuple[str, ...], ...]
    skipped: bool = False


@dataclass(frozen=True)
class ForceLevel2Status:
    """Observable, non-speculative progress for one FORCE Level-2 job."""

    job_stem: str
    status: str
    phase: str
    elapsed_seconds: float | None
    queue_status: str | None
    boa_tiles: int
    qai_tiles: int
    overview_tiles: int
    output_bytes: int
    latest_output_at: str | None
    manifest_path: Path
    progress_path: Path | None
    log_paths: tuple[Path, ...]
    within_scene_percent: None = None


def inspect_force_level2_status(
    output_root: str | Path,
    *,
    job_stem: str | None = None,
) -> tuple[ForceLevel2Status, ...]:
    """Inspect durable FORCE milestones without inventing an internal percentage."""

    root = _absolute(output_root)
    jobs_root = root / "_terravault" / "force-l2" / "jobs"
    if job_stem is not None and (Path(job_stem).name != job_stem or job_stem.endswith(".json")):
        raise ValueError("job_stem must be a SAFE stem without path separators or .json")
    manifests = (
        [jobs_root / f"{job_stem}.json"]
        if job_stem is not None
        else sorted(jobs_root.glob("*.json"))
    )
    statuses: list[ForceLevel2Status] = []
    now = datetime.now(timezone.utc)
    for manifest_path in manifests:
        if not manifest_path.is_file():
            continue
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        stem = manifest_path.stem
        published = Path(str(payload.get("level2_root") or root / "level2" / "products" / stem))
        attempt = Path(
            str(
                payload.get("attempt_level2_root")
                or root / "_terravault" / "force-l2" / "attempts" / stem / "level2"
            )
        )
        product_root = attempt if attempt.is_dir() else published
        product_tag = str(payload.get("product_tag") or "")
        boa = tuple(product_root.glob(f"X*_Y*/{product_tag}_BOA.tif"))
        qai = tuple(product_root.glob(f"X*_Y*/{product_tag}_QAI.tif"))
        overviews = tuple(
            path
            for path in product_root.glob(f"X*_Y*/{product_tag}_OVV.*")
            if path.suffix.casefold() in {".jpg", ".jpeg"}
        )
        outputs = tuple(path for path in (*boa, *qai, *overviews) if path.is_file())
        queue_status = payload.get("queue_status")
        queue_path = Path(str(payload.get("queue_path") or ""))
        if queue_path.is_file():
            lines = [
                line.strip()
                for line in queue_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if lines:
                live_queue = lines[-1].rsplit(maxsplit=1)[-1]
                if live_queue in {"QUEUED", "DONE", "FAIL"}:
                    queue_status = live_queue
        status = str(payload.get("status") or "unknown")
        progress_path = Path(
            str(
                payload.get("progress_path")
                or root / "_terravault" / "force-l2" / "progress" / f"{stem}.json"
            )
        )
        progress: dict[str, Any] = {}
        if progress_path.is_file():
            try:
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                progress = {}
        if status == "complete":
            phase = "complete"
        elif status in {"failed", "interrupted"}:
            phase = status
        elif queue_status == "DONE":
            phase = "validation-or-mosaic"
        elif boa or qai:
            phase = "writing-force-tiles"
        else:
            phase = "force-l2ps-core"
        if status == "processing" and progress.get("phase"):
            phase = str(progress["phase"])
        started_text = payload.get("started_at")
        if started_text is None and status == "processing":
            started_text = payload.get("updated_at")
        elapsed: float | None = None
        if started_text:
            try:
                started = datetime.fromisoformat(str(started_text))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                elapsed = max(0.0, (now - started).total_seconds())
            except ValueError:
                pass
        if isinstance(progress.get("elapsed_seconds"), (int, float)):
            elapsed = max(0.0, float(progress["elapsed_seconds"]))
        latest_mtime = max((path.stat().st_mtime for path in outputs), default=None)
        log_candidates = tuple(
            Path(str(value))
            for value in (
                payload.get("batch_log_path"),
                payload.get("product_log_root"),
            )
            if value
        )
        statuses.append(
            ForceLevel2Status(
                job_stem=stem,
                status=status,
                phase=phase,
                elapsed_seconds=elapsed,
                queue_status=None if queue_status is None else str(queue_status),
                boa_tiles=len(boa),
                qai_tiles=len(qai),
                overview_tiles=len(overviews),
                output_bytes=sum(path.stat().st_size for path in outputs),
                latest_output_at=(
                    None
                    if latest_mtime is None
                    else datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat()
                ),
                manifest_path=manifest_path.resolve(),
                progress_path=progress_path.resolve() if progress_path.is_file() else None,
                log_paths=tuple(path.resolve() for path in log_candidates if path.exists()),
            )
        )
    return tuple(statuses)


class _RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: Any | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.stream.close()
            raise RuntimeError(f"Another FORCE L2PS worker holds {self.path}") from exc
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write(f"pid={os.getpid()}\n")
        self.stream.flush()
        return self

    def __exit__(self, *_args: object) -> None:
        if self.stream is not None:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


class ForceLevel2Processor:
    """Run FORCE L2PS with durable queue, parameters and provenance."""

    def __init__(self, config: ForceLevel2Config) -> None:
        self.config = config
        (
            self.product_name,
            self.product_tag,
            self.product_identity,
        ) = self._product_identity()
        self.job_stem = self.product_name.removesuffix(".SAFE").removesuffix(".zip")
        self.pool_root = config.output_root / "level2"
        self.level2_root = self.pool_root / "products" / self.job_stem
        self.state_root = config.output_root / "_terravault" / "force-l2"
        self.log_root = self.state_root / "logs"
        self.provenance_root = self.state_root / "provenance"
        self.product_log_root = self.log_root / self.job_stem
        self.product_provenance_root = self.provenance_root / self.job_stem
        self.attempt_root = self.state_root / "attempts" / self.job_stem
        self.attempt_level2_root = self.attempt_root / "level2"
        self.temp_root = self.attempt_root / "scratch"
        self.parameter_root = self.state_root / "parameters"
        self.queue_root = self.state_root / "queues"
        self.job_root = self.state_root / "jobs"
        self.progress_root = self.state_root / "progress"
        self.container_root = self.state_root / "containers"
        self.cube_config_path = self.state_root / "cube.json"
        self._commands: list[tuple[str, ...]] = []
        self._runtime: str | None = None
        self._mount_root: Path | None = None
        self._actual_force_version: str | None = None
        self._expected_projection_wkt: str | None = None
        self._started_at: str | None = None
        self.parameter_path = self.parameter_root / f"{self.job_stem}.prm"
        self.queue_path = self.queue_root / f"{self.job_stem}.txt"
        self.manifest_path = self.job_root / f"{self.job_stem}.json"
        self.progress_path = self.progress_root / f"{self.job_stem}.json"
        self.container_cid_path = self.container_root / f"{self.job_stem}.cid"
        self.batch_log_path = self.log_root / f"{self.job_stem}.batch.log"
        self.lock_path = self.state_root / "worker.lock"

    def _product_identity(self) -> tuple[str, str, dict[str, str]]:
        name = self.config.input_path.name
        match = _L1C_NAME.fullmatch(name)
        if match is None:
            raise ValueError(
                "Expected a complete Sentinel-2 L1C product named "
                "S2*_MSIL1C_*.SAFE or S2*_MSIL1C_*.SAFE.zip"
            )
        product_name = name[:-4] if name.endswith(".zip") else name
        sensor = match.group("platform").replace("S2", "SEN2")
        identity = {
            "platform": match.group("platform"),
            "sensing_time": match.group("sensing"),
            "processing_baseline": match.group("baseline"),
            "relative_orbit": match.group("orbit"),
            "mgrs_tile": match.group("tile"),
            "production_time": match.group("production"),
        }
        return product_name, f"{match.group('sensing')[:8]}_LEVEL2_{sensor}", identity

    @staticmethod
    def _run(
        arguments: Sequence[str],
        *,
        check: bool = True,
        log_path: Path | None = None,
        container_cid_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        logger.debug("Running FORCE L2 command: %s", " ".join(arguments))
        log_stream = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_stream = log_path.open("a", encoding="utf-8", buffering=1)
            log_stream.write(
                f"\n# started_at={datetime.now(timezone.utc).isoformat()}\n"
                f"# command={shlex.join(str(argument) for argument in arguments)}\n"
            )
        try:
            process = subprocess.Popen(
                list(arguments),
                stdout=subprocess.PIPE if log_stream is None else log_stream,
                stderr=subprocess.PIPE if log_stream is None else subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name == "posix",
            )
        except BaseException:
            if log_stream is not None:
                log_stream.close()
            raise
        try:
            if log_stream is None:
                stdout, stderr = process.communicate()
            else:
                process.wait()
                stdout, stderr = "", ""
        except BaseException as exc:
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
                if container_cid_path is not None:
                    ForceLevel2Processor._stop_docker_container(container_cid_path)
                try:
                    if log_stream is None:
                        process.communicate(timeout=10)
                    else:
                        process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    if container_cid_path is not None:
                        ForceLevel2Processor._stop_docker_container(container_cid_path)
                    if log_stream is None:
                        process.communicate()
                    else:
                        process.wait()
            if log_stream is not None:
                log_stream.write(
                    f"\n# interrupted_at={datetime.now(timezone.utc).isoformat()}\n"
                    f"# exception={type(exc).__name__}: {exc}\n"
                )
            raise
        finally:
            if log_stream is not None:
                if process.poll() is not None:
                    log_stream.write(
                        f"\n# finished_at={datetime.now(timezone.utc).isoformat()}\n"
                        f"# exit_code={process.returncode}\n"
                    )
                log_stream.close()
        if log_path is not None:
            stdout = ""
        if container_cid_path is not None:
            container_cid_path.unlink(missing_ok=True)
        completed = subprocess.CompletedProcess(
            list(arguments),
            process.returncode,
            stdout,
            stderr,
        )
        if completed.stdout.strip():
            logger.debug("FORCE L2 stdout: %s", completed.stdout.strip())
        if completed.stderr.strip():
            level = logging.ERROR if completed.returncode else logging.DEBUG
            logger.log(level, "FORCE L2 stderr: %s", completed.stderr.strip())
        if check and completed.returncode:
            detail = (
                f"inspect {log_path}"
                if log_path is not None
                else completed.stderr.strip() or completed.stdout.strip()
            )
            raise RuntimeError(
                f"FORCE L2 command failed with exit code {completed.returncode}: {detail}"
            )
        return completed

    @staticmethod
    def _stop_docker_container(cid_path: Path) -> None:
        """Stop a daemon-side container that may outlive an interrupted Docker CLI."""

        try:
            container_id = cid_path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not container_id:
            return
        try:
            stopped = subprocess.run(
                ["docker", "stop", "--time", "10", container_id],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if stopped.returncode:
                subprocess.run(
                    ["docker", "kill", container_id],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            subprocess.run(
                ["docker", "rm", "--force", container_id],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Could not stop interrupted Docker container %s: %s", container_id, exc)
        else:
            cid_path.unlink(missing_ok=True)

    def _select_runtime(self) -> str:
        if self._runtime is not None:
            return self._runtime
        if self.config.runtime == "auto":
            native = platform.system() == "Linux" and all(
                shutil.which(command)
                for command in (
                    "force-info",
                    "force-level2",
                    "force-l2ps",
                    "force-mosaic",
                    "gdalinfo",
                    "gdalsrsinfo",
                )
            )
            runtime = "native" if native else "docker"
        else:
            runtime = self.config.runtime
        if runtime == "native":
            missing = [
                command
                for command in (
                    "force-info",
                    "force-level2",
                    "force-l2ps",
                    "force-mosaic",
                    "gdalinfo",
                    "gdalsrsinfo",
                )
                if shutil.which(command) is None
            ]
            if missing and not self.config.dry_run:
                raise RuntimeError("Native FORCE runtime is missing: " + ", ".join(missing))
        elif shutil.which("docker") is None and not self.config.dry_run:
            raise RuntimeError("Docker is required because native FORCE is unavailable")
        self._runtime = runtime
        return runtime

    def _docker_mount_root(self) -> Path:
        if self._mount_root is not None:
            return self._mount_root
        paths = [self.config.input_path, self.config.output_root]
        if self.config.aoi_path is not None:
            paths.append(self.config.aoi_path)
        if self.config.dem_path is not None:
            paths.append(self.config.dem_path)
        common = self.config.mount_root or Path(os.path.commonpath(paths))
        if common == Path(common.anchor):
            raise ValueError(
                "Docker inputs and output must share a safely scoped parent; "
                "set mount_root explicitly"
            )
        for path in paths:
            try:
                path.relative_to(common)
            except ValueError as exc:
                raise ValueError(f"{path} is outside Docker mount root {common}") from exc
        self._mount_root = common
        return common

    def _runtime_path(self, path: Path) -> str:
        if self._select_runtime() == "native":
            return str(path)
        return str(Path("/data") / path.relative_to(self._docker_mount_root()))

    def _force_command(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        track_container: bool = False,
    ) -> list[str]:
        if self._select_runtime() == "native":
            return [executable, *arguments]
        command = ["docker", "run", "--rm"]
        if track_container:
            self.container_root.mkdir(parents=True, exist_ok=True)
            self.container_cid_path.unlink(missing_ok=True)
            command.extend(["--cidfile", str(self.container_cid_path)])
        if self.config.docker_platform is not None:
            command.extend(["--platform", self.config.docker_platform])
        command.extend(
            [
                "--volume",
                f"{self._docker_mount_root()}:/data",
                "--env",
                "HOME=/tmp",
                "--env",
                "PARALLEL_HOME=/tmp/.parallel",
                "--env",
                "SHELL=/bin/bash",
            ]
        )
        if hasattr(os, "getuid") and hasattr(os, "getgid"):
            command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        command.extend([self.config.docker_image, executable, *arguments])
        return command

    def _execute_force(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        check: bool = True,
        log_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str] | None:
        track_container = (
            not self.config.dry_run
            and self._select_runtime() == "docker"
            and executable == "force-level2"
        )
        command = self._force_command(
            executable,
            arguments,
            track_container=track_container,
        )
        self._commands.append(tuple(command))
        logger.info("FORCE native Level-2 step – executable=%s", executable)
        if self.config.dry_run:
            return None
        return self._run(
            command,
            check=check,
            log_path=log_path,
            container_cid_path=self.container_cid_path if track_container else None,
        )

    def _validate_input(self) -> None:
        path = self.config.input_path
        required_markers = ("manifest.safe", "MTD_MSIL1C.xml")
        if path.is_dir():
            missing = [name for name in required_markers if not (path / name).is_file()]
            granules = list((path / "GRANULE").glob("L1C_*"))
            incomplete_granules = []
            for granule in granules:
                bands = {
                    match.group(1)
                    for candidate in (granule / "IMG_DATA").glob("*.jp2")
                    if (match := _L1C_BAND_NAME.search(candidate.name)) is not None
                }
                if bands != _L1C_BANDS or not (granule / "MTD_TL.xml").is_file():
                    incomplete_granules.append(granule.name)
            if missing or not granules or incomplete_granules:
                raise ValueError(
                    "Incomplete Sentinel-2 L1C SAFE directory; required product/granule "
                    "metadata and all 13 L1C bands were not found"
                )
        elif path.is_file() and path.name.endswith(".SAFE.zip"):
            try:
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                    roots = {name.split("/", 1)[0] for name in names if "/" in name}
                    expected_root = self.product_name
                    granule_bands: dict[str, set[str]] = {}
                    granule_metadata: set[str] = set()
                    granules: set[str] = set()
                    for name in names:
                        parts = name.split("/")
                        if (
                            len(parts) >= 4
                            and parts[0] == expected_root
                            and parts[1] == "GRANULE"
                            and parts[2].startswith("L1C_")
                        ):
                            granule = parts[2]
                            granules.add(granule)
                            if len(parts) >= 5 and parts[3] == "IMG_DATA":
                                match = _L1C_BAND_NAME.search(parts[-1])
                                if match is not None:
                                    granule_bands.setdefault(granule, set()).add(match.group(1))
                            elif parts[-1] == "MTD_TL.xml":
                                granule_metadata.add(granule)
                    complete = (
                        expected_root in roots
                        and f"{expected_root}/manifest.safe" in names
                        and f"{expected_root}/MTD_MSIL1C.xml" in names
                        and bool(granules)
                        and set(granule_bands) == granules
                        and granules <= granule_metadata
                        and all(bands == _L1C_BANDS for bands in granule_bands.values())
                    )
                    if not complete:
                        raise ValueError(
                            "Incomplete or incorrectly named L1C SAFE ZIP; FORCE requires "
                            f"the root {expected_root}/ with metadata and all bands"
                        )
            except zipfile.BadZipFile as exc:
                raise ValueError(f"Invalid L1C SAFE ZIP: {path}") from exc
        elif not path.exists():
            raise FileNotFoundError(f"L1C input does not exist: {path}")
        else:
            raise ValueError("L1C input must be a .SAFE directory or .SAFE.zip archive")
        for auxiliary in (self.config.aoi_path, self.config.dem_path):
            if auxiliary is not None and not auxiliary.is_file():
                raise FileNotFoundError(f"FORCE auxiliary input does not exist: {auxiliary}")
        if self.config.dem_path is None:
            logger.warning(
                "FORCE L2PS is running without a DEM; native cloud-shadow detection and "
                "atmospheric correction quality will be reduced, and topographic "
                "correction is disabled"
            )

    def _projection_wkt(self) -> str:
        if self.config.dry_run:
            return f"<WKT for {self.config.target_crs}>"
        command = self._force_command(
            "gdalsrsinfo",
            ["-o", "wkt1", self.config.target_crs],
        )
        self._commands.append(tuple(command))
        completed = self._run(command)
        wkt = " ".join(completed.stdout.split())
        if not wkt:
            raise RuntimeError(f"Could not resolve target CRS {self.config.target_crs}")
        return wkt

    def _raster_projection_wkt(self, path: Path) -> str:
        command = self._force_command(
            "gdalsrsinfo",
            ["-o", "wkt1", self._runtime_path(path)],
        )
        self._commands.append(tuple(command))
        completed = self._run(command)
        wkt = " ".join(completed.stdout.split())
        if not wkt:
            raise RuntimeError(f"Could not resolve the output CRS for {path}")
        return wkt

    @staticmethod
    def _normalized_wkt(value: str) -> str:
        return re.sub(r"\s+", "", value)

    def _verify_force_version(self) -> str:
        completed = self._execute_force("force-info", [])
        if completed is None:
            return FORCE_VERSION
        output = "\n".join((completed.stdout, completed.stderr))
        match = re.search(r"FORCE\s+v\.\s*([0-9]+(?:\.[0-9]+)+)", output)
        if match is None:
            raise RuntimeError("Could not determine the FORCE runtime version")
        actual = match.group(1)
        if actual != FORCE_VERSION:
            raise RuntimeError(
                f"TerraVault requires FORCE {FORCE_VERSION}, but the runtime reports {actual}"
            )
        self._actual_force_version = actual
        return actual

    @staticmethod
    def _logical(value: bool) -> str:
        return "TRUE" if value else "FALSE"

    def _parameter_values(self, projection_wkt: str) -> list[tuple[str, str]]:
        runtime = self._runtime_path
        return [
            ("FILE_QUEUE", runtime(self.queue_path)),
            ("DIR_LEVEL2", runtime(self.attempt_level2_root)),
            ("DIR_LOG", runtime(self.product_log_root)),
            ("DIR_PROVENANCE", runtime(self.product_provenance_root)),
            ("DIR_TEMP", runtime(self.temp_root)),
            ("FILE_AOI", "NULL" if self.config.aoi_path is None else runtime(self.config.aoi_path)),
            ("FILE_DEM", "NULL" if self.config.dem_path is None else runtime(self.config.dem_path)),
            ("DEM_RESAMPLING", "BL"),
            ("USE_DEM_DATABASE", "FALSE"),
            ("DEM_NODATA", str(self.config.dem_nodata)),
            ("DO_REPROJ", "TRUE"),
            ("DO_TILE", "TRUE"),
            ("FILE_TILE", "NULL"),
            ("TILE_SIZE", f"{self.config.tile_size} {self.config.tile_size}"),
            ("RESOLUTION_LANDSAT", "30"),
            ("RESOLUTION_SENTINEL2", str(self.config.resolution)),
            ("ORIGIN_LON", str(self.config.origin_lon)),
            ("ORIGIN_LAT", str(self.config.origin_lat)),
            ("PROJECTION", projection_wkt),
            ("RESAMPLING", "CC"),
            ("DO_ATMO", "TRUE"),
            ("DO_TOPO", self._logical(self.config.dem_path is not None)),
            ("DO_BRDF", "TRUE"),
            ("ADJACENCY_EFFECT", "TRUE"),
            ("MULTI_SCATTERING", "TRUE"),
            ("DIR_WVPLUT", "NULL"),
            ("STRICT_WATER_VAPOR", "FALSE"),
            ("WATER_VAPOR", "NULL"),
            ("DO_AOD", "TRUE"),
            ("DIR_AOD", "NULL"),
            ("ERASE_CLOUDS", "FALSE"),
            ("MAX_CLOUD_COVER_FRAME", str(self.config.max_cloud_cover_frame)),
            ("MAX_CLOUD_COVER_TILE", str(self.config.max_cloud_cover_tile)),
            ("CLOUD_BUFFER", str(self.config.cloud_buffer)),
            ("CIRRUS_BUFFER", str(self.config.cirrus_buffer)),
            ("SHADOW_BUFFER", str(self.config.shadow_buffer)),
            ("SNOW_BUFFER", str(self.config.snow_buffer)),
            ("CLOUD_THRESHOLD", str(self.config.cloud_threshold)),
            ("SHADOW_THRESHOLD", str(self.config.shadow_threshold)),
            ("RES_MERGE", self.config.resolution_merge),
            ("DIR_COREG_BASE", "NULL"),
            ("COREG_BASE_NODATA", "-9999"),
            ("IMPULSE_NOISE", "TRUE"),
            ("BUFFER_NODATA", "FALSE"),
            ("TIER", "1"),
            ("NPROC", str(self.config.nproc)),
            ("NTHREAD", str(self.config.nthread)),
            ("PARALLEL_READS", self._logical(self.config.parallel_reads)),
            ("DELAY", "0"),
            ("TIMEOUT_ZIP", "30"),
            ("OUTPUT_FORMAT", "COG"),
            ("FILE_OUTPUT_OPTIONS", "NULL"),
            ("OUTPUT_DST", "FALSE"),
            ("OUTPUT_AOD", "FALSE"),
            ("OUTPUT_WVP", "FALSE"),
            ("OUTPUT_VZN", "FALSE"),
            ("OUTPUT_HOT", "FALSE"),
            ("OUTPUT_OVV", self._logical(self.config.output_overview)),
        ]

    def _parameter_text(self, projection_wkt: str) -> str:
        values = self._parameter_values(projection_wkt)
        return "\n".join(
            [
                "++PARAM_LEVEL2_START++",
                *(f"{key} = {value}" for key, value in values),
                "++PARAM_LEVEL2_END++",
                "",
            ]
        )

    def _cube_config(self, projection_wkt: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "force_version": FORCE_VERSION,
            "target_crs": self.config.target_crs,
            "projection_wkt": projection_wkt,
            "origin_lon": self.config.origin_lon,
            "origin_lat": self.config.origin_lat,
            "tile_size": self.config.tile_size,
            "sentinel2_resolution": self.config.resolution,
        }

    def _ensure_cube_invariant(self, projection_wkt: str) -> None:
        requested = self._cube_config(projection_wkt)
        if self.cube_config_path.is_file():
            existing = json.loads(self.cube_config_path.read_text(encoding="utf-8"))
            if existing != requested:
                raise FileExistsError(
                    "FORCE output root already has a different immutable cube grid; "
                    f"use a new --output-root (definition: {self.cube_config_path})"
                )
            return
        _write_json_atomic(self.cube_config_path, requested)

    def _input_signature(self) -> list[dict[str, Any]]:
        path = self.config.input_path
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        signature: list[dict[str, Any]] = []
        for candidate in candidates:
            stat = candidate.stat()
            signature.append(
                {
                    "path": str(candidate.relative_to(path.parent)),
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        return signature

    @staticmethod
    def _auxiliary_files(path: Path | None) -> set[Path]:
        if path is None:
            return set()
        files = {path}
        if path.suffix.casefold() == ".shp":
            files.update(
                candidate for candidate in path.parent.glob(f"{path.stem}.*") if candidate.is_file()
            )
        if path.suffix.casefold() == ".vrt":
            tree = ET.parse(path)
            for node in tree.findall(".//SourceFilename"):
                if not node.text:
                    continue
                source = Path(node.text)
                if node.get("relativeToVRT") == "1":
                    source = path.parent / source
                files.add(source.resolve())
        return files

    def _auxiliary_signature(self) -> list[dict[str, Any]]:
        files = self._auxiliary_files(self.config.aoi_path) | self._auxiliary_files(
            self.config.dem_path
        )
        signature = []
        for path in sorted(files):
            if not path.is_file():
                raise FileNotFoundError(f"FORCE auxiliary dependency does not exist: {path}")
            stat = path.stat()
            signature.append(
                {
                    "path": str(path),
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        return signature

    def _settings(self) -> dict[str, Any]:
        return {
            "force_version": FORCE_VERSION,
            "docker_image": self.config.docker_image,
            "docker_platform": self.config.docker_platform,
            "target_crs": self.config.target_crs,
            "origin_lon": self.config.origin_lon,
            "origin_lat": self.config.origin_lat,
            "tile_size": self.config.tile_size,
            "resolution": self.config.resolution,
            "aoi_path": None if self.config.aoi_path is None else str(self.config.aoi_path),
            "dem_path": None if self.config.dem_path is None else str(self.config.dem_path),
            "dem_nodata": self.config.dem_nodata,
            "cloud_buffer": self.config.cloud_buffer,
            "cirrus_buffer": self.config.cirrus_buffer,
            "shadow_buffer": self.config.shadow_buffer,
            "snow_buffer": self.config.snow_buffer,
            "cloud_threshold": self.config.cloud_threshold,
            "shadow_threshold": self.config.shadow_threshold,
            "max_cloud_cover_frame": self.config.max_cloud_cover_frame,
            "max_cloud_cover_tile": self.config.max_cloud_cover_tile,
            "resolution_merge": self.config.resolution_merge,
            "nproc": self.config.nproc,
            "nthread": self.config.nthread,
            "parallel_reads": self.config.parallel_reads,
            "output_overview": self.config.output_overview,
        }

    def _fingerprint(
        self,
        input_signature: list[dict[str, Any]],
        auxiliary_signature: list[dict[str, Any]],
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "input": input_signature,
                    "auxiliary": auxiliary_signature,
                    "settings": self._settings(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _products(
        self,
        suffix: str,
        *,
        root: Path | None = None,
    ) -> tuple[Path, ...]:
        level2_root = self.level2_root if root is None else root
        return tuple(sorted(level2_root.glob(f"X*_Y*/{self.product_tag}_{suffix}.tif")))

    def _mosaic(self, suffix: str, *, root: Path | None = None) -> Path | None:
        level2_root = self.level2_root if root is None else root
        path = level2_root / "mosaic" / f"{self.product_tag}_{suffix}.vrt"
        return path if path.is_file() else None

    def _overviews(self, *, root: Path | None = None) -> tuple[Path, ...]:
        level2_root = self.level2_root if root is None else root
        return tuple(
            sorted(
                path
                for path in level2_root.glob(f"X*_Y*/{self.product_tag}_OVV.*")
                if path.suffix.casefold() in {".jpg", ".jpeg"}
            )
        )

    def _inspect_force_raster(self, path: Path) -> dict[str, Any]:
        completed = self._execute_force(
            "gdalinfo",
            ["-json", "-mdd", "all", self._runtime_path(path)],
        )
        assert completed is not None
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"GDAL could not inspect FORCE output: {path}") from exc

    @staticmethod
    def _force_domain(band: dict[str, Any]) -> str | None:
        metadata = band.get("metadata") or {}
        force = metadata.get("FORCE") or metadata.get("force") or {}
        value = force.get("Domain") or force.get("DOMAIN")
        return None if value is None else str(value)

    def _validate_force_raster(
        self,
        path: Path,
        suffix: str,
        *,
        chip: bool = False,
    ) -> dict[str, Any]:
        metadata = self._inspect_force_raster(path)
        bands = metadata.get("bands") or []
        if suffix == "BOA":
            expected_domains = (
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
            if len(bands) != len(expected_domains):
                raise RuntimeError(f"FORCE BOA must contain 10 bands: {path}")
            for band, domain in zip(bands, expected_domains):
                if (
                    band.get("type") != "Int16"
                    or band.get("noDataValue") != -9999
                    or str(band.get("description") or "").upper() != domain
                    or self._force_domain(band) != domain
                ):
                    raise RuntimeError(f"FORCE BOA band metadata is invalid for {domain}: {path}")
        elif suffix == "QAI":
            if (
                len(bands) != 1
                or bands[0].get("type") != "Int16"
                or bands[0].get("noDataValue") != 1
                or str(bands[0].get("description") or "").casefold()
                != "quality assurance information"
                or self._force_domain(bands[0]) != "QAI"
            ):
                raise RuntimeError(f"FORCE QAI band metadata is invalid: {path}")
        elif suffix == "OVV":
            if len(bands) != 3 or any(
                band.get("type") not in {"Byte", "Int16", "UInt16"} for band in bands
            ):
                raise RuntimeError(f"FORCE OVV must contain three image bands: {path}")
            colors = tuple(str(band.get("colorInterpretation") or "") for band in bands)
            if metadata.get("driverShortName") not in {None, "JPEG"} or colors not in {
                ("Red", "Green", "Blue"),
                ("", "", ""),  # accepted for older FORCE/GDAL combinations
            }:
                raise RuntimeError(f"FORCE OVV band metadata is invalid: {path}")
        else:
            raise ValueError(f"Unknown FORCE product suffix: {suffix}")
        size = metadata.get("size") or []
        if suffix == "OVV":
            expected_pixels = int(
                self.config.tile_size
                / (0.0015 if self.config.resolution < 1 else 150.0)
            )
            if (
                len(size) != 2
                or any(int(value) <= 0 for value in size)
                or (chip and [int(value) for value in size] != [expected_pixels, expected_pixels])
            ):
                raise RuntimeError(f"FORCE OVV chip dimensions are invalid: {path}")
            # FORCE 3.10.04 writes OVV as a plain JPEG without a world file or
            # embedded CRS. Its grid identity comes from the validated sibling
            # BOA/QAI tile directory rather than JPEG georeferencing.
            return metadata
        transform = metadata.get("geoTransform") or []
        wkt = metadata.get("coordinateSystem", {}).get("wkt")
        if (
            len(size) != 2
            or any(int(value) <= 0 for value in size)
            or len(transform) != 6
            or not wkt
        ):
            raise RuntimeError(f"FORCE {suffix} georeferencing is incomplete: {path}")
        if self._expected_projection_wkt is None:
            raise RuntimeError("Expected FORCE output projection was not initialized")
        if self._normalized_wkt(self._raster_projection_wkt(path)) != self._normalized_wkt(
            self._expected_projection_wkt
        ):
            raise RuntimeError(f"FORCE {suffix} CRS does not match the requested cube: {path}")
        resolution = self.config.resolution
        if suffix == "OVV":
            resolution = 0.0015 if self.config.resolution < 1 else 150.0
        if not (
            float(transform[1]) > 0
            and float(transform[5]) < 0
            and math.isclose(float(transform[2]), 0.0, abs_tol=1e-12)
            and math.isclose(float(transform[4]), 0.0, abs_tol=1e-12)
            and math.isclose(abs(float(transform[1])), resolution, abs_tol=1e-8)
            and math.isclose(abs(float(transform[5])), resolution, abs_tol=1e-8)
        ):
            raise RuntimeError(f"FORCE {suffix} resolution does not match the cube: {path}")
        if chip:
            expected_pixels = int(self.config.tile_size / resolution)
            if expected_pixels <= 0 or [int(value) for value in size] != [
                expected_pixels,
                expected_pixels,
            ]:
                raise RuntimeError(f"FORCE {suffix} chip dimensions are invalid: {path}")
        return metadata

    def _cube_definition(self, *, root: Path) -> dict[str, Any]:
        path = root / "datacube-definition.prj"
        if not path.is_file():
            raise RuntimeError(f"FORCE data-cube definition is missing: {path}")
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        required = {
            "PROJECTION",
            "ORIGIN_GEO_X",
            "ORIGIN_GEO_Y",
            "ORIGIN_MAP_X",
            "ORIGIN_MAP_Y",
            "TILE_SIZE_X",
            "TILE_SIZE_Y",
        }
        if not required <= values.keys():
            raise RuntimeError(f"FORCE data-cube definition is incomplete: {path}")
        if self._expected_projection_wkt is None or self._normalized_wkt(
            values["PROJECTION"]
        ) != self._normalized_wkt(self._expected_projection_wkt):
            raise RuntimeError(f"FORCE data-cube projection is invalid: {path}")
        numeric = {key: float(values[key]) for key in required - {"PROJECTION"}}
        expected_origin = {
            "ORIGIN_GEO_X": self.config.origin_lon,
            "ORIGIN_GEO_Y": self.config.origin_lat,
        }
        expected_tile_size = {
            "TILE_SIZE_X": float(self.config.tile_size),
            "TILE_SIZE_Y": float(self.config.tile_size),
        }
        if any(
            not math.isclose(numeric[key], value, abs_tol=1e-6)
            for key, value in expected_origin.items()
        ) or any(
            not math.isclose(numeric[key], value, abs_tol=1e-8)
            for key, value in expected_tile_size.items()
        ):
            raise RuntimeError(f"FORCE data-cube origin or tile size is invalid: {path}")
        return {"path": path, **numeric}

    def _verify_mosaic(self, suffix: str, *, root: Path) -> Path:
        path = self._mosaic(suffix, root=root)
        if path is None:
            raise RuntimeError(f"FORCE did not create the {suffix} mosaic")
        try:
            tree = ET.parse(path)
        except ET.ParseError as exc:
            raise RuntimeError(f"FORCE created an invalid {suffix} VRT: {path}") from exc
        sources: set[Path] = set()
        for node in tree.findall(".//SourceFilename"):
            if not node.text:
                continue
            source = Path(node.text)
            if node.get("relativeToVRT") == "1":
                source = path.parent / source
            sources.add(source.resolve())
        expected = {product.resolve() for product in self._products(suffix, root=root)}
        if not expected or sources != expected:
            raise RuntimeError(
                f"FORCE {suffix} mosaic sources do not match the complete chip set "
                f"(expected={len(expected)}, found={len(sources)}): {path}"
            )
        self._validate_force_raster(path, suffix)
        return path

    def _sanitize_mosaic_xml(self, *, root: Path) -> None:
        """Repair non-UTF-8 FORCE metadata copied into otherwise valid VRT XML."""

        for path in sorted((root / "mosaic").glob("*.vrt")):
            raw = path.read_bytes()
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")
                try:
                    ET.fromstring(text)
                except ET.ParseError as exc:
                    raise RuntimeError(
                        f"FORCE created an invalid VRT that cannot be sanitized: {path}"
                    ) from exc
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
                temporary.write_text(text, encoding="utf-8")
                os.replace(temporary, path)
                logger.warning(
                    "Replaced non-UTF-8 bytes in FORCE VRT metadata – path=%s",
                    path,
                )

    def _verify_product_files(self, *, root: Path) -> None:
        cube = self._cube_definition(root=root)
        boa = self._products("BOA", root=root)
        qai = self._products("QAI", root=root)
        if not boa or not qai:
            raise RuntimeError("Expected FORCE BOA/QAI chips are missing")
        if any(path.stat().st_size <= 0 for path in (*boa, *qai)):
            raise RuntimeError("A FORCE BOA/QAI chip is empty")
        boa_tiles = {path.parent.name for path in boa}
        qai_tiles = {path.parent.name for path in qai}
        if boa_tiles != qai_tiles:
            raise RuntimeError("FORCE BOA and QAI tile coverage does not match")
        boa_by_tile = {path.parent.name: path for path in boa}
        qai_by_tile = {path.parent.name: path for path in qai}
        for tile in sorted(boa_tiles):
            tile_match = _TILE_NAME.fullmatch(tile)
            if tile_match is None:
                raise RuntimeError(f"Invalid FORCE tile directory: {tile}")
            boa_metadata = self._validate_force_raster(
                boa_by_tile[tile],
                "BOA",
                chip=True,
            )
            qai_metadata = self._validate_force_raster(
                qai_by_tile[tile],
                "QAI",
                chip=True,
            )
            for key in ("size", "geoTransform", "coordinateSystem"):
                if boa_metadata.get(key) != qai_metadata.get(key):
                    raise RuntimeError(f"FORCE BOA/QAI grids differ for tile {tile}")
            transform = boa_metadata["geoTransform"]
            expected_x = cube["ORIGIN_MAP_X"] + int(tile_match.group("x")) * self.config.tile_size
            expected_y = cube["ORIGIN_MAP_Y"] - int(tile_match.group("y")) * self.config.tile_size
            if not (
                math.isclose(float(transform[0]), expected_x, abs_tol=1e-6)
                and math.isclose(float(transform[3]), expected_y, abs_tol=1e-6)
            ):
                raise RuntimeError(f"FORCE chip is not aligned to cube tile {tile}")
        if self.config.output_overview:
            overviews = self._overviews(root=root)
            if not overviews or any(path.stat().st_size <= 0 for path in overviews):
                raise RuntimeError("Expected FORCE OVV quicklooks are missing or empty")
            overview_tiles = {path.parent.name for path in overviews}
            if overview_tiles != boa_tiles:
                raise RuntimeError("FORCE BOA/QAI/OVV tile coverage does not match")
            for overview in overviews:
                tile = overview.parent.name
                if _TILE_NAME.fullmatch(tile) is None:
                    raise RuntimeError(f"Invalid FORCE tile directory: {tile}")
                self._validate_force_raster(overview, "OVV", chip=True)

    def _outputs_complete(self, *, root: Path) -> bool:
        try:
            self._verify_product_files(root=root)
            self._verify_mosaic("BOA", root=root)
            self._verify_mosaic("QAI", root=root)
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _prepare_attempt(self) -> None:
        """Discard any partial merge before safely retrying this SAFE."""

        if self.attempt_root.exists():
            shutil.rmtree(self.attempt_root)
        self.attempt_level2_root.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)

    @property
    def publication_backup(self) -> Path:
        return self.level2_root.with_name(f".{self.level2_root.name}.backup")

    def _recover_publication(self) -> None:
        """Recover the last published product after a process-level interruption."""

        backup = self.publication_backup
        if not backup.exists():
            return
        if self.level2_root.exists():
            shutil.rmtree(backup)
        else:
            os.replace(backup, self.level2_root)

    def _publish_attempt(self) -> None:
        """Publish a complete per-SAFE cube without touching companion granules."""

        self.level2_root.parent.mkdir(parents=True, exist_ok=True)
        backup = self.publication_backup
        if backup.exists():
            raise RuntimeError(f"Unrecovered FORCE publication backup exists: {backup}")
        replaced = False
        if self.level2_root.exists():
            os.replace(self.level2_root, backup)
            replaced = True
        try:
            os.replace(self.attempt_level2_root, self.level2_root)
        except BaseException:
            if replaced and backup.exists() and not self.level2_root.exists():
                os.replace(backup, self.level2_root)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        if self.attempt_root.exists():
            shutil.rmtree(self.attempt_root)

    def _result(self, *, status: str, skipped: bool = False) -> ForceLevel2Result:
        return ForceLevel2Result(
            status=status,
            runtime=self._select_runtime(),
            input_path=self.config.input_path,
            level2_root=self.level2_root,
            parameter_path=self.parameter_path,
            queue_path=self.queue_path,
            manifest_path=self.manifest_path,
            boa_paths=self._products("BOA"),
            qai_paths=self._products("QAI"),
            overview_paths=self._overviews(),
            boa_mosaic_path=self._mosaic("BOA"),
            qai_mosaic_path=self._mosaic("QAI"),
            commands=tuple(self._commands),
            skipped=skipped,
        )

    def _existing_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _manifest_payload(
        self,
        *,
        status: str,
        fingerprint: str,
        input_signature: list[dict[str, Any]],
        auxiliary_signature: list[dict[str, Any]],
        attempts: int,
        queue_status: str | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "started_at": self._started_at,
            "status": status,
            "error": error,
            "attempts": attempts,
            "queue_status": queue_status,
            "runtime": self._select_runtime(),
            "force_version_required": FORCE_VERSION,
            "force_version_actual": self._actual_force_version,
            "input_path": str(self.config.input_path),
            "input_signature": input_signature,
            "auxiliary_signature": auxiliary_signature,
            "fingerprint": fingerprint,
            "product_name": self.product_name,
            "product_tag": self.product_tag,
            "product_identity": self.product_identity,
            "settings": self._settings(),
            "parameter_path": str(self.parameter_path),
            "queue_path": str(self.queue_path),
            "batch_log_path": str(self.batch_log_path),
            "progress_path": str(self.progress_path),
            "container_cid_path": str(self.container_cid_path),
            "product_log_root": str(self.product_log_root),
            "product_provenance_root": str(self.product_provenance_root),
            "cube_config_path": str(self.cube_config_path),
            "pool_root": str(self.pool_root),
            "level2_root": str(self.level2_root),
            "attempt_level2_root": str(self.attempt_level2_root),
            "boa_paths": [str(path) for path in self._products("BOA")],
            "qai_paths": [str(path) for path in self._products("QAI")],
            "overview_paths": [str(path) for path in self._overviews()],
            "boa_mosaic_path": None if self._mosaic("BOA") is None else str(self._mosaic("BOA")),
            "qai_mosaic_path": None if self._mosaic("QAI") is None else str(self._mosaic("QAI")),
            "commands": [list(command) for command in self._commands],
        }

    def _write_progress(
        self,
        *,
        phase: str,
        status: str,
        error: str | None = None,
        emit_log: bool = True,
    ) -> None:
        root = self.attempt_level2_root if self.attempt_level2_root.is_dir() else self.level2_root
        boa = self._products("BOA", root=root)
        qai = self._products("QAI", root=root)
        overviews = self._overviews(root=root)
        outputs = tuple(path for path in (*boa, *qai, *overviews) if path.is_file())
        elapsed = None
        if self._started_at is not None:
            started = datetime.fromisoformat(self._started_at)
            elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        latest_mtime = max((path.stat().st_mtime for path in outputs), default=None)
        queue_status = None
        if self.queue_path.is_file():
            try:
                queue_status = self._queue_status()
            except RuntimeError:
                queue_status = "UNKNOWN"
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "started_at": self._started_at,
            "status": status,
            "phase": phase,
            "error": error,
            "job_stem": self.job_stem,
            "elapsed_seconds": elapsed,
            "queue_status": queue_status,
            "boa_tiles": len(boa),
            "qai_tiles": len(qai),
            "overview_tiles": len(overviews),
            "output_bytes": sum(path.stat().st_size for path in outputs),
            "latest_output_at": (
                None
                if latest_mtime is None
                else datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat()
            ),
            "within_scene_percent": None,
            "percent_note": "FORCE L2PS does not expose reliable within-scene percentage",
            "manifest_path": str(self.manifest_path),
            "container_id": (
                self.container_cid_path.read_text(encoding="utf-8").strip()
                if self.container_cid_path.is_file()
                else None
            ),
        }
        _write_json_atomic(self.progress_path, payload)
        if emit_log:
            logger.info(
                "FORCE progress – job=%s phase=%s elapsed=%.1fmin "
                "queue=%s BOA/QAI/OVV=%d/%d/%d bytes=%d percent=unavailable",
                self.job_stem,
                phase,
                (elapsed or 0.0) / 60,
                payload["queue_status"],
                len(boa),
                len(qai),
                len(overviews),
                payload["output_bytes"],
            )

    @contextmanager
    def _progress_heartbeat(self):
        stop = threading.Event()
        self._write_progress(phase="force-l2ps-core", status="running")

        def report() -> None:
            while not stop.wait(self.config.progress_interval_seconds):
                try:
                    self._write_progress(phase="force-l2ps-core", status="running")
                except Exception:  # noqa: BLE001
                    logger.exception("Could not update FORCE progress heartbeat")

        worker = threading.Thread(
            target=report,
            name=f"force-progress-{self.job_stem}",
            daemon=True,
        )
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=self.config.progress_interval_seconds + 1)

    def _queue_status(self) -> str:
        if not self.queue_path.is_file():
            raise RuntimeError(f"FORCE queue was not created: {self.queue_path}")
        lines = [
            line.strip()
            for line in self.queue_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(lines) != 1:
            raise RuntimeError(f"Expected one FORCE queue entry, found {len(lines)}")
        status = lines[0].rsplit(maxsplit=1)[-1]
        if status not in {"QUEUED", "DONE", "FAIL"}:
            raise RuntimeError(f"Unexpected FORCE queue status: {status}")
        return status

    def run(self) -> ForceLevel2Result:
        """Process one L1C observation and retain metadata for preflight failures."""

        try:
            return self._run_impl()
        except BaseException as exc:
            if not self.manifest_path.exists() and "holds" not in str(exc):
                try:
                    _write_json_atomic(
                        self.manifest_path,
                        {
                            "schema_version": 1,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                            "status": (
                                "interrupted"
                                if isinstance(exc, (KeyboardInterrupt, SystemExit))
                                else "failed"
                            ),
                            "phase": "preflight",
                            "error": str(exc),
                            "attempts": 0,
                            "queue_status": None,
                            "runtime_requested": self.config.runtime,
                            "input_path": str(self.config.input_path),
                            "product_name": self.product_name,
                            "product_tag": self.product_tag,
                            "product_identity": self.product_identity,
                            "settings": self._settings(),
                            "manifest_path": str(self.manifest_path),
                            "cube_config_path": str(self.cube_config_path),
                            "level2_root": str(self.level2_root),
                        },
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Could not persist FORCE preflight failure metadata")
            raise

    def _run_impl(self) -> ForceLevel2Result:
        """Process or safely reuse one complete L1C observation."""

        self._validate_input()
        self._select_runtime()
        input_signature = self._input_signature()
        auxiliary_signature = self._auxiliary_signature()
        fingerprint = self._fingerprint(input_signature, auxiliary_signature)
        existing = self._existing_manifest()
        if existing and existing.get("status") == "failed" and not self.config.retry_failed:
            raise RuntimeError(
                f"FORCE L2PS job is failed in {self.manifest_path}; pass --retry-failed"
            )
        if (
            existing
            and existing.get("fingerprint") not in {None, fingerprint}
            and not self.config.overwrite
        ):
            raise FileExistsError(
                "FORCE L2PS settings or input changed; pass --overwrite or use a new output root"
            )

        self._verify_force_version()
        projection_wkt = self._projection_wkt()
        self._expected_projection_wkt = projection_wkt
        parameter_text = self._parameter_text(projection_wkt)
        runtime_input = self._runtime_path(self.config.input_path)
        if self.config.dry_run:
            self._execute_force("force-level2", [self._runtime_path(self.parameter_path)])
            self._execute_force(
                "force-mosaic",
                [
                    "-j",
                    "1",
                    "-m",
                    self._runtime_path(self.attempt_level2_root / "mosaic"),
                    self._runtime_path(self.attempt_level2_root),
                ],
            )
            return self._result(status="planned")

        for path in (
            self.pool_root / "products",
            self.product_log_root,
            self.product_provenance_root,
            self.parameter_root,
            self.queue_root,
            self.job_root,
            self.progress_root,
            self.container_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        with _RunLock(self.lock_path):
            self._recover_publication()
            existing = self._existing_manifest()
            if existing and existing.get("status") == "failed" and not self.config.retry_failed:
                raise RuntimeError(
                    f"FORCE L2PS job is failed in {self.manifest_path}; pass --retry-failed"
                )
            if (
                existing
                and existing.get("fingerprint") not in {None, fingerprint}
                and not self.config.overwrite
            ):
                raise FileExistsError(
                    "FORCE L2PS settings or input changed; pass --overwrite or use a new "
                    "output root"
                )
            self._ensure_cube_invariant(projection_wkt)
            if (
                existing
                and existing.get("fingerprint") == fingerprint
                and not self.config.overwrite
                and self._outputs_complete(root=self.level2_root)
            ):
                if existing.get("status") != "complete":
                    logger.warning(
                        "Recovering completed FORCE publication with unfinished metadata – "
                        "product=%s",
                        self.product_name,
                    )
                    self._started_at = existing.get("started_at")
                    _write_json_atomic(
                        self.manifest_path,
                        self._manifest_payload(
                            status="complete",
                            fingerprint=fingerprint,
                            input_signature=input_signature,
                            auxiliary_signature=auxiliary_signature,
                            attempts=int(existing.get("attempts") or 1),
                            queue_status="DONE",
                        ),
                    )
                    self._write_progress(phase="complete", status="complete")
                logger.info("FORCE L2PS job already complete; skipping %s", self.product_name)
                return self._result(status="complete", skipped=True)
            resume_completed_attempt = False
            if (
                existing
                and existing.get("fingerprint") == fingerprint
                and self.attempt_level2_root.is_dir()
                and self.queue_path.is_file()
            ):
                try:
                    resume_completed_attempt = self._queue_status() == "DONE"
                except RuntimeError:
                    pass
            attempts = int((existing or {}).get("attempts") or 0)
            if not resume_completed_attempt:
                attempts += 1
            self._started_at = datetime.now(timezone.utc).isoformat()
            if resume_completed_attempt:
                logger.warning(
                    "Resuming validation/publication of completed FORCE tiles – product=%s",
                    self.product_name,
                )
            else:
                self._prepare_attempt()
            self.parameter_path.write_text(parameter_text, encoding="utf-8")
            if not resume_completed_attempt:
                self.queue_path.write_text(f"{runtime_input} QUEUED\n", encoding="utf-8")
            _write_json_atomic(
                self.manifest_path,
                self._manifest_payload(
                    status="processing",
                    fingerprint=fingerprint,
                    input_signature=input_signature,
                    auxiliary_signature=auxiliary_signature,
                    attempts=attempts,
                    queue_status="DONE" if resume_completed_attempt else "QUEUED",
                ),
            )
            try:
                if not resume_completed_attempt:
                    with self._progress_heartbeat():
                        completed = self._execute_force(
                            "force-level2",
                            [self._runtime_path(self.parameter_path)],
                            check=False,
                            log_path=self.batch_log_path,
                        )
                    assert completed is not None
                    queue_status = self._queue_status()
                    if completed.returncode or queue_status != "DONE":
                        raise RuntimeError(
                            "FORCE Level-2 child processing did not complete "
                            f"(exit={completed.returncode}, queue={queue_status}); "
                            f"inspect {self.log_root} and {self.batch_log_path}"
                        )
                self._write_progress(phase="validating-force-tiles", status="running")
                self._verify_product_files(root=self.attempt_level2_root)
                self._write_progress(phase="building-mosaics", status="running")
                self._execute_force(
                    "force-mosaic",
                    [
                        "-j",
                        "1",
                        "-m",
                        self._runtime_path(self.attempt_level2_root / "mosaic"),
                        self._runtime_path(self.attempt_level2_root),
                    ],
                )
                self._sanitize_mosaic_xml(root=self.attempt_level2_root)
                self._verify_mosaic("BOA", root=self.attempt_level2_root)
                self._verify_mosaic("QAI", root=self.attempt_level2_root)
                self._publish_attempt()
            except BaseException as exc:
                status = (
                    "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
                )
                queue_status = None
                if self.queue_path.is_file():
                    try:
                        queue_status = self._queue_status()
                    except RuntimeError:
                        pass
                _write_json_atomic(
                    self.manifest_path,
                    self._manifest_payload(
                        status=status,
                        fingerprint=fingerprint,
                        input_signature=input_signature,
                        auxiliary_signature=auxiliary_signature,
                        attempts=attempts,
                        queue_status=queue_status,
                        error=str(exc),
                    ),
                )
                self._write_progress(
                    phase=status,
                    status=status,
                    error=str(exc),
                )
                raise

            _write_json_atomic(
                self.manifest_path,
                self._manifest_payload(
                    status="complete",
                    fingerprint=fingerprint,
                    input_signature=input_signature,
                    auxiliary_signature=auxiliary_signature,
                    attempts=attempts,
                    queue_status="DONE",
                ),
            )
            self._write_progress(phase="complete", status="complete")
        logger.info(
            "FORCE native Level-2 complete – product=%s BOA=%d QAI=%d",
            self.product_name,
            len(self._products("BOA")),
            len(self._products("QAI")),
        )
        return self._result(status="complete")
