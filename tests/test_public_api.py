"""Tests for documented package-level imports."""

from __future__ import annotations

import platform


def test_readme_pipeline_imports_are_public():
    from terravault import CDSEDownloadAuthConfig, Pipeline, PipelineConfig

    assert CDSEDownloadAuthConfig is not None
    assert Pipeline is not None
    assert PipelineConfig is not None


def test_historical_imports_are_public():
    from terravault import HistoricalConfig, HistoricalIngestor, HistoricalRunResult

    assert HistoricalConfig is not None
    assert HistoricalIngestor is not None
    assert HistoricalRunResult is not None


def test_ndvi_profile_is_public():
    from terravault import NDVI_L2A_ASSET_KEYS

    assert NDVI_L2A_ASSET_KEYS == (
        "B04_10m",
        "B08_10m",
        "SCL_20m",
        "CLD_20m",
    )


def test_local_dataset_query_and_extraction_imports_are_public():
    from terravault import (
        DatasetCatalog,
        ExtractionConfig,
        ExtractionResult,
        RasterExtractor,
    )

    assert DatasetCatalog is not None
    assert ExtractionConfig is not None
    assert ExtractionResult is not None
    assert RasterExtractor is not None


def test_force_postprocessing_imports_are_public():
    from terravault import (
        FORCE_DOCKER_IMAGE,
        FORCE_DOCKER_PLATFORM,
        FORCE_VERSION,
        ForceConfig,
        ForcePostprocessor,
        ForceResult,
    )

    assert FORCE_VERSION == "3.10.04"
    expected_image = (
        "terravault/force:3.10.04-arm64"
        if platform.machine().casefold() in {"arm64", "aarch64"}
        else "davidfrantz/force:3.10.04"
    )
    assert FORCE_DOCKER_IMAGE == expected_image
    assert FORCE_DOCKER_PLATFORM == (
        "linux/arm64"
        if platform.machine().casefold() in {"arm64", "aarch64"}
        else "linux/amd64"
    )
    assert ForceConfig is not None
    assert ForcePostprocessor is not None
    assert ForceResult is not None


def test_force_visualization_imports_are_public():
    from terravault import (
        ForceVisualizationConfig,
        ForceVisualizationResult,
        ForceVisualizer,
    )

    assert ForceVisualizationConfig is not None
    assert ForceVisualizationResult is not None
    assert ForceVisualizer is not None


def test_native_force_level2_imports_are_public():
    from terravault import (
        ForceLevel2Config,
        ForceLevel2Processor,
        ForceLevel2Result,
        ForceLevel2Status,
        L1CDownloadConfig,
        L1CDownloadResult,
        L1CProductDownloader,
        inspect_force_level2_status,
    )

    assert ForceLevel2Config is not None
    assert ForceLevel2Processor is not None
    assert ForceLevel2Result is not None
    assert ForceLevel2Status is not None
    assert inspect_force_level2_status is not None
    assert L1CDownloadConfig is not None
    assert L1CDownloadResult is not None
    assert L1CProductDownloader is not None
