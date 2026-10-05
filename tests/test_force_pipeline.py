"""Tests for Copernicus discovery and high-level native FORCE orchestration."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pystac
import pytest

import terravault.force_pipeline as force_pipeline_module
from terravault.cli import _default_log_file, build_parser
from terravault.force_level2 import ForceLevel2Cancelled, ForceLevel2Result
from terravault.force_pipeline import (
    ForceLevel2Options,
    ForcePipeline,
    ForcePipelineConfig,
    ForcePipelineDatabase,
    run_force_pipeline,
)
from terravault.l1c_download import L1CDownloadResult


def _item(
    *,
    platform: str = "S2B",
    sensing: str = "20260717T103029",
    baseline: str = "0512",
    tile: str = "T32TMT",
    production: str = "20260717T142404",
    cloud_cover: float = 8.0,
) -> pystac.Item:
    product_name = (
        f"{platform}_MSIL1C_{sensing}_N{baseline}_R108_{tile}_{production}.SAFE"
    )
    item = pystac.Item(
        id=product_name.removesuffix(".SAFE"),
        geometry={
            "type": "Polygon",
            "coordinates": [
                [[7.0, 46.0], [8.0, 46.0], [8.0, 47.0], [7.0, 47.0], [7.0, 46.0]]
            ],
        },
        bbox=[7.0, 46.0, 8.0, 47.0],
        datetime=datetime.strptime(sensing, "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        ),
        properties={
            "eo:cloud_cover": cloud_cover,
            "_private": {"product_name": product_name},
        },
        collection="sentinel-2-l1c",
    )
    item.add_asset(
        "Product",
        pystac.Asset(
            href=f"https://example.test/{product_name}.zip",
            media_type="application/zip",
            extra_fields={"file:local_path": f"{product_name}.zip"},
        ),
    )
    return item


class _Catalog:
    def __init__(self, items: list[pystac.Item]) -> None:
        self.items = items

    def search(self, **_kwargs):
        return iter(self.items)


class _Downloader:
    calls: list[Path] = []

    def __init__(self, config) -> None:
        self.config = config

    def run(self) -> L1CDownloadResult:
        item = json.loads(self.config.item_path.read_text(encoding="utf-8"))
        product_name = item["properties"]["_private"]["product_name"]
        product_path = self.config.output_root / "level1" / product_name
        product_path.mkdir(parents=True, exist_ok=True)
        self.calls.append(product_path)
        return L1CDownloadResult(
            status="complete",
            item_id=item["id"],
            product_name=product_name,
            product_path=product_path,
            manifest_path=self.config.output_root / "download.json",
            source_mode="fake",
            file_count=17,
            byte_count=1234,
        )


class _Processor:
    calls: list[Path] = []

    def __init__(self, config) -> None:
        self.config = config

    def run(self) -> ForceLevel2Result:
        self.calls.append(self.config.input_path)
        stem = self.config.input_path.name.removesuffix(".SAFE")
        root = self.config.output_root / "level2" / "products" / stem
        tile = root / "X0000_Y0000"
        mosaic = root / "mosaic"
        tile.mkdir(parents=True, exist_ok=True)
        mosaic.mkdir(parents=True, exist_ok=True)
        boa = tile / "20260717_LEVEL2_SEN2B_BOA.tif"
        qai = tile / "20260717_LEVEL2_SEN2B_QAI.tif"
        overview = tile / "20260717_LEVEL2_SEN2B_OVV.jpg"
        boa_mosaic = mosaic / "20260717_LEVEL2_SEN2B_BOA.vrt"
        qai_mosaic = mosaic / "20260717_LEVEL2_SEN2B_QAI.vrt"
        for path in (boa, qai, overview, boa_mosaic, qai_mosaic):
            path.write_bytes(b"image")
        return ForceLevel2Result(
            status="complete",
            runtime="native",
            input_path=self.config.input_path,
            level2_root=root,
            parameter_path=root / "parameters.prm",
            queue_path=root / "queue.txt",
            manifest_path=root / "manifest.json",
            boa_paths=(boa,),
            qai_paths=(qai,),
            overview_paths=(overview,),
            boa_mosaic_path=boa_mosaic,
            qai_mosaic_path=qai_mosaic,
            commands=(),
        )


class _InterruptedProcessor:
    def __init__(self, config) -> None:
        self.config = config

    def run(self):
        raise KeyboardInterrupt("stopped")


def _config(tmp_path: Path) -> ForcePipelineConfig:
    return ForcePipelineConfig(
        output_root=tmp_path / "force",
        start_datetime=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 8, 1, tzinfo=timezone.utc),
        bbox=(5.96, 45.82, 10.49, 47.81),
        max_cloud_cover=20,
    )


def test_discover_uses_force_duplicate_policy_and_sensor_filter(tmp_path):
    older_baseline = _item(baseline="0400", production="20260717T160000")
    older_publication = _item(baseline="0512", production="20260717T130000")
    selected_version = _item(baseline="0512", production="20260717T142404")
    other_sensor = _item(
        platform="S2A",
        sensing="20260718T103031",
        production="20260718T140000",
    )
    config = ForcePipelineConfig(
        **{
            **_config(tmp_path).__dict__,
            "sensors": ("S2B",),
        }
    )

    with ForcePipeline(
        config,
        catalog=_Catalog(
            [older_baseline, older_publication, selected_version, other_sensor]
        ),
    ) as pipeline:
        discovered, selected = pipeline.discover()

    assert discovered == 4
    assert [item.id for item in selected] == [selected_version.id]


def test_run_creates_force_queue_and_queryable_image_database(tmp_path):
    item = _item()
    config = _config(tmp_path)
    _Downloader.calls = []
    _Processor.calls = []

    with ForcePipeline(
        config,
        catalog=_Catalog([item]),
        downloader_factory=_Downloader,
        processor_factory=_Processor,
    ) as pipeline:
        result = pipeline.run()

    assert result.status == "complete"
    assert result.discovered == 1
    assert result.selected == 1
    assert result.downloaded == 1
    assert result.processed == 1
    assert result.database_path.is_file()
    assert result.queue_path.read_text(encoding="utf-8") == (
        f"{_Downloader.calls[0]} DONE\n"
    )

    with ForcePipelineDatabase(result.database_path) as database:
        scenes = database.scenes()
        images = database.images()
    assert scenes[0]["cloud_cover"] == 8.0
    assert scenes[0]["download_status"] == "complete"
    assert scenes[0]["force_status"] == "complete"
    assert {image["image_type"] for image in images} == {
        "L1C_SAFE",
        "BOA",
        "QAI",
        "OVERVIEW",
        "BOA_MOSAIC",
        "QAI_MOSAIC",
    }
    assert all(Path(image["local_path"]).exists() for image in images)

    connection = duckdb.connect(str(result.database_path))
    try:
        run = connection.execute(
            "SELECT status, discovered, selected, downloaded, processed FROM runs"
        ).fetchone()
    finally:
        connection.close()
    assert run == ("complete", 1, 1, 1, 1)


def test_metadata_only_run_records_selection_without_downloading(tmp_path):
    item = _item()
    config = _config(tmp_path)

    with ForcePipeline(config, catalog=_Catalog([item])) as pipeline:
        result = pipeline.run(download=False, process=False)

    assert result.status == "complete"
    assert result.selected == 1
    assert result.downloaded == 0
    assert result.processed == 0
    assert result.queue_path.is_file()
    assert result.queue_path.read_text(encoding="utf-8") == ""

    with ForcePipelineDatabase(result.database_path) as database:
        scenes = database.scenes()
        images = database.images()
    assert scenes[0]["download_status"] == "selected"
    assert images == ()


def test_force_pipeline_cli_exposes_selection_and_useful_force_options(tmp_path):
    output_root = tmp_path / "force"
    args = build_parser().parse_args(
        [
            "force-pipeline",
            "--bbox",
            "5.96",
            "45.82",
            "10.49",
            "47.81",
            "--start-date",
            "2026-07-01",
            "--end-date",
            "2026-07-31",
            "--max-cloud-cover",
            "12.5",
            "--max-cloud-cover-frame",
            "80",
            "--max-cloud-cover-tile",
            "60",
            "--processes",
            "2",
            "--threads",
            "4",
            "--output-root",
            str(output_root),
        ]
    )

    assert args.max_cloud_cover == 12.5
    assert args.max_cloud_cover_frame == 80
    assert args.max_cloud_cover_tile == 60
    assert args.processes == 2
    assert args.threads == 4
    assert _default_log_file(args) == (
        output_root / "_terravault/logs/force-pipeline.log"
    )


def test_force_pipeline_cli_process_only_is_mutually_exclusive(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "force-pipeline",
            "--bbox",
            "5.96",
            "45.82",
            "10.49",
            "47.81",
            "--start-date",
            "2026-07-01",
            "--output-root",
            str(tmp_path / "force"),
            "--process-only",
        ]
    )

    assert args.process_only is True
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "force-pipeline",
                "--bbox",
                "5.96",
                "45.82",
                "10.49",
                "47.81",
                "--start-date",
                "2026-07-01",
                "--output-root",
                str(tmp_path / "force"),
                "--process-only",
                "--download-only",
            ]
        )


def test_interruption_is_reflected_in_database_and_force_queue(tmp_path):
    item = _item()
    config = _config(tmp_path)

    with ForcePipeline(
        config,
        catalog=_Catalog([item]),
        downloader_factory=_Downloader,
        processor_factory=_InterruptedProcessor,
    ) as pipeline:
        with pytest.raises(KeyboardInterrupt):
            pipeline.run()

    with ForcePipelineDatabase(config.effective_database_path) as database:
        scene = database.scenes()[0]
        run = database.connection.execute(
            "SELECT status FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    assert scene["force_status"] == "interrupted"
    assert run[0] == "interrupted"
    assert config.effective_queue_path.read_text(encoding="utf-8").endswith(" FAIL\n")


def test_convenience_api_resolves_cdse_credentials_from_environment(
    tmp_path,
    monkeypatch,
):
    captured = None

    class _Pipeline:
        def __init__(self, config) -> None:
            nonlocal captured
            captured = config

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def run(self, **_kwargs):
            return captured

    monkeypatch.setenv("TERRAVAULT_CDSE_S3_ACCESS_KEY", "access")
    monkeypatch.setenv("TERRAVAULT_CDSE_S3_SECRET_KEY", "secret")
    monkeypatch.setenv("TERRAVAULT_CDSE_ACCESS_TOKEN", "bearer")
    monkeypatch.setattr(force_pipeline_module, "ForcePipeline", _Pipeline)

    result = run_force_pipeline(
        output_root=tmp_path / "force",
        start_datetime=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 8, 1, tzinfo=timezone.utc),
        bbox=(5.96, 45.82, 10.49, 47.81),
        download=False,
        process=False,
    )

    assert result.s3.access_key == "access"
    assert result.s3.secret_key == "secret"
    assert result.auth.access_token == "bearer"


def _two_items() -> list[pystac.Item]:
    return [
        _item(sensing="20260717T103029", production="20260717T142404"),
        _item(sensing="20260720T102559", production="20260720T123456"),
    ]


def test_scene_workers_process_scenes_concurrently(tmp_path):
    config = _config(tmp_path)
    config = ForcePipelineConfig(
        **{**config.__dict__, "force": ForceLevel2Options(scene_workers=2)}
    )
    both_running = threading.Barrier(2, timeout=5)
    threads: set[str] = set()

    class _ConcurrentProcessor(_Processor):
        def run(self):
            threads.add(threading.current_thread().name)
            both_running.wait()
            return super().run()

    with ForcePipeline(
        config,
        catalog=_Catalog(_two_items()),
        downloader_factory=_Downloader,
        processor_factory=_ConcurrentProcessor,
    ) as pipeline:
        result = pipeline.run()

    assert result.status == "complete"
    assert result.processed == 2
    assert len(threads) == 2
    with ForcePipelineDatabase(result.database_path) as database:
        assert {scene["force_status"] for scene in database.scenes()} == {"complete"}
    assert result.queue_path.read_text(encoding="utf-8").count(" DONE\n") == 2


def test_downloads_continue_while_a_scene_is_processed(tmp_path):
    second_downloaded = threading.Event()
    processed_before_second_download: list[bool] = []

    class _TrackingDownloader(_Downloader):
        def run(self):
            result = super().run()
            if "20260720" in result.product_name:
                second_downloaded.set()
            return result

    class _WaitingProcessor(_Processor):
        def run(self):
            if "20260717" in self.config.input_path.name:
                processed_before_second_download.append(not second_downloaded.wait(5))
            return super().run()

    with ForcePipeline(
        _config(tmp_path),
        catalog=_Catalog(_two_items()),
        downloader_factory=_TrackingDownloader,
        processor_factory=_WaitingProcessor,
    ) as pipeline:
        result = pipeline.run()

    assert result.status == "complete"
    assert processed_before_second_download == [False]


def test_interruption_cancels_running_force_jobs(tmp_path):
    first_started = threading.Event()
    cancelled: list[str] = []

    class _CancellableProcessor(_Processor):
        def __init__(self, config) -> None:
            super().__init__(config)
            self._cancelled = threading.Event()

        def cancel(self) -> None:
            cancelled.append(self.config.input_path.name)
            self._cancelled.set()

        def run(self):
            first_started.set()
            if not self._cancelled.wait(5):
                raise AssertionError("pipeline did not cancel the running FORCE job")
            raise ForceLevel2Cancelled("stopped")

    class _InterruptingDownloader(_Downloader):
        def run(self):
            item = json.loads(self.config.item_path.read_text(encoding="utf-8"))
            if "20260720" in item["id"]:
                assert first_started.wait(5)
                raise KeyboardInterrupt("stop")
            return super().run()

    config = _config(tmp_path)
    with ForcePipeline(
        config,
        catalog=_Catalog(_two_items()),
        downloader_factory=_InterruptingDownloader,
        processor_factory=_CancellableProcessor,
    ) as pipeline:
        with pytest.raises(KeyboardInterrupt):
            pipeline.run()

    assert len(cancelled) == 1 and "20260717" in cancelled[0]
    with ForcePipelineDatabase(config.effective_database_path) as database:
        scenes = {scene["item_id"]: scene for scene in database.scenes()}
        run = database.connection.execute(
            "SELECT status FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    first, second = sorted(scenes)
    assert scenes[first]["force_status"] == "interrupted"
    assert scenes[second]["download_status"] == "interrupted"
    assert run[0] == "interrupted"


def test_clip_to_selection_passes_a_stable_aoi_to_force(tmp_path):
    configs = []

    class _RecordingProcessor(_Processor):
        def __init__(self, config) -> None:
            super().__init__(config)
            configs.append(config)

    config = ForcePipelineConfig(**{**_config(tmp_path).__dict__, "clip_to_selection": True})
    for _ in range(2):
        with ForcePipeline(
            config,
            catalog=_Catalog([_item()]),
            downloader_factory=_Downloader,
            processor_factory=_RecordingProcessor,
        ) as pipeline:
            assert pipeline.run().status == "complete"

    first, second = configs
    assert first.aoi_path == second.aoi_path
    assert first.aoi_path.parent == config.output_root / "_terravault/force-pipeline/aoi"
    aoi = json.loads(first.aoi_path.read_text(encoding="utf-8"))
    assert aoi["features"][0]["geometry"]["coordinates"][0][0] == [5.96, 45.82]
    with pytest.raises(ValueError, match="clip_to_selection"):
        ForcePipelineConfig(
            **{
                **_config(tmp_path).__dict__,
                "clip_to_selection": True,
                "force": ForceLevel2Options(aoi_path=tmp_path / "aoi.gpkg"),
            }
        )


def test_default_threads_are_shared_between_scene_workers(tmp_path, monkeypatch):
    monkeypatch.setattr("terravault.force_level2.os.cpu_count", lambda: 12)
    safe = tmp_path / "S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T142404.SAFE"

    shared = ForceLevel2Options(scene_workers=4, dry_run=True).config(
        input_path=safe, output_root=tmp_path
    )
    explicit = ForceLevel2Options(scene_workers=4, threads=5, dry_run=True).config(
        input_path=safe, output_root=tmp_path
    )

    assert shared.nthread == 3
    assert explicit.nthread == 5
    with pytest.raises(ValueError, match="scene_workers"):
        ForceLevel2Options(scene_workers=0)


def test_force_pipeline_cli_scene_workers_and_clip_to_roi(tmp_path):
    parser = build_parser()
    base = [
        "force-pipeline",
        "--bbox",
        "5.96",
        "45.82",
        "10.49",
        "47.81",
        "--start-date",
        "2026-07-01",
        "--output-root",
        str(tmp_path / "force"),
    ]

    defaults = parser.parse_args(base)
    args = parser.parse_args([*base, "--scene-workers", "3", "--clip-to-roi"])

    assert defaults.scene_workers == 1
    assert defaults.threads is None
    assert defaults.clip_to_roi is False
    assert args.scene_workers == 3
    assert args.clip_to_roi is True
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--clip-to-roi", "--force-aoi", "aoi.gpkg"])
