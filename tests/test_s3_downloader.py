"""Tests for resumable native S3 downloads."""

from __future__ import annotations

import io

import pytest

from terravault.s3_downloader import (
    DownloadInterrupted,
    QuotaExceededError,
    S3Config,
    S3Downloader,
    split_s3_uri,
)


class FakeBody(io.BytesIO):
    pass


class FakeS3Client:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.get_calls: list[dict[str, str]] = []

    def head_object(self, **_kwargs):
        return {"ContentLength": len(self.data)}

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        range_value = kwargs.get("Range")
        if range_value:
            offset = int(range_value.removeprefix("bytes=").removesuffix("-"))
            return {
                "Body": FakeBody(self.data[offset:]),
                "ContentRange": f"bytes {offset}-{len(self.data) - 1}/{len(self.data)}",
            }
        return {"Body": FakeBody(self.data)}


def _downloader(data: bytes, chunk_size: int = 2) -> tuple[S3Downloader, FakeS3Client]:
    client = FakeS3Client(data)
    config = S3Config("access", "secret", chunk_size=chunk_size)
    return S3Downloader(config, client=client), client


def test_split_s3_uri():
    assert split_s3_uri("s3://eodata/path/to/file.jp2") == (
        "eodata",
        "path/to/file.jp2",
    )
    with pytest.raises(ValueError):
        split_s3_uri("https://example.com/file.jp2")


def test_fresh_download_is_atomic_and_hashed(tmp_path):
    downloader, client = _downloader(b"abcdef")
    destination = tmp_path / "B04.jp2"

    result = downloader.download("s3://eodata/B04.jp2", destination)

    assert destination.read_bytes() == b"abcdef"
    assert not (tmp_path / "B04.jp2.part").exists()
    assert result.byte_count == 6
    assert result.resumed_from == 0
    assert not result.skipped
    assert len(result.sha256) == 64
    assert "Range" not in client.get_calls[0]


def test_resumes_existing_partial_download(tmp_path):
    downloader, client = _downloader(b"abcdef")
    destination = tmp_path / "B04.jp2"
    (tmp_path / "B04.jp2.part").write_bytes(b"abc")

    result = downloader.download("s3://eodata/B04.jp2", destination)

    assert destination.read_bytes() == b"abcdef"
    assert result.resumed_from == 3
    assert client.get_calls[0]["Range"] == "bytes=3-"


def test_stop_retains_partial_file(tmp_path):
    downloader, _client = _downloader(b"abcdef", chunk_size=2)
    destination = tmp_path / "B04.jp2"
    checks = 0

    def stop_requested() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(DownloadInterrupted):
        downloader.download(
            "s3://eodata/B04.jp2",
            destination,
            stop_requested=stop_requested,
        )

    assert not destination.exists()
    assert (tmp_path / "B04.jp2.part").read_bytes() == b"ab"


def test_quota_response_exposes_retry_after(tmp_path):
    class FakeClientError(Exception):
        def __init__(self):
            self.response = {
                "Error": {"Code": "SlowDown", "Message": "usage limit reached"},
                "ResponseMetadata": {
                    "HTTPStatusCode": 429,
                    "HTTPHeaders": {"retry-after": "120"},
                },
            }

    class QuotaClient:
        def head_object(self, **_kwargs):
            raise FakeClientError()

    downloader = S3Downloader(
        S3Config("access", "secret"),
        client=QuotaClient(),
    )
    with pytest.raises(QuotaExceededError) as caught:
        downloader.download("s3://eodata/B04.jp2", tmp_path / "B04.jp2")

    assert caught.value.retry_after_seconds == 120
    assert caught.value.error_code == "SlowDown"
    assert caught.value.status_code == 429
