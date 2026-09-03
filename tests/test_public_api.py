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


def test_monthly_vegetation_api_is_public():
    from terravault import (
        FORCE_BOA_DOMAINS,
        FORCE_REFLECTANCE_BAND_MAP,
        MONTHLY_BAND_NAMES,
        MONTHLY_INDEX_NAMES,
        ForceMonthlyDiscoveryResult,
        COMPARISON_SOURCES,
        ComparisonConfig,
        ComparisonProduct,
        ComparisonResult,
        CopernicusMonthlyConfig,
        CopernicusMonthlyProcessor,
        CopernicusMonthlyResult,
        MonthlyComparisonRenderer,
        SwissEOClient,
        SwissEODiscoveryResult,
        SwissEOImageConfig,
        SwissEOImageProcessor,
        SwissEOImageResult,
        SwissEOMonthlyConfig,
        SwissEOMonthlyProcessor,
        SwissEOMonthlyResult,
        VEGETATION_L2A_ASSET_KEYS,
        VEGETATION_INDEX_DEFINITIONS,
        MonthlyCompositeConfig,
        MonthlyCompositeProcessor,
        MonthlyCompositeResult,
        discover_force_month,
        discover_comparison_products,
        discover_swisseo_month,
    )

    assert FORCE_BOA_DOMAINS[6:9] == ("BROADNIR", "NIR", "SWIR1")
    assert FORCE_REFLECTANCE_BAND_MAP["B08"] == ("BROADNIR", 7)
    assert MONTHLY_BAND_NAMES == ("B03", "B04", "B05", "B08", "B8A", "B11")
    assert MONTHLY_INDEX_NAMES == ("NDVI", "GNDVI", "NDRE", "NDWI")
    assert VEGETATION_INDEX_DEFINITIONS["NDWI"]["numerator_negative"] == "B11"
    assert ForceMonthlyDiscoveryResult is not None
    assert MonthlyCompositeConfig is not None
    assert MonthlyCompositeProcessor is not None
    assert MonthlyCompositeResult is not None
    assert discover_force_month is not None
    assert SwissEOClient is not None
    assert SwissEODiscoveryResult is not None
    assert SwissEOImageConfig is not None
    assert SwissEOImageProcessor is not None
    assert SwissEOImageResult is not None
    assert discover_swisseo_month is not None
    assert VEGETATION_L2A_ASSET_KEYS == (
        "B03_10m",
        "B04_10m",
        "B05_20m",
        "B08_10m",
        "B8A_20m",
        "B11_20m",
        "SCL_20m",
        "CLD_20m",
    )
    assert CopernicusMonthlyConfig is not None
    assert CopernicusMonthlyProcessor is not None
    assert CopernicusMonthlyResult is not None
    assert SwissEOMonthlyConfig is not None
    assert SwissEOMonthlyProcessor is not None
    assert SwissEOMonthlyResult is not None
    assert COMPARISON_SOURCES == ("copernicus", "force", "swisseo")
    assert ComparisonConfig is not None
    assert ComparisonProduct is not None
    assert ComparisonResult is not None
    assert MonthlyComparisonRenderer is not None
    assert discover_comparison_products is not None


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


def test_high_level_force_pipeline_imports_are_public():
    from terravault import (
        ForceDownloadOptions,
        ForceLevel2Options,
        ForcePipeline,
        ForcePipelineConfig,
        ForcePipelineDatabase,
        ForcePipelineResult,
        run_force_pipeline,
    )

    assert ForceDownloadOptions is not None
    assert ForceLevel2Options is not None
    assert ForcePipeline is not None
    assert ForcePipelineConfig is not None
    assert ForcePipelineDatabase is not None
    assert ForcePipelineResult is not None
    assert run_force_pipeline is not None
