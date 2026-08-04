"""Resumable acquisition of complete Sentinel-2 Level-1C SAFE products.

FORCE L2PS needs the complete SAFE hierarchy rather than selected raster
assets.  CDSE STAC items expose either an authenticated ``Product`` ZIP or
individual files below an S3 SAFE prefix.  This module supports both forms and
keeps durable state outside the immutable product directory.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import requests

from .auth import CDSEDownloadAuthConfig, build_cdse_session_factory
from .s3_downloader import (
    DownloadInterrupted,
    QuotaExceededError,
    S3Config,
    S3Downloader,
    _retry_after_seconds,
    quota_error_from_exception,
    sha256_file,
    split_s3_uri,
)

logger = logging.getLogger(__name__)

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
_SAFE_ITEM_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class _DownloadLock:
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
            raise RuntimeError(f"Another L1C download holds {self.path}") from exc
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write(f"pid={os.getpid()}\n")
        self.stream.flush()
        return self

    def __exit__(self, *_args: object) -> None:
        if self.stream is not None:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class L1CDownloadConfig:
    """Download settings for one complete L1C product described by STAC."""

    item_path: Path
    output_root: Path
    s3: S3Config | None = None
    auth: CDSEDownloadAuthConfig | None = None
    chunk_size: int = 8 * 1024 * 1024
    max_retries: int = 5
    retry_base_seconds: float = 2.0
    quota_wait_seconds: float = 900.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_path", _absolute(self.item_path))
        object.__setattr__(self, "output_root", _absolute(self.output_root))
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self.max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if self.retry_base_seconds < 0:
            raise ValueError("retry_base_seconds cannot be negative")
        if self.quota_wait_seconds < 0:
            raise ValueError("quota_wait_seconds cannot be negative")


@dataclass(frozen=True)
class L1CDownloadResult:
    """Outcome of a complete SAFE product transfer."""

    status: str
    item_id: str
    product_name: str
    product_path: Path
    manifest_path: Path
    source_mode: str
    file_count: int
    byte_count: int
    skipped: bool = False


class L1CProductDownloader:
    """Download a full L1C SAFE tree or product ZIP with restart support."""

    def __init__(
        self,
        config: L1CDownloadConfig,
        *,
        s3_client: Any | None = None,
        session_factory: Callable[[], requests.Session] | None = None,
    ) -> None:
        self.config = config
        self.item = self._load_item()
        self.item_id = str(self.item.get("id") or "")
        if not self.item_id:
            raise ValueError("STAC item metadata has no id")
        if _SAFE_ITEM_ID.fullmatch(self.item_id) is None:
            raise ValueError(f"STAC item id contains unsafe filename characters: {self.item_id!r}")
        self.product_name = self._product_name()
        if "_MSIL1C_" not in self.product_name:
            raise ValueError(
                f"FORCE L2PS requires a complete Sentinel-2 MSIL1C product; got {self.product_name}"
            )
        self.level1_root = config.output_root / "level1"
        self.state_root = config.output_root / "_terravault" / "force-l2" / "downloads"
        self.manifest_path = self.state_root / f"{self.item_id}.json"
        self.s3_manifest_path = self.state_root / f"{self.item_id}.s3.json"
        self.lock_path = self.state_root / f"{self.product_name}.lock"
        self._s3_client = s3_client
        self._session_factory = session_factory

    def _load_item(self) -> dict[str, Any]:
        if not self.config.item_path.is_file():
            raise FileNotFoundError(f"STAC item metadata does not exist: {self.config.item_path}")
        payload = json.loads(self.config.item_path.read_text(encoding="utf-8"))
        if payload.get("type") == "FeatureCollection":
            features = payload.get("features") or []
            if len(features) != 1:
                raise ValueError("STAC FeatureCollection must contain exactly one item")
            payload = features[0]
        if not isinstance(payload, dict) or payload.get("type") != "Feature":
            raise ValueError("Expected a STAC Item GeoJSON Feature")
        return payload

    def _product_name(self) -> str:
        private = (self.item.get("properties") or {}).get("_private") or {}
        name = private.get("product_name")
        if not name:
            product = (self.item.get("assets") or {}).get("Product") or {}
            name = Path(str(product.get("file:local_path") or "")).name
            if name.endswith(".zip"):
                name = name[:-4]
        name = str(name or "").strip()
        if not name.endswith(".SAFE") or Path(name).name != name:
            raise ValueError(f"Invalid or missing SAFE product name: {name!r}")
        return name

    def _safe_manifest_uri(self) -> str | None:
        asset = (self.item.get("assets") or {}).get("safe_manifest") or {}
        href = str(asset.get("href") or "")
        return href if href.startswith("s3://") else None

    def _product_asset(self) -> dict[str, Any] | None:
        asset = (self.item.get("assets") or {}).get("Product")
        return asset if isinstance(asset, dict) and asset.get("href") else None

    @staticmethod
    def _advertised_sha256(asset: dict[str, Any]) -> str | None:
        value = str(asset.get("file:checksum") or "").strip().lower()
        if not value:
            return None
        if value.startswith("sha256:"):
            value = value.removeprefix("sha256:")
        elif len(value) == 68 and value.startswith("1220"):
            value = value[4:]
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(
                "Unsupported Product file:checksum; expected SHA-256 hex or "
                "the 0x12/0x20 multihash form"
            )
        return value

    def _existing_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _existing_s3_manifest(self) -> dict[str, Any] | None:
        """Load the S3 checkpoint even if an HTTP fallback replaced main state."""

        current = self._existing_manifest()
        if current and current.get("source_mode") == "s3-tree":
            return current
        if self.s3_manifest_path.is_file():
            return json.loads(self.s3_manifest_path.read_text(encoding="utf-8"))
        return None

    def _write_s3_state(self, payload: dict[str, Any]) -> None:
        """Persist both user-facing state and the route-specific resume checkpoint."""

        _write_json_atomic(self.s3_manifest_path, payload)
        _write_json_atomic(self.manifest_path, payload)

    @staticmethod
    def _object_fingerprint(objects: list[dict[str, Any]]) -> str:
        stable = [
            {
                "key": str(entry["Key"]),
                "size": int(entry["Size"]),
                "etag": str(entry.get("ETag") or ""),
            }
            for entry in objects
        ]
        return hashlib.sha256(
            json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _validate_safe_members(members: list[str]) -> None:
        """Require product/granule metadata and every Sentinel-2 L1C band."""

        normalized = {
            PurePosixPath(member).as_posix().lstrip("/")
            for member in members
            if member and not member.endswith("/")
        }
        missing_product = {
            marker for marker in ("manifest.safe", "MTD_MSIL1C.xml") if marker not in normalized
        }
        granules: set[str] = set()
        metadata: set[str] = set()
        bands: dict[str, set[str]] = {}
        for member in normalized:
            parts = PurePosixPath(member).parts
            if len(parts) < 3 or parts[0] != "GRANULE" or not parts[1].startswith("L1C_"):
                continue
            granule = parts[1]
            granules.add(granule)
            if len(parts) == 3 and parts[2] == "MTD_TL.xml":
                metadata.add(granule)
            elif len(parts) >= 4 and parts[2] == "IMG_DATA":
                match = _L1C_BAND_NAME.search(parts[-1])
                if match is not None:
                    bands.setdefault(granule, set()).add(match.group(1))
        incomplete = [
            granule
            for granule in sorted(granules)
            if granule not in metadata or bands.get(granule, set()) != _L1C_BANDS
        ]
        if missing_product or not granules or incomplete:
            detail = []
            if missing_product:
                detail.append("missing " + ", ".join(sorted(missing_product)))
            if not granules:
                detail.append("no L1C granule")
            if incomplete:
                detail.append("incomplete granules: " + ", ".join(incomplete))
            raise RuntimeError(
                "Complete Sentinel-2 L1C SAFE validation failed (" + "; ".join(detail) + ")"
            )

    def _validate_safe_zip(self, path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                prefix = f"{self.product_name}/"
                members = [
                    name.removeprefix(prefix)
                    for name in archive.namelist()
                    if name.startswith(prefix)
                ]
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"Downloaded Product is not a valid ZIP: {path}") from exc
        self._validate_safe_members(members)

    def _list_s3_objects(self, uri: str) -> tuple[str, str, list[dict[str, Any]]]:
        if self.config.s3 is None:
            raise ValueError("CDSE S3 credentials are required for SAFE tree download")
        bucket, manifest_key = split_s3_uri(uri)
        prefix = manifest_key.rsplit("/", 1)[0] + "/"
        client = self._s3_client or S3Downloader(self.config.s3).client
        objects: list[dict[str, Any]] = []
        continuation: str | None = None
        while True:
            arguments: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation:
                arguments["ContinuationToken"] = continuation
            try:
                response = client.list_objects_v2(**arguments)
            except Exception as exc:  # noqa: BLE001
                quota_error = quota_error_from_exception(exc)
                if quota_error is not None:
                    raise quota_error from exc
                raise
            objects.extend(
                entry for entry in response.get("Contents") or [] if int(entry.get("Size") or 0) > 0
            )
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
            if not continuation:
                raise RuntimeError("S3 listing was truncated without a continuation token")
        objects.sort(key=lambda entry: str(entry["Key"]))
        if not objects or not any(
            str(entry["Key"]).endswith("/manifest.safe") for entry in objects
        ):
            raise RuntimeError(f"S3 SAFE prefix is empty or incomplete: s3://{bucket}/{prefix}")
        self._validate_safe_members([str(entry["Key"]).removeprefix(prefix) for entry in objects])
        return bucket, prefix, objects

    @staticmethod
    def _safe_relative_path(key: str, prefix: str) -> Path:
        if not key.startswith(prefix):
            raise ValueError(f"S3 object is outside the SAFE prefix: {key}")
        relative = PurePosixPath(key.removeprefix(prefix))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError(f"Unsafe SAFE object key: {key}")
        return Path(*relative.parts)

    @staticmethod
    def _safe_destination(product_root: Path, relative: Path) -> Path:
        root = product_root.resolve()
        destination = (product_root / relative).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"SAFE object destination escapes the product root: {relative}"
            ) from exc
        return destination

    def _state_payload(
        self,
        *,
        status: str,
        source_mode: str,
        product_path: Path,
        fingerprint: str,
        expected_files: int,
        expected_bytes: int,
        completed_files: int,
        completed_bytes: int,
        error: str | None = None,
        staging_path: Path | None = None,
        file_sha256: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "error": error,
            "item_id": self.item_id,
            "item_path": str(self.config.item_path),
            "product_name": self.product_name,
            "product_path": str(product_path),
            "source_mode": source_mode,
            "fingerprint": fingerprint,
            "expected_files": expected_files,
            "expected_bytes": expected_bytes,
            "completed_files": completed_files,
            "completed_bytes": completed_bytes,
        }
        if staging_path is not None:
            payload["staging_path"] = str(staging_path)
        if file_sha256 is not None:
            payload["file_sha256"] = dict(sorted(file_sha256.items()))
        return payload

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    @classmethod
    def _publish_safe_tree(cls, staging_path: Path, product_path: Path) -> None:
        """Replace one SAFE atomically while retaining a crash-recovery backup."""

        backup = product_path.with_name(f".{product_path.name}.backup")
        if backup.exists():
            if product_path.exists():
                cls._remove_path(backup)
            else:
                os.replace(backup, product_path)
        replaced = False
        if product_path.exists():
            os.replace(product_path, backup)
            replaced = True
        try:
            os.replace(staging_path, product_path)
        except BaseException:
            if replaced and backup.exists() and not product_path.exists():
                os.replace(backup, product_path)
            raise
        if backup.exists():
            cls._remove_path(backup)

    def _download_s3_tree(
        self,
        uri: str,
        *,
        stop_requested: Callable[[], bool],
    ) -> L1CDownloadResult:
        assert self.config.s3 is not None
        product_path = self._safe_destination(self.level1_root, Path(self.product_name))
        staging_path = self._safe_destination(
            self.level1_root,
            Path(f".{self.product_name}.partial"),
        )
        backup_path = self._safe_destination(
            self.level1_root,
            Path(f".{self.product_name}.backup"),
        )
        self.level1_root.mkdir(parents=True, exist_ok=True)
        if backup_path.exists():
            if product_path.exists():
                self._remove_path(backup_path)
            else:
                os.replace(backup_path, product_path)
        existing = self._existing_s3_manifest()
        prior_fingerprint = str((existing or {}).get("fingerprint") or "")
        prior_hashes = {
            str(key): str(value)
            for key, value in ((existing or {}).get("file_sha256") or {}).items()
        }
        self._write_s3_state(
            self._state_payload(
                status="listing",
                source_mode="s3-tree",
                product_path=product_path,
                fingerprint=prior_fingerprint,
                expected_files=0,
                expected_bytes=0,
                completed_files=0,
                completed_bytes=0,
                staging_path=staging_path,
                file_sha256=prior_hashes,
            )
        )
        try:
            bucket, prefix, objects = self._list_s3_objects(uri)
        except BaseException as exc:
            self._write_s3_state(
                self._state_payload(
                    status=(
                        "interrupted"
                        if isinstance(exc, (KeyboardInterrupt, SystemExit))
                        else "failed"
                    ),
                    source_mode="s3-tree",
                    product_path=product_path,
                    fingerprint=prior_fingerprint,
                    expected_files=0,
                    expected_bytes=0,
                    completed_files=0,
                    completed_bytes=0,
                    error=str(exc),
                    staging_path=staging_path,
                    file_sha256=prior_hashes,
                )
            )
            raise
        fingerprint = self._object_fingerprint(objects)
        expected_bytes = sum(int(entry["Size"]) for entry in objects)
        object_paths = [
            (
                self._safe_relative_path(str(entry["Key"]), prefix),
                int(entry["Size"]),
            )
            for entry in objects
        ]
        expected_relative = {relative.as_posix() for relative, _size in object_paths}
        if product_path.is_dir() and existing and existing.get("fingerprint") == fingerprint:
            actual_relative = {
                path.relative_to(product_path).as_posix()
                for path in product_path.rglob("*")
                if path.is_file()
            }
            size_complete = actual_relative == expected_relative and all(
                self._safe_destination(product_path, relative).stat().st_size == size
                for relative, size in object_paths
            )
            if size_complete:
                actual_hashes = {
                    relative.as_posix(): sha256_file(
                        self._safe_destination(product_path, relative),
                        self.config.chunk_size,
                    )
                    for relative, _size in object_paths
                }
                hashes_complete = bool(prior_hashes) and (
                    prior_hashes.keys() == actual_hashes.keys()
                    and all(prior_hashes[key] == digest for key, digest in actual_hashes.items())
                )
                if hashes_complete:
                    payload = self._state_payload(
                        status="complete",
                        source_mode="s3-tree",
                        product_path=product_path,
                        fingerprint=fingerprint,
                        expected_files=len(objects),
                        expected_bytes=expected_bytes,
                        completed_files=len(objects),
                        completed_bytes=expected_bytes,
                        staging_path=staging_path,
                        file_sha256=actual_hashes,
                    )
                    self._write_s3_state(payload)
                    return L1CDownloadResult(
                        status="complete",
                        item_id=self.item_id,
                        product_name=self.product_name,
                        product_path=product_path,
                        manifest_path=self.manifest_path,
                        source_mode="s3-tree",
                        file_count=len(objects),
                        byte_count=expected_bytes,
                        skipped=True,
                    )
                logger.warning(
                    "Published L1C SAFE content hash changed; rebuilding atomically – item=%s",
                    self.item_id,
                )

        if staging_path.exists() and (
            existing is None or existing.get("fingerprint") != fingerprint
        ):
            self._remove_path(staging_path)
        staging_path.mkdir(parents=True, exist_ok=True)
        downloader = S3Downloader(self.config.s3, client=self._s3_client)
        completed_files = 0
        completed_bytes = 0
        file_hashes: dict[str, str] = {}
        try:
            for index, (entry, (relative, _size)) in enumerate(
                zip(objects, object_paths),
                start=1,
            ):
                if stop_requested():
                    raise DownloadInterrupted("Stop requested before the next SAFE object")
                key = str(entry["Key"])
                destination = self._safe_destination(
                    staging_path,
                    relative,
                )
                logger.info(
                    "Downloading L1C SAFE object – item=%s file=%d/%d path=%s",
                    self.item_id,
                    index,
                    len(objects),
                    destination,
                )
                result = downloader.download(
                    f"s3://{bucket}/{key}",
                    destination,
                    stop_requested=stop_requested,
                )
                prior_digest = prior_hashes.get(relative.as_posix())
                if result.skipped and result.sha256 != prior_digest:
                    logger.warning(
                        "Resumed L1C object has no matching durable hash; "
                        "downloading it again – item=%s path=%s",
                        self.item_id,
                        destination,
                    )
                    destination.unlink()
                    result = downloader.download(
                        f"s3://{bucket}/{key}",
                        destination,
                        stop_requested=stop_requested,
                    )
                completed_files += 1
                completed_bytes += result.byte_count
                file_hashes[relative.as_posix()] = result.sha256
                self._write_s3_state(
                    self._state_payload(
                        status="downloading",
                        source_mode="s3-tree",
                        product_path=product_path,
                        fingerprint=fingerprint,
                        expected_files=len(objects),
                        expected_bytes=expected_bytes,
                        completed_files=completed_files,
                        completed_bytes=completed_bytes,
                        staging_path=staging_path,
                        file_sha256=file_hashes,
                    )
                )
            actual_relative = {
                path.relative_to(staging_path).as_posix()
                for path in staging_path.rglob("*")
                if path.is_file()
            }
            if actual_relative != expected_relative:
                raise RuntimeError("Staged L1C SAFE contents do not match the S3 listing")
            self._validate_safe_members(sorted(actual_relative))
            self._publish_safe_tree(staging_path, product_path)
        except BaseException as exc:
            self._write_s3_state(
                self._state_payload(
                    status=(
                        "interrupted"
                        if isinstance(
                            exc,
                            (DownloadInterrupted, KeyboardInterrupt, SystemExit),
                        )
                        else "failed"
                    ),
                    source_mode="s3-tree",
                    product_path=product_path,
                    fingerprint=fingerprint,
                    expected_files=len(objects),
                    expected_bytes=expected_bytes,
                    completed_files=completed_files,
                    completed_bytes=completed_bytes,
                    error=str(exc),
                    staging_path=staging_path,
                    file_sha256=file_hashes,
                )
            )
            raise

        self._write_s3_state(
            self._state_payload(
                status="complete",
                source_mode="s3-tree",
                product_path=product_path,
                fingerprint=fingerprint,
                expected_files=len(objects),
                expected_bytes=expected_bytes,
                completed_files=completed_files,
                completed_bytes=completed_bytes,
                staging_path=staging_path,
                file_sha256=file_hashes,
            )
        )
        return L1CDownloadResult(
            status="complete",
            item_id=self.item_id,
            product_name=self.product_name,
            product_path=product_path,
            manifest_path=self.manifest_path,
            source_mode="s3-tree",
            file_count=len(objects),
            byte_count=completed_bytes,
        )

    def _http_session(self) -> requests.Session:
        if self._session_factory is not None:
            return self._session_factory()
        if self.config.auth is None:
            raise ValueError("CDSE download authentication is required for Product ZIP download")
        return build_cdse_session_factory(self.config.auth)()

    def _download_http_product(
        self,
        asset: dict[str, Any],
        *,
        stop_requested: Callable[[], bool],
    ) -> L1CDownloadResult:
        url = str(asset["href"])
        expected_bytes = int(asset.get("file:size") or 0)
        expected_sha256 = self._advertised_sha256(asset)
        product_path = self._safe_destination(
            self.level1_root,
            Path(f"{self.product_name}.zip"),
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "url": url,
                    "size": expected_bytes,
                    "sha256": expected_sha256,
                    "item": self.item_id,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        existing = self._existing_manifest()
        product_stat = product_path.stat() if product_path.is_file() else None
        if (
            product_stat is not None
            and (expected_bytes <= 0 or product_stat.st_size == expected_bytes)
            and existing
            and existing.get("status") == "complete"
            and existing.get("fingerprint") == fingerprint
            and existing.get("sha256")
            and existing.get("product_mtime_ns") == product_stat.st_mtime_ns
            and (expected_sha256 is None or existing.get("sha256") == expected_sha256)
        ):
            return L1CDownloadResult(
                status="complete",
                item_id=self.item_id,
                product_name=self.product_name,
                product_path=product_path,
                manifest_path=self.manifest_path,
                source_mode="odata-zip",
                file_count=1,
                byte_count=product_stat.st_size,
                skipped=True,
            )

        self.level1_root.mkdir(parents=True, exist_ok=True)
        partial = product_path.with_name(f"{product_path.name}.part")
        if product_path.is_file():
            try:
                self._validate_safe_zip(product_path)
                digest = sha256_file(product_path, self.config.chunk_size)
                if expected_sha256 is not None and digest != expected_sha256:
                    raise OSError("Existing Product ZIP checksum does not match STAC")
            except (OSError, RuntimeError):
                product_path.unlink(missing_ok=True)
            else:
                stat = product_path.stat()
                payload = self._state_payload(
                    status="complete",
                    source_mode="odata-zip",
                    product_path=product_path,
                    fingerprint=fingerprint,
                    expected_files=1,
                    expected_bytes=expected_bytes or stat.st_size,
                    completed_files=1,
                    completed_bytes=stat.st_size,
                )
                payload.update(
                    {
                        "advertised_sha256": expected_sha256,
                        "sha256": digest,
                        "product_mtime_ns": stat.st_mtime_ns,
                    }
                )
                _write_json_atomic(self.manifest_path, payload)
                return L1CDownloadResult(
                    status="complete",
                    item_id=self.item_id,
                    product_name=self.product_name,
                    product_path=product_path,
                    manifest_path=self.manifest_path,
                    source_mode="odata-zip",
                    file_count=1,
                    byte_count=stat.st_size,
                    skipped=True,
                )
        if partial.exists() and (existing is None or existing.get("fingerprint") != fingerprint):
            partial.unlink()
        initial = self._state_payload(
            status="downloading",
            source_mode="odata-zip",
            product_path=product_path,
            fingerprint=fingerprint,
            expected_files=1,
            expected_bytes=expected_bytes,
            completed_files=0,
            completed_bytes=partial.stat().st_size if partial.exists() else 0,
        )
        initial["advertised_sha256"] = expected_sha256
        _write_json_atomic(self.manifest_path, initial)
        digest: str | None = None
        try:
            for attempt in range(self.config.max_retries):
                offset = partial.stat().st_size if partial.exists() else 0
                if expected_bytes and offset > expected_bytes:
                    partial.unlink()
                    offset = 0
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                session = self._http_session()
                try:
                    with session.get(url, headers=headers, stream=True, timeout=120) as response:
                        if response.status_code in {429, 509}:
                            raise QuotaExceededError(
                                f"CDSE product download returned HTTP {response.status_code}",
                                retry_after_seconds=_retry_after_seconds(response.headers),
                                error_code=None,
                                status_code=response.status_code,
                            )
                        if response.status_code != 416:
                            response.raise_for_status()
                            if offset and response.status_code != 206:
                                partial.unlink(missing_ok=True)
                                offset = 0
                            mode = "ab" if offset else "wb"
                            checkpoint = offset
                            with partial.open(mode) as stream:
                                for chunk in response.iter_content(
                                    chunk_size=self.config.chunk_size
                                ):
                                    if stop_requested():
                                        raise DownloadInterrupted(
                                            "Stop requested; partial transfer retained at "
                                            f"{partial}"
                                        )
                                    if chunk:
                                        stream.write(chunk)
                                        checkpoint += len(chunk)
                                    if (
                                        checkpoint - int(initial["completed_bytes"])
                                        >= 64 * 1024 * 1024
                                    ):
                                        initial["completed_bytes"] = checkpoint
                                        initial["updated_at"] = datetime.now(
                                            timezone.utc
                                        ).isoformat()
                                        _write_json_atomic(self.manifest_path, initial)
                                        percent = (
                                            100.0 * checkpoint / expected_bytes
                                            if expected_bytes > 0
                                            else None
                                        )
                                        logger.info(
                                            "Downloading L1C Product ZIP – item=%s bytes=%d%s",
                                            self.item_id,
                                            checkpoint,
                                            "" if percent is None else f" ({percent:.1f}%)",
                                        )

                    actual_bytes = partial.stat().st_size
                    if expected_bytes and actual_bytes != expected_bytes:
                        raise OSError(
                            "Incomplete Product ZIP: expected "
                            f"{expected_bytes} bytes, got {actual_bytes}"
                        )
                    self._validate_safe_zip(partial)
                    digest = sha256_file(partial, self.config.chunk_size)
                    if expected_sha256 is not None and digest != expected_sha256:
                        raise OSError(
                            "Downloaded Product ZIP SHA-256 does not match the STAC checksum"
                        )
                    os.replace(partial, product_path)
                    break
                except (QuotaExceededError, DownloadInterrupted):
                    raise
                except (requests.RequestException, OSError, RuntimeError) as exc:
                    if partial.exists() and (
                        isinstance(exc, RuntimeError)
                        or (expected_bytes > 0 and partial.stat().st_size >= expected_bytes)
                    ):
                        partial.unlink()
                    if attempt >= self.config.max_retries - 1:
                        raise
                    delay = self.config.retry_base_seconds * (2**attempt)
                    logger.warning(
                        "L1C Product download attempt %d/%d failed; retrying in %.1fs: %s",
                        attempt + 1,
                        self.config.max_retries,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                finally:
                    session.close()
        except BaseException as exc:
            _write_json_atomic(
                self.manifest_path,
                self._state_payload(
                    status=(
                        "interrupted"
                        if isinstance(
                            exc,
                            (DownloadInterrupted, KeyboardInterrupt, SystemExit),
                        )
                        else "failed"
                    ),
                    source_mode="odata-zip",
                    product_path=product_path,
                    fingerprint=fingerprint,
                    expected_files=1,
                    expected_bytes=expected_bytes,
                    completed_files=0,
                    completed_bytes=partial.stat().st_size if partial.exists() else 0,
                    error=str(exc),
                ),
            )
            raise

        if digest is None:
            raise RuntimeError("L1C Product download ended without a verified ZIP")
        actual_bytes = product_path.stat().st_size
        payload = self._state_payload(
            status="complete",
            source_mode="odata-zip",
            product_path=product_path,
            fingerprint=fingerprint,
            expected_files=1,
            expected_bytes=expected_bytes or actual_bytes,
            completed_files=1,
            completed_bytes=actual_bytes,
        )
        payload.update(
            {
                "advertised_sha256": expected_sha256,
                "sha256": digest,
                "product_mtime_ns": product_path.stat().st_mtime_ns,
            }
        )
        _write_json_atomic(self.manifest_path, payload)
        return L1CDownloadResult(
            status="complete",
            item_id=self.item_id,
            product_name=self.product_name,
            product_path=product_path,
            manifest_path=self.manifest_path,
            source_mode="odata-zip",
            file_count=1,
            byte_count=actual_bytes,
        )

    def run(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> L1CDownloadResult:
        """Acquire the complete product, preferring configured S3 access."""

        should_stop = stop_requested or (lambda: False)
        s3_uri = self._safe_manifest_uri()
        product = self._product_asset()
        with _DownloadLock(self.lock_path):
            for quota_attempt in range(self.config.max_retries):
                try:
                    if self.config.s3 is not None and s3_uri is not None:
                        try:
                            return self._download_s3_tree(
                                s3_uri,
                                stop_requested=should_stop,
                            )
                        except (
                            QuotaExceededError,
                            DownloadInterrupted,
                            KeyboardInterrupt,
                            SystemExit,
                        ):
                            raise
                        except Exception as exc:
                            if product is None or self.config.auth is None:
                                raise
                            logger.warning(
                                "CDSE S3 SAFE route failed; falling back to the "
                                "authenticated Product ZIP: %s",
                                exc,
                            )
                            return self._download_http_product(
                                product,
                                stop_requested=should_stop,
                            )
                    if product is not None and self.config.auth is not None:
                        return self._download_http_product(
                            product,
                            stop_requested=should_stop,
                        )
                    raise ValueError(
                        "No complete L1C download route is configured. Supply CDSE "
                        "S3 credentials for the safe_manifest prefix or CDSE download "
                        "authentication for Product."
                    )
                except QuotaExceededError as exc:
                    if quota_attempt >= self.config.max_retries - 1:
                        payload = self._existing_manifest() or {
                            "schema_version": 1,
                            "item_id": self.item_id,
                            "product_name": self.product_name,
                        }
                        payload.update(
                            {
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                                "status": "failed",
                                "error": str(exc),
                                "quota_attempt": quota_attempt + 1,
                                "quota_exhausted": True,
                            }
                        )
                        _write_json_atomic(self.manifest_path, payload)
                        raise
                    delay = (
                        exc.retry_after_seconds
                        if exc.retry_after_seconds is not None
                        else self.config.quota_wait_seconds
                    )
                    logger.warning(
                        "CDSE quota/throttling pause %d/%d; retrying in %.1fs: %s",
                        quota_attempt + 1,
                        self.config.max_retries,
                        delay,
                        exc,
                    )
                    payload = self._existing_manifest() or {
                        "schema_version": 1,
                        "item_id": self.item_id,
                        "product_name": self.product_name,
                    }
                    payload.update(
                        {
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                            "status": "waiting_for_quota",
                            "error": str(exc),
                            "quota_attempt": quota_attempt + 1,
                            "quota_retry_seconds": delay,
                            "quota_exhausted": False,
                        }
                    )
                    _write_json_atomic(self.manifest_path, payload)
                    deadline = time.monotonic() + max(0.0, delay)
                    while time.monotonic() < deadline:
                        if should_stop():
                            payload = self._existing_manifest() or {
                                "schema_version": 1,
                                "item_id": self.item_id,
                                "product_name": self.product_name,
                            }
                            payload.update(
                                {
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                    "status": "interrupted",
                                    "error": "Stop requested while waiting for CDSE quota reset",
                                }
                            )
                            _write_json_atomic(self.manifest_path, payload)
                            raise DownloadInterrupted(
                                "Stop requested while waiting for CDSE quota reset"
                            )
                        time.sleep(min(1.0, deadline - time.monotonic()))
                except (DownloadInterrupted, KeyboardInterrupt, SystemExit) as exc:
                    payload = self._existing_manifest() or {
                        "schema_version": 1,
                        "item_id": self.item_id,
                        "product_name": self.product_name,
                    }
                    payload.update(
                        {
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                            "status": "interrupted",
                            "error": str(exc),
                        }
                    )
                    _write_json_atomic(self.manifest_path, payload)
                    raise
        raise RuntimeError("L1C download retry loop ended unexpectedly")
