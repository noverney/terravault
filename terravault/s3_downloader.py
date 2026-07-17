"""Resumable downloads for native Copernicus Data Space S3 assets."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


class DownloadInterrupted(RuntimeError):
    """Raised when a graceful stop is requested during a download."""


class QuotaExceededError(RuntimeError):
    """A remote usage/throttling response that may succeed after waiting."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None,
        error_code: str | None,
        status_code: int | None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.error_code = error_code
        self.status_code = status_code


_QUOTA_ERROR_CODES = {
    "bandwidthlimitexceeded",
    "limitexceeded",
    "quotaexceeded",
    "requestlimitexceeded",
    "servicequotaexceeded",
    "servicequotaexceededexception",
    "slowdown",
    "throttled",
    "throttling",
    "throttlingexception",
    "toomanyrequests",
    "toomanyrequestsexception",
}


def _retry_after_seconds(headers: dict[str, Any]) -> float | None:
    value = next(
        (header_value for key, header_value in headers.items() if key.lower() == "retry-after"),
        None,
    )
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def quota_error_from_exception(exc: Exception) -> QuotaExceededError | None:
    """Translate a boto-style throttling exception without importing botocore."""

    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error") or {}
    metadata = response.get("ResponseMetadata") or {}
    code = str(error.get("Code") or "") or None
    status_raw = metadata.get("HTTPStatusCode")
    status = int(status_raw) if isinstance(status_raw, (int, str)) and str(status_raw).isdigit() else None
    is_quota = status in {429, 509} or (
        code is not None and code.lower() in _QUOTA_ERROR_CODES
    )
    if not is_quota:
        return None
    headers = metadata.get("HTTPHeaders") or {}
    retry_after = _retry_after_seconds(headers) if isinstance(headers, dict) else None
    message = str(error.get("Message") or exc)
    return QuotaExceededError(
        f"Remote quota/throttling response"
        f"{f' ({code})' if code else ''}"
        f"{f' HTTP {status}' if status else ''}: {message}",
        retry_after_seconds=retry_after,
        error_code=code,
        status_code=status,
    )


@dataclass(frozen=True)
class S3Config:
    """Connection and transfer settings for the CDSE S3 service."""

    access_key: str
    secret_key: str
    endpoint_url: str = "https://eodata.dataspace.copernicus.eu"
    region_name: str = "default"
    chunk_size: int = 8 * 1024 * 1024

    def validate(self) -> None:
        if not self.access_key or not self.secret_key:
            raise ValueError(
                "Native asset downloads require TERRAVAULT_CDSE_S3_ACCESS_KEY and "
                "TERRAVAULT_CDSE_S3_SECRET_KEY (or the corresponding CLI flags)."
            )
        if self.chunk_size <= 0:
            raise ValueError("S3 chunk_size must be positive")


@dataclass(frozen=True)
class S3DownloadResult:
    """Result of one verified-size S3 transfer."""

    path: Path
    byte_count: int
    sha256: str
    resumed_from: int
    skipped: bool


def split_s3_uri(uri: str) -> tuple[str, str]:
    """Return the bucket and key from an ``s3://`` URI."""

    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"Expected an s3://bucket/key URI, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class S3Downloader:
    """Download S3 objects atomically while retaining resumable ``.part`` files."""

    def __init__(self, config: S3Config, client: Any | None = None) -> None:
        config.validate()
        self.config = config
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError(
                    "Native S3 ingestion requires the optional dependency: "
                    'python -m pip install -e ".[s3]"'
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.config.endpoint_url,
                aws_access_key_id=self.config.access_key,
                aws_secret_access_key=self.config.secret_key,
                region_name=self.config.region_name,
            )
        return self._client

    def download(
        self,
        uri: str,
        destination: str | Path,
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> S3DownloadResult:
        """Download an object, resuming a prior partial transfer when possible."""

        should_stop = stop_requested or (lambda: False)
        if should_stop():
            raise DownloadInterrupted("Stop requested before download")

        bucket, key = split_s3_uri(uri)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.part")

        try:
            head = self.client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            quota_error = quota_error_from_exception(exc)
            if quota_error is not None:
                raise quota_error from exc
            raise
        total_size = int(head["ContentLength"])

        if destination.exists() and destination.stat().st_size == total_size:
            return S3DownloadResult(
                path=destination,
                byte_count=total_size,
                sha256=sha256_file(destination, self.config.chunk_size),
                resumed_from=total_size,
                skipped=True,
            )

        if destination.exists():
            if not partial.exists() and destination.stat().st_size < total_size:
                os.replace(destination, partial)
            else:
                destination.unlink()

        offset = partial.stat().st_size if partial.exists() else 0
        if offset > total_size:
            partial.unlink()
            offset = 0
        if offset == total_size:
            os.replace(partial, destination)
            return S3DownloadResult(
                path=destination,
                byte_count=total_size,
                sha256=sha256_file(destination, self.config.chunk_size),
                resumed_from=offset,
                skipped=False,
            )

        get_args: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if offset:
            get_args["Range"] = f"bytes={offset}-"
        try:
            response = self.client.get_object(**get_args)
        except Exception as exc:  # noqa: BLE001
            quota_error = quota_error_from_exception(exc)
            if quota_error is not None:
                raise quota_error from exc
            raise
        body = response["Body"]

        # A conforming S3 endpoint returns ContentRange for ranged requests.
        # Refuse to append a full-object response to a partial file.
        if offset and not response.get("ContentRange"):
            body.close()
            partial.unlink(missing_ok=True)
            offset = 0
            try:
                response = self.client.get_object(Bucket=bucket, Key=key)
            except Exception as exc:  # noqa: BLE001
                quota_error = quota_error_from_exception(exc)
                if quota_error is not None:
                    raise quota_error from exc
                raise
            body = response["Body"]

        mode = "ab" if offset else "wb"
        try:
            with partial.open(mode) as stream:
                while True:
                    if should_stop():
                        raise DownloadInterrupted(
                            f"Stop requested; partial transfer retained at {partial}"
                        )
                    chunk = body.read(self.config.chunk_size)
                    if not chunk:
                        break
                    stream.write(chunk)
                    stream.flush()
        finally:
            body.close()

        actual_size = partial.stat().st_size
        if actual_size != total_size:
            raise OSError(
                f"Incomplete S3 transfer for {uri}: expected {total_size} bytes, "
                f"received {actual_size}"
            )

        os.replace(partial, destination)
        return S3DownloadResult(
            path=destination,
            byte_count=actual_size,
            sha256=sha256_file(destination, self.config.chunk_size),
            resumed_from=offset,
            skipped=False,
        )
