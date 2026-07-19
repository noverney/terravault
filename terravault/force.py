"""FORCE postprocessing bridge for local TerraVault raster products.

TerraVault's selected Sentinel-2 L2A assets are not FORCE Level-2 ARD. This
module uses FORCE's supported external-feature path: a local stitched raster
is imported into a regular FORCE datacube with ``force-cube`` and virtual
mosaics are generated with ``force-mosaic``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

FORCE_VERSION = "3.10.04"
FORCE_DOCKER_IMAGE = f"davidfrantz/force:{FORCE_VERSION}"
_BASENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FORCE_DTYPES = frozenset({"Byte", "Int16"})
_FORCE_DTYPE_LIMITS = {
    "Byte": (0, 255),
    "Int16": (-32_768, 32_767),
}


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _safe_default_basename(path: Path) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem).strip("._-")
    return value or "terravault-feature"


def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


@dataclass(frozen=True)
class ForceConfig:
    """Configuration for importing one local raster into a FORCE feature cube."""

    input_path: Path
    output_root: Path
    basename: str | None = None
    runtime: str = "auto"
    docker_image: str = FORCE_DOCKER_IMAGE
    mount_root: Path | None = None
    target_crs: str = "EPSG:2056"
    origin_lon: float = 5.5
    origin_lat: float = 48.0
    tile_size: int = 30_000
    resolution: float = 10.0
    resampling: str = "near"
    output_nodata: int = -9999
    output_dtype: str = "Int16"
    jobs: int = 1
    overwrite: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        input_path = _absolute(self.input_path)
        output_root = _absolute(self.output_root)
        object.__setattr__(self, "input_path", input_path)
        object.__setattr__(self, "output_root", output_root)
        basename = self.basename or _safe_default_basename(input_path)
        if not _BASENAME_PATTERN.fullmatch(basename):
            raise ValueError(
                "basename must start with a letter or digit and contain only "
                "letters, digits, '.', '_' or '-'"
            )
        object.__setattr__(self, "basename", basename)
        if self.runtime not in {"auto", "native", "docker"}:
            raise ValueError("runtime must be 'auto', 'native' or 'docker'")
        if not self.docker_image.strip():
            raise ValueError("docker_image cannot be empty")
        if not (-180 <= self.origin_lon <= 180):
            raise ValueError("origin_lon must be between -180 and 180")
        if not (-90 <= self.origin_lat <= 90):
            raise ValueError("origin_lat must be between -90 and 90")
        if self.tile_size <= 0:
            raise ValueError("tile_size must be positive")
        if self.resolution <= 0:
            raise ValueError("resolution must be positive")
        tile_pixels = self.tile_size / self.resolution
        if not math.isclose(tile_pixels, round(tile_pixels), abs_tol=1e-9):
            raise ValueError("tile_size must be an exact multiple of resolution")
        if self.output_dtype not in _FORCE_DTYPES:
            raise ValueError("FORCE external features must use Byte or Int16")
        nodata_minimum, nodata_maximum = _FORCE_DTYPE_LIMITS[self.output_dtype]
        if not nodata_minimum <= self.output_nodata <= nodata_maximum:
            raise ValueError(
                f"output_nodata must fit {self.output_dtype} "
                f"({nodata_minimum}..{nodata_maximum})"
            )
        if self.jobs < 1:
            raise ValueError("jobs must be at least 1")
        if self.mount_root is not None:
            object.__setattr__(self, "mount_root", _absolute(self.mount_root))


@dataclass(frozen=True)
class ForceResult:
    """Outcome of a FORCE feature-cube import."""

    status: str
    runtime: str
    cube_root: Path
    manifest_path: Path
    chip_paths: tuple[Path, ...]
    mosaic_path: Path | None
    commands: tuple[tuple[str, ...], ...]
    skipped: bool = False


class ForcePostprocessor:
    """Import a TerraVault raster into FORCE with durable, idempotent state."""

    def __init__(self, config: ForceConfig) -> None:
        self.config = config
        self.cube_root = config.output_root / "datacube"
        self.state_root = config.output_root / "_terravault" / "force"
        self.cube_config_path = self.state_root / "cube-config.json"
        self.manifest_path = self.state_root / "jobs" / f"{config.basename}.json"
        self._commands: list[tuple[str, ...]] = []
        self._runtime: str | None = None
        self._mount_root: Path | None = None

    @staticmethod
    def _run(
        arguments: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        logger.debug("Running command: %s", " ".join(arguments))
        completed = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.stdout.strip():
            logger.debug("Command stdout: %s", completed.stdout.strip())
        if completed.stderr.strip():
            level = logging.ERROR if completed.returncode else logging.DEBUG
            logger.log(level, "Command stderr: %s", completed.stderr.strip())
        if check and completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"Command failed with exit code {completed.returncode}: {detail}"
            )
        return completed

    def _select_runtime(self) -> str:
        if self._runtime is not None:
            return self._runtime
        if self.config.runtime == "auto":
            native = all(
                shutil.which(command)
                for command in (
                    "force-info",
                    "force-cube-init",
                    "force-cube",
                    "force-mosaic",
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
                    "force-cube-init",
                    "force-cube",
                    "force-mosaic",
                    "gdalsrsinfo",
                )
                if shutil.which(command) is None
            ]
            if missing and not self.config.dry_run:
                raise RuntimeError(
                    "Native FORCE runtime is missing: " + ", ".join(missing)
                )
        elif shutil.which("docker") is None:
            raise RuntimeError(
                "Docker is required because native FORCE commands are unavailable"
            )
        self._runtime = runtime
        return runtime

    def _docker_mount_root(self) -> Path:
        if self._mount_root is not None:
            return self._mount_root
        if self.config.mount_root is None:
            common = Path(
                os.path.commonpath(
                    [self.config.input_path, self.config.output_root]
                )
            )
        else:
            common = self.config.mount_root
        if common == Path(common.anchor):
            raise ValueError(
                "Docker input and output must share a safely scoped parent directory; "
                "set mount_root explicitly"
            )
        for path in (self.config.input_path, self.config.output_root):
            try:
                path.relative_to(common)
            except ValueError as exc:
                raise ValueError(f"{path} is outside Docker mount root {common}") from exc
        self._mount_root = common
        return common

    def _runtime_path(self, path: Path) -> str:
        if self._select_runtime() == "native":
            return str(path)
        relative = path.relative_to(self._docker_mount_root())
        return str(Path("/data") / relative)

    def _force_command(self, executable: str, arguments: Sequence[str]) -> list[str]:
        if self._select_runtime() == "native":
            return [executable, *arguments]
        mount_root = self._docker_mount_root()
        command = [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{mount_root}:/data",
            "--env",
            "HOME=/tmp",
            "--env",
            "PARALLEL_HOME=/tmp/.parallel",
            "--env",
            "SHELL=/bin/bash",
        ]
        if hasattr(os, "getuid") and hasattr(os, "getgid"):
            command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        command.extend([self.config.docker_image, executable, *arguments])
        return command

    def _execute_force(
        self,
        executable: str,
        arguments: Sequence[str],
    ) -> subprocess.CompletedProcess[str] | None:
        command = self._force_command(executable, arguments)
        self._commands.append(tuple(command))
        logger.info("FORCE step – executable=%s", executable)
        if self.config.dry_run:
            return None
        return self._run(command)

    @staticmethod
    def _command_detail(completed: subprocess.CompletedProcess[str] | None) -> str:
        if completed is None:
            return ""
        output = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part and part.strip()
        )
        return f": {output}" if output else ""

    def _input_metadata(self) -> dict[str, Any]:
        if not self.config.input_path.is_file():
            raise FileNotFoundError(f"Input raster does not exist: {self.config.input_path}")
        gdalinfo = shutil.which("gdalinfo")
        if gdalinfo is None:
            raise RuntimeError("gdalinfo is required to validate the FORCE input")
        completed = self._run([gdalinfo, "-json", str(self.config.input_path)])
        metadata = json.loads(completed.stdout)
        bands = metadata.get("bands") or []
        if not bands:
            raise ValueError("FORCE input has no raster bands")
        missing_nodata = [
            index
            for index, band in enumerate(bands, start=1)
            if band.get("noDataValue") is None
        ]
        if missing_nodata:
            raise ValueError(
                "force-cube requires input nodata on every band; missing for band(s): "
                + ", ".join(str(index) for index in missing_nodata)
            )
        nodata_values = {band["noDataValue"] for band in bands}
        if len(nodata_values) != 1:
            raise ValueError(
                "force-cube accepts one source nodata value; every input band "
                "must use the same nodata"
            )
        if not metadata.get("coordinateSystem", {}).get("wkt"):
            raise ValueError("FORCE input is not georeferenced")
        return metadata

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

    def _cube_settings(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "force_version": FORCE_VERSION,
            "docker_image": self.config.docker_image,
            "target_crs": self.config.target_crs,
            "origin_lon": self.config.origin_lon,
            "origin_lat": self.config.origin_lat,
            "tile_size": self.config.tile_size,
            "resolution": self.config.resolution,
            "output_dtype": self.config.output_dtype,
            "output_nodata": self.config.output_nodata,
        }

    def _ensure_cube(self) -> None:
        requested = self._cube_settings()
        if self.cube_config_path.is_file():
            existing = json.loads(self.cube_config_path.read_text(encoding="utf-8"))
            if existing != requested:
                raise ValueError(
                    f"FORCE cube settings differ from {self.cube_config_path}; "
                    "use a different output root for a different grid"
                )
        elif not self.config.dry_run:
            _write_json_atomic(self.cube_config_path, requested)

        definition = self.cube_root / "datacube-definition.prj"
        if definition.is_file():
            lines = [
                line.strip()
                for line in definition.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            required = {
                "PROJECTION",
                "ORIGIN_GEO_X",
                "ORIGIN_GEO_Y",
                "ORIGIN_MAP_X",
                "ORIGIN_MAP_Y",
                "TILE_SIZE_X",
                "TILE_SIZE_Y",
            }
            tags = {
                line.split("=", 1)[0].strip()
                for line in lines
                if "=" in line
            }
            valid = required.issubset(tags) and all("=" in line for line in lines)
            if valid:
                return
            if self._chip_paths():
                raise ValueError(
                    f"Malformed FORCE cube definition has existing chips: {definition}"
                )
            logger.warning(
                "Replacing malformed FORCE cube definition with normalized one-line WKT "
                "– path=%s",
                definition,
            )
            definition.unlink()
        if not self.config.dry_run:
            self.cube_root.mkdir(parents=True, exist_ok=True)
        wkt = self._projection_wkt()
        self._execute_force(
            "force-cube-init",
            [
                "-d",
                self._runtime_path(self.cube_root),
                "-o",
                f"{self.config.origin_lon},{self.config.origin_lat}",
                "-t",
                f"{self.config.tile_size},{self.config.tile_size}",
                wkt,
            ],
        )
        if not self.config.dry_run and not definition.is_file():
            raise RuntimeError("FORCE did not create datacube-definition.prj")

    def _existing_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _job_fingerprint(self, input_sha256: str) -> str:
        payload = {
            "input_sha256": input_sha256,
            "basename": self.config.basename,
            "cube": self._cube_settings(),
            "resampling": self.config.resampling,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _chip_paths(self) -> tuple[Path, ...]:
        return tuple(
            sorted(self.cube_root.glob(f"X*_Y*/{self.config.basename}.tif"))
        )

    def _mosaic_path(self) -> Path | None:
        path = self.cube_root / "mosaic" / f"{self.config.basename}.vrt"
        return path if path.is_file() else None

    def _manifest_payload(
        self,
        *,
        status: str,
        input_sha256: str | None,
        fingerprint: str | None,
        attempts: int,
        error: str | None = None,
        force_info: str | None = None,
    ) -> dict[str, Any]:
        chips = self._chip_paths()
        mosaic = self._mosaic_path()
        return {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "attempts": attempts,
            "error": error,
            "runtime": self._select_runtime(),
            "force_version": FORCE_VERSION,
            "force_info": force_info,
            "docker_image": self.config.docker_image,
            "input_path": str(self.config.input_path),
            "input_bytes": (
                self.config.input_path.stat().st_size
                if self.config.input_path.is_file()
                else None
            ),
            "input_mtime_ns": (
                self.config.input_path.stat().st_mtime_ns
                if self.config.input_path.is_file()
                else None
            ),
            "input_sha256": input_sha256,
            "fingerprint": fingerprint,
            "basename": self.config.basename,
            "cube_root": str(self.cube_root),
            "cube_settings": self._cube_settings(),
            "resampling": self.config.resampling,
            "chip_paths": [str(path) for path in chips],
            "mosaic_path": None if mosaic is None else str(mosaic),
            "commands": [list(command) for command in self._commands],
        }

    def run(self) -> ForceResult:
        """Validate, cube and mosaic one local raster."""

        metadata = self._input_metadata()
        runtime = self._select_runtime()
        logger.info(
            "FORCE input validated – path=%s size=%sx%s bands=%d runtime=%s",
            self.config.input_path,
            metadata["size"][0],
            metadata["size"][1],
            len(metadata["bands"]),
            runtime,
        )
        self._ensure_cube()
        if self.config.dry_run:
            self._execute_force(
                "force-cube",
                [
                    "-r",
                    self.config.resampling,
                    "-s",
                    str(self.config.resolution),
                    "-n",
                    str(self.config.output_nodata),
                    "-t",
                    self.config.output_dtype,
                    "-o",
                    self._runtime_path(self.cube_root),
                    "-b",
                    str(self.config.basename),
                    "-j",
                    str(self.config.jobs),
                    self._runtime_path(self.config.input_path),
                ],
            )
            self._execute_force(
                "force-mosaic",
                [
                    "-j",
                    str(self.config.jobs),
                    "-m",
                    self._runtime_path(self.cube_root / "mosaic"),
                    self._runtime_path(self.cube_root),
                ],
            )
            return ForceResult(
                status="planned",
                runtime=runtime,
                cube_root=self.cube_root,
                manifest_path=self.manifest_path,
                chip_paths=(),
                mosaic_path=None,
                commands=tuple(self._commands),
            )

        existing = self._existing_manifest()
        input_stat = self.config.input_path.stat()
        if (
            existing
            and existing.get("status") == "complete"
            and existing.get("input_path") == str(self.config.input_path)
            and existing.get("input_bytes") == input_stat.st_size
            and existing.get("input_mtime_ns") == input_stat.st_mtime_ns
            and existing.get("cube_settings") == self._cube_settings()
            and existing.get("resampling") == self.config.resampling
            and self._chip_paths()
            and not self.config.overwrite
        ):
            logger.info(
                "FORCE job already complete; skipping unchanged input – manifest=%s",
                self.manifest_path,
            )
            return ForceResult(
                status="complete",
                runtime=runtime,
                cube_root=self.cube_root,
                manifest_path=self.manifest_path,
                chip_paths=self._chip_paths(),
                mosaic_path=self._mosaic_path(),
                commands=tuple(self._commands),
                skipped=True,
            )

        input_sha256 = _sha256(self.config.input_path)
        fingerprint = self._job_fingerprint(input_sha256)
        if (
            existing
            and existing.get("status") == "complete"
            and existing.get("fingerprint") == fingerprint
            and self._chip_paths()
            and not self.config.overwrite
        ):
            logger.info(
                "FORCE job already complete; skipping identical input – manifest=%s",
                self.manifest_path,
            )
            return ForceResult(
                status="complete",
                runtime=runtime,
                cube_root=self.cube_root,
                manifest_path=self.manifest_path,
                chip_paths=self._chip_paths(),
                mosaic_path=self._mosaic_path(),
                commands=tuple(self._commands),
                skipped=True,
            )
        if (
            existing
            and existing.get("status") == "complete"
            and existing.get("fingerprint") != fingerprint
            and not self.config.overwrite
        ):
            raise FileExistsError(
                f"FORCE basename {self.config.basename!r} already belongs to a "
                "different input; choose another basename or pass overwrite"
            )

        attempts = int((existing or {}).get("attempts") or 0) + 1
        if self.config.overwrite:
            for path in self._chip_paths():
                path.unlink()
            mosaic = self._mosaic_path()
            if mosaic is not None:
                mosaic.unlink()
        running = self._manifest_payload(
            status="running",
            input_sha256=input_sha256,
            fingerprint=fingerprint,
            attempts=attempts,
        )
        _write_json_atomic(self.manifest_path, running)
        force_info: str | None = None
        try:
            info_result = self._execute_force("force-info", [])
            force_info = None if info_result is None else info_result.stdout.strip()
            cube_result = self._execute_force(
                "force-cube",
                [
                    "-r",
                    self.config.resampling,
                    "-s",
                    str(self.config.resolution),
                    "-n",
                    str(self.config.output_nodata),
                    "-t",
                    self.config.output_dtype,
                    "-o",
                    self._runtime_path(self.cube_root),
                    "-b",
                    str(self.config.basename),
                    "-j",
                    str(self.config.jobs),
                    self._runtime_path(self.config.input_path),
                ],
            )
            chips = self._chip_paths()
            if not chips:
                raise RuntimeError(
                    "force-cube completed but produced no raster chips"
                    + self._command_detail(cube_result)
                )
            mosaic_result = self._execute_force(
                "force-mosaic",
                [
                    "-j",
                    str(self.config.jobs),
                    "-m",
                    self._runtime_path(self.cube_root / "mosaic"),
                    self._runtime_path(self.cube_root),
                ],
            )
            mosaic = self._mosaic_path()
            if mosaic is None:
                raise RuntimeError(
                    "force-mosaic completed but produced no VRT"
                    + self._command_detail(mosaic_result)
                )
            complete = self._manifest_payload(
                status="complete",
                input_sha256=input_sha256,
                fingerprint=fingerprint,
                attempts=attempts,
                force_info=force_info,
            )
            _write_json_atomic(self.manifest_path, complete)
        except Exception as exc:
            failed = self._manifest_payload(
                status="failed",
                input_sha256=input_sha256,
                fingerprint=fingerprint,
                attempts=attempts,
                error=f"{type(exc).__name__}: {exc}",
                force_info=force_info,
            )
            _write_json_atomic(self.manifest_path, failed)
            raise

        logger.info(
            "FORCE postprocessing complete – input=%s chips=%d mosaic=%s",
            self.config.input_path,
            len(chips),
            mosaic,
        )
        return ForceResult(
            status="complete",
            runtime=runtime,
            cube_root=self.cube_root,
            manifest_path=self.manifest_path,
            chip_paths=chips,
            mosaic_path=mosaic,
            commands=tuple(self._commands),
        )
