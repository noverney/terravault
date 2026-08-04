"""Tests for complete, restartable Sentinel-2 L1C product downloads."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from terravault.auth import CDSEDownloadAuthConfig
from terravault.l1c_download import L1CDownloadConfig, L1CProductDownloader
from terravault.s3_downloader import QuotaExceededError, S3Config


PRODUCT_NAME = "S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T142404.SAFE"
ITEM_ID = PRODUCT_NAME.removesuffix(".SAFE")
BANDS = ("01", "02", "03", "04", "05", "06", "07", "08", "8A", "09", "10", "11", "12")


def _safe_members() -> dict[str, bytes]:
    granule = f"{PRODUCT_NAME}/GRANULE/L1C_TEST"
    members = {
        f"{PRODUCT_NAME}/manifest.safe": b"manifest",
        f"{PRODUCT_NAME}/MTD_MSIL1C.xml": b"product metadata",
        f"{granule}/MTD_TL.xml": b"granule metadata",
    }
    members.update({f"{granule}/IMG_DATA/T_TEST_B{band}.jp2": band.encode() for band in BANDS})
    return members


def _safe_zip_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in _safe_members().items():
            archive.writestr(name, data)
    return stream.getvalue()


def _item_payload(
    *,
    level: str = "L1C",
    product_size: int = 0,
    product_checksum: str | None = None,
) -> dict:
    product_name = PRODUCT_NAME.replace("MSIL1C", f"MSIL{level.removeprefix('L')}")
    item_id = product_name.removesuffix(".SAFE")
    return {
        "type": "Feature",
        "id": item_id,
        "properties": {"_private": {"product_name": product_name}},
        "assets": {
            "Product": {
                "href": f"https://example.test/Products({item_id})/$value",
                "type": "application/zip",
                "file:size": product_size,
                "file:local_path": f"{product_name}.zip",
                **({"file:checksum": product_checksum} if product_checksum is not None else {}),
            },
            "safe_manifest": {
                "href": f"s3://eodata/archive/{product_name}/manifest.safe",
            },
        },
    }


def _write_item(tmp_path: Path, payload: dict) -> Path:
    item_path = tmp_path / "item.json"
    item_path.write_text(json.dumps(payload), encoding="utf-8")
    return item_path


class _FakeResponse:
    def __init__(
        self,
        data: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int):
        for offset in range(0, len(self.data), chunk_size):
            yield self.data[offset : offset + chunk_size]


class _FakeSession:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.get_calls: list[dict] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return _FakeResponse(self.data)

    def close(self) -> None:
        self.closed = True


class _FakeS3TreeClient:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.get_calls: list[str] = []
        self.list_calls: list[dict] = []

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(kwargs)
        prefix = kwargs["Prefix"]
        return {
            "IsTruncated": False,
            "Contents": [
                {
                    "Key": key,
                    "Size": len(value),
                    "ETag": f'"{hashlib.sha256(value).hexdigest()}"',
                }
                for key, value in self.objects.items()
                if key.startswith(prefix)
            ],
        }

    def head_object(self, *, Bucket: str, Key: str):
        assert Bucket == "eodata"
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None):
        assert Bucket == "eodata"
        self.get_calls.append(Key)
        data = self.objects[Key]
        response = {"Body": io.BytesIO(data)}
        if Range:
            offset = int(Range.removeprefix("bytes=").removesuffix("-"))
            response["Body"] = io.BytesIO(data[offset:])
            response["ContentRange"] = f"bytes {offset}-{len(data) - 1}/{len(data)}"
        return response


def test_l1c_downloader_rejects_l2a_item(tmp_path):
    item_path = _write_item(tmp_path, _item_payload(level="L2A"))

    with pytest.raises(ValueError, match="MSIL1C"):
        L1CProductDownloader(
            L1CDownloadConfig(item_path=item_path, output_root=tmp_path / "output")
        )


def test_l1c_downloader_rejects_unsafe_item_id_and_object_path(tmp_path):
    payload = _item_payload()
    payload["id"] = "../../unsafe"
    with pytest.raises(ValueError, match="unsafe filename"):
        L1CProductDownloader(
            L1CDownloadConfig(
                item_path=_write_item(tmp_path, payload),
                output_root=tmp_path / "output",
            )
        )

    with pytest.raises(ValueError, match="Unsafe SAFE object key"):
        L1CProductDownloader._safe_relative_path("safe//tmp/file", "safe/")


def test_s3_checkpoint_survives_http_fallback_state(tmp_path):
    downloader = L1CProductDownloader(
        L1CDownloadConfig(
            item_path=_write_item(tmp_path, _item_payload()),
            output_root=tmp_path / "output",
            s3=S3Config("access", "secret"),
            auth=CDSEDownloadAuthConfig(access_token="test-token"),
        )
    )
    s3_state = {
        "source_mode": "s3-tree",
        "status": "failed",
        "fingerprint": "s3-fingerprint",
        "file_sha256": {"manifest.safe": "digest"},
    }
    downloader._write_s3_state(s3_state)
    downloader.manifest_path.write_text(
        json.dumps(
            {
                "source_mode": "odata-zip",
                "status": "complete",
                "fingerprint": "http-fingerprint",
            }
        ),
        encoding="utf-8",
    )

    assert downloader._existing_s3_manifest() == s3_state


def test_odata_product_writes_durable_manifest_and_skips_verified_file(tmp_path):
    product = _safe_zip_bytes()
    digest = hashlib.sha256(product).hexdigest()
    item_path = _write_item(
        tmp_path,
        _item_payload(
            product_size=len(product),
            product_checksum=f"1220{digest}",
        ),
    )
    config = L1CDownloadConfig(
        item_path=item_path,
        output_root=tmp_path / "output",
        auth=CDSEDownloadAuthConfig(access_token="test-token"),
        chunk_size=5,
    )
    session = _FakeSession(product)

    result = L1CProductDownloader(config, session_factory=lambda: session).run()

    assert result.status == "complete"
    assert result.source_mode == "odata-zip"
    assert not result.skipped
    assert result.product_path.name == f"{PRODUCT_NAME}.zip"
    assert result.product_path.read_bytes() == product
    assert session.closed
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["expected_files"] == 1
    assert manifest["completed_bytes"] == len(product)
    assert manifest["advertised_sha256"] == digest
    assert manifest["sha256"] == digest

    def unexpected_session():
        raise AssertionError("verified complete product should not open an HTTP session")

    repeated = L1CProductDownloader(config, session_factory=unexpected_session).run()
    assert repeated.skipped
    assert repeated.product_path == result.product_path


def test_odata_product_waits_for_quota_and_retries_with_a_fresh_session(tmp_path):
    product = _safe_zip_bytes()
    item_path = _write_item(tmp_path, _item_payload(product_size=len(product)))
    sessions = [
        _FakeSession(b""),
        _FakeSession(product),
    ]
    sessions[0].get = lambda *_args, **_kwargs: _FakeResponse(
        b"",
        status_code=429,
        headers={"Retry-After": "0"},
    )

    result = L1CProductDownloader(
        L1CDownloadConfig(
            item_path=item_path,
            output_root=tmp_path / "output",
            auth=CDSEDownloadAuthConfig(access_token="test-token"),
            retry_base_seconds=0,
            quota_wait_seconds=0,
        ),
        session_factory=lambda: sessions.pop(0),
    ).run()

    assert result.status == "complete"
    assert not sessions


def test_odata_product_records_exhausted_quota_retries(tmp_path):
    item_path = _write_item(tmp_path, _item_payload(product_size=100))

    def quota_session():
        session = _FakeSession(b"")
        session.get = lambda *_args, **_kwargs: _FakeResponse(
            b"",
            status_code=429,
            headers={"Retry-After": "0"},
        )
        return session

    downloader = L1CProductDownloader(
        L1CDownloadConfig(
            item_path=item_path,
            output_root=tmp_path / "output",
            auth=CDSEDownloadAuthConfig(access_token="test-token"),
            max_retries=2,
            quota_wait_seconds=0,
        ),
        session_factory=quota_session,
    )

    with pytest.raises(QuotaExceededError):
        downloader.run()

    manifest = json.loads(downloader.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["quota_attempt"] == 2
    assert manifest["quota_exhausted"] is True


def test_s3_tree_preserves_safe_hierarchy_and_skips_verified_tree(tmp_path):
    prefix = f"archive/{PRODUCT_NAME}/"
    objects = {
        f"{prefix}{name.removeprefix(PRODUCT_NAME + '/')}": data
        for name, data in _safe_members().items()
    }
    client = _FakeS3TreeClient(objects)
    item_path = _write_item(tmp_path, _item_payload())
    config = L1CDownloadConfig(
        item_path=item_path,
        output_root=tmp_path / "output",
        s3=S3Config("access", "secret", chunk_size=3),
    )

    result = L1CProductDownloader(config, s3_client=client).run()

    assert result.status == "complete"
    assert result.source_mode == "s3-tree"
    assert result.file_count == len(objects)
    assert (result.product_path / "manifest.safe").read_bytes() == b"manifest"
    assert (result.product_path / "GRANULE/L1C_TEST/IMG_DATA/T_TEST_B10.jp2").read_bytes() == b"10"
    first_get_count = len(client.get_calls)

    repeated = L1CProductDownloader(config, s3_client=client).run()
    assert repeated.skipped
    assert len(client.get_calls) == first_get_count


def test_s3_tree_redownloads_a_same_size_changed_object(tmp_path):
    prefix = f"archive/{PRODUCT_NAME}/"
    objects = {
        f"{prefix}{name.removeprefix(PRODUCT_NAME + '/')}": data
        for name, data in _safe_members().items()
    }
    client = _FakeS3TreeClient(objects)
    config = L1CDownloadConfig(
        item_path=_write_item(tmp_path, _item_payload()),
        output_root=tmp_path / "output",
        s3=S3Config("access", "secret"),
    )
    first = L1CProductDownloader(config, s3_client=client).run()
    changed_key = f"{prefix}GRANULE/L1C_TEST/IMG_DATA/T_TEST_B04.jp2"
    client.objects[changed_key] = b"xx"

    repeated = L1CProductDownloader(config, s3_client=client).run()

    assert not repeated.skipped
    assert (first.product_path / "GRANULE/L1C_TEST/IMG_DATA/T_TEST_B04.jp2").read_bytes() == b"xx"


def test_s3_tree_repairs_same_size_local_corruption(tmp_path):
    prefix = f"archive/{PRODUCT_NAME}/"
    objects = {
        f"{prefix}{name.removeprefix(PRODUCT_NAME + '/')}": data
        for name, data in _safe_members().items()
    }
    client = _FakeS3TreeClient(objects)
    config = L1CDownloadConfig(
        item_path=_write_item(tmp_path, _item_payload()),
        output_root=tmp_path / "output",
        s3=S3Config("access", "secret"),
    )
    first = L1CProductDownloader(config, s3_client=client).run()
    band = first.product_path / "GRANULE/L1C_TEST/IMG_DATA/T_TEST_B04.jp2"
    band.write_bytes(b"xx")
    first_get_count = len(client.get_calls)

    repaired = L1CProductDownloader(config, s3_client=client).run()

    assert not repaired.skipped
    assert len(client.get_calls) > first_get_count
    assert band.read_bytes() == b"04"
    manifest = json.loads(repaired.manifest_path.read_text(encoding="utf-8"))
    relative = "GRANULE/L1C_TEST/IMG_DATA/T_TEST_B04.jp2"
    assert manifest["file_sha256"][relative] == hashlib.sha256(b"04").hexdigest()


def test_s3_tree_repairs_same_size_corruption_in_resumable_staging(tmp_path):
    prefix = f"archive/{PRODUCT_NAME}/"
    objects = {
        f"{prefix}{name.removeprefix(PRODUCT_NAME + '/')}": data
        for name, data in _safe_members().items()
    }
    client = _FakeS3TreeClient(objects)
    config = L1CDownloadConfig(
        item_path=_write_item(tmp_path, _item_payload()),
        output_root=tmp_path / "output",
        s3=S3Config("access", "secret"),
    )
    first = L1CProductDownloader(config, s3_client=client).run()
    staging = first.product_path.with_name(f".{first.product_path.name}.partial")
    first.product_path.rename(staging)
    relative = Path("GRANULE/L1C_TEST/IMG_DATA/T_TEST_B04.jp2")
    (staging / relative).write_bytes(b"xx")
    first_get_count = len(client.get_calls)

    repaired = L1CProductDownloader(config, s3_client=client).run()

    assert not repaired.skipped
    assert len(client.get_calls) == first_get_count + 1
    assert (repaired.product_path / relative).read_bytes() == b"04"


def test_s3_tree_rebuilds_legacy_manifest_without_content_hashes(tmp_path):
    prefix = f"archive/{PRODUCT_NAME}/"
    objects = {
        f"{prefix}{name.removeprefix(PRODUCT_NAME + '/')}": data
        for name, data in _safe_members().items()
    }
    client = _FakeS3TreeClient(objects)
    config = L1CDownloadConfig(
        item_path=_write_item(tmp_path, _item_payload()),
        output_root=tmp_path / "output",
        s3=S3Config("access", "secret"),
    )
    first = L1CProductDownloader(config, s3_client=client).run()
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest.pop("file_sha256")
    first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    first_get_count = len(client.get_calls)

    rebuilt = L1CProductDownloader(config, s3_client=client).run()

    assert not rebuilt.skipped
    assert len(client.get_calls) > first_get_count
    rebuilt_manifest = json.loads(rebuilt.manifest_path.read_text(encoding="utf-8"))
    assert len(rebuilt_manifest["file_sha256"]) == len(objects)


def test_s3_tree_keeps_last_publication_when_changed_download_is_stopped(tmp_path):
    prefix = f"archive/{PRODUCT_NAME}/"
    objects = {
        f"{prefix}{name.removeprefix(PRODUCT_NAME + '/')}": data
        for name, data in _safe_members().items()
    }
    client = _FakeS3TreeClient(objects)
    config = L1CDownloadConfig(
        item_path=_write_item(tmp_path, _item_payload()),
        output_root=tmp_path / "output",
        s3=S3Config("access", "secret"),
    )
    first = L1CProductDownloader(config, s3_client=client).run()
    changed_key = f"{prefix}GRANULE/L1C_TEST/IMG_DATA/T_TEST_B04.jp2"
    client.objects[changed_key] = b"xx"

    with pytest.raises(RuntimeError, match="Stop requested"):
        L1CProductDownloader(config, s3_client=client).run(stop_requested=lambda: True)

    published_band = first.product_path / "GRANULE/L1C_TEST/IMG_DATA/T_TEST_B04.jp2"
    assert published_band.read_bytes() == b"04"
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "interrupted"
    assert Path(manifest["staging_path"]).is_dir()


def test_s3_tree_rejects_prefix_without_safe_manifest(tmp_path):
    prefix = f"archive/{PRODUCT_NAME}/"
    client = _FakeS3TreeClient({f"{prefix}MTD_MSIL1C.xml": b"metadata"})
    item_path = _write_item(tmp_path, _item_payload())
    downloader = L1CProductDownloader(
        L1CDownloadConfig(
            item_path=item_path,
            output_root=tmp_path / "output",
            s3=S3Config("access", "secret"),
        ),
        s3_client=client,
    )

    with pytest.raises(RuntimeError, match="empty or incomplete"):
        downloader.run()


def test_s3_tree_rejects_safe_missing_a_spectral_band(tmp_path):
    prefix = f"archive/{PRODUCT_NAME}/"
    objects = {
        f"{prefix}{name.removeprefix(PRODUCT_NAME + '/')}": data
        for name, data in _safe_members().items()
        if not name.endswith("_B04.jp2")
    }
    downloader = L1CProductDownloader(
        L1CDownloadConfig(
            item_path=_write_item(tmp_path, _item_payload()),
            output_root=tmp_path / "output",
            s3=S3Config("access", "secret"),
        ),
        s3_client=_FakeS3TreeClient(objects),
    )

    with pytest.raises(RuntimeError, match="incomplete granules"):
        downloader.run()
