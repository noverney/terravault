"""Tests for documented package-level imports."""

from __future__ import annotations


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
    assert FORCE_DOCKER_IMAGE == "davidfrantz/force:3.10.04"
    assert FORCE_DOCKER_PLATFORM == "linux/amd64"
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
