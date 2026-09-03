"""TerraVault – open satellite data ingestion pipeline."""

__all__ = [
    "CDSEDownloadAuthConfig",
    "CatalogClient",
    "COMPARISON_SOURCES",
    "ComparisonConfig",
    "ComparisonProduct",
    "ComparisonResult",
    "CopernicusMonthlyConfig",
    "CopernicusMonthlyProcessor",
    "CopernicusMonthlyResult",
    "DatasetCatalog",
    "ExtractionConfig",
    "ExtractionResult",
    "FORCE_DOCKER_IMAGE",
    "FORCE_DOCKER_PLATFORM",
    "FORCE_VERSION",
    "ForceConfig",
    "ForceLevel2Config",
    "ForceLevel2Processor",
    "ForceLevel2Result",
    "ForceLevel2Status",
    "ForceDownloadOptions",
    "ForceLevel2Options",
    "ForcePipeline",
    "ForcePipelineConfig",
    "ForcePipelineDatabase",
    "ForcePipelineResult",
    "ForcePostprocessor",
    "ForceResult",
    "ForceVisualizationConfig",
    "ForceVisualizationResult",
    "ForceVisualizer",
    "HistoricalConfig",
    "HistoricalIngestor",
    "HistoricalRunResult",
    "L1CDownloadConfig",
    "L1CDownloadResult",
    "L1CProductDownloader",
    "MONTHLY_BAND_NAMES",
    "MONTHLY_INDEX_NAMES",
    "FORCE_BOA_DOMAINS",
    "FORCE_REFLECTANCE_BAND_MAP",
    "ForceMonthlyDiscoveryResult",
    "MonthlyCompositeConfig",
    "MonthlyCompositeProcessor",
    "MonthlyCompositeResult",
    "MonthlyComparisonRenderer",
    "NDVI_L2A_ASSET_KEYS",
    "VEGETATION_L2A_ASSET_KEYS",
    "VEGETATION_INDEX_DEFINITIONS",
    "SwissEOClient",
    "SwissEODiscoveryResult",
    "SwissEOImageConfig",
    "SwissEOImageProcessor",
    "SwissEOImageResult",
    "SwissEOMonthlyConfig",
    "SwissEOMonthlyProcessor",
    "SwissEOMonthlyResult",
    "AssetDownloader",
    "Pipeline",
    "PipelineConfig",
    "PipelineRunResult",
    "ProcessPatchConfig",
    "RasterExtractor",
    "RegionOfInterest",
    "RollingConfig",
    "RollingIngestor",
    "SentinelHubAuthConfig",
    "StateManager",
    "StorageManager",
    "fetch_sentinel2_patch",
    "discover_force_month",
    "discover_comparison_products",
    "discover_swisseo_month",
    "select_copernicus_scenes",
    "load_roi",
    "inspect_force_level2_status",
    "parse_utc_date",
    "run_force_pipeline",
]

__version__ = "0.1.0"


def __getattr__(name: str):
    if name == "CDSEDownloadAuthConfig":
        from .auth import CDSEDownloadAuthConfig

        return CDSEDownloadAuthConfig
    if name == "SentinelHubAuthConfig":
        from .auth import SentinelHubAuthConfig

        return SentinelHubAuthConfig
    if name == "CatalogClient":
        from .catalog import CatalogClient

        return CatalogClient
    if name == "DatasetCatalog":
        from .dataset_catalog import DatasetCatalog

        return DatasetCatalog
    if name in {"ExtractionConfig", "ExtractionResult", "RasterExtractor"}:
        from .extractor import ExtractionConfig, ExtractionResult, RasterExtractor

        return {
            "ExtractionConfig": ExtractionConfig,
            "ExtractionResult": ExtractionResult,
            "RasterExtractor": RasterExtractor,
        }[name]
    if name in {
        "FORCE_DOCKER_IMAGE",
        "FORCE_DOCKER_PLATFORM",
        "FORCE_VERSION",
        "ForceConfig",
        "ForcePostprocessor",
        "ForceResult",
    }:
        from .force import (
            FORCE_DOCKER_IMAGE,
            FORCE_DOCKER_PLATFORM,
            FORCE_VERSION,
            ForceConfig,
            ForcePostprocessor,
            ForceResult,
        )

        return {
            "FORCE_DOCKER_IMAGE": FORCE_DOCKER_IMAGE,
            "FORCE_DOCKER_PLATFORM": FORCE_DOCKER_PLATFORM,
            "FORCE_VERSION": FORCE_VERSION,
            "ForceConfig": ForceConfig,
            "ForcePostprocessor": ForcePostprocessor,
            "ForceResult": ForceResult,
        }[name]
    if name in {
        "ForceLevel2Config",
        "ForceLevel2Processor",
        "ForceLevel2Result",
        "ForceLevel2Status",
        "inspect_force_level2_status",
    }:
        from .force_level2 import (
            ForceLevel2Config,
            ForceLevel2Processor,
            ForceLevel2Result,
            ForceLevel2Status,
            inspect_force_level2_status,
        )

        return {
            "ForceLevel2Config": ForceLevel2Config,
            "ForceLevel2Processor": ForceLevel2Processor,
            "ForceLevel2Result": ForceLevel2Result,
            "ForceLevel2Status": ForceLevel2Status,
            "inspect_force_level2_status": inspect_force_level2_status,
        }[name]
    if name in {
        "ForceDownloadOptions",
        "ForceLevel2Options",
        "ForcePipeline",
        "ForcePipelineConfig",
        "ForcePipelineDatabase",
        "ForcePipelineResult",
        "run_force_pipeline",
    }:
        from .force_pipeline import (
            ForceDownloadOptions,
            ForceLevel2Options,
            ForcePipeline,
            ForcePipelineConfig,
            ForcePipelineDatabase,
            ForcePipelineResult,
            run_force_pipeline,
        )

        return {
            "ForceDownloadOptions": ForceDownloadOptions,
            "ForceLevel2Options": ForceLevel2Options,
            "ForcePipeline": ForcePipeline,
            "ForcePipelineConfig": ForcePipelineConfig,
            "ForcePipelineDatabase": ForcePipelineDatabase,
            "ForcePipelineResult": ForcePipelineResult,
            "run_force_pipeline": run_force_pipeline,
        }[name]
    if name in {
        "ForceVisualizationConfig",
        "ForceVisualizationResult",
        "ForceVisualizer",
    }:
        from .force_visualization import (
            ForceVisualizationConfig,
            ForceVisualizationResult,
            ForceVisualizer,
        )

        return {
            "ForceVisualizationConfig": ForceVisualizationConfig,
            "ForceVisualizationResult": ForceVisualizationResult,
            "ForceVisualizer": ForceVisualizer,
        }[name]
    if name in {
        "L1CDownloadConfig",
        "L1CDownloadResult",
        "L1CProductDownloader",
    }:
        from .l1c_download import (
            L1CDownloadConfig,
            L1CDownloadResult,
            L1CProductDownloader,
        )

        return {
            "L1CDownloadConfig": L1CDownloadConfig,
            "L1CDownloadResult": L1CDownloadResult,
            "L1CProductDownloader": L1CProductDownloader,
        }[name]
    if name in {
        "HistoricalConfig",
        "HistoricalIngestor",
        "HistoricalRunResult",
        "parse_utc_date",
    }:
        from .historical import (
            HistoricalConfig,
            HistoricalIngestor,
            HistoricalRunResult,
            parse_utc_date,
        )

        return {
            "HistoricalConfig": HistoricalConfig,
            "HistoricalIngestor": HistoricalIngestor,
            "HistoricalRunResult": HistoricalRunResult,
            "parse_utc_date": parse_utc_date,
        }[name]
    if name == "AssetDownloader":
        from .downloader import AssetDownloader

        return AssetDownloader
    if name == "Pipeline":
        from .pipeline import Pipeline

        return Pipeline
    if name == "PipelineConfig":
        from .pipeline import PipelineConfig

        return PipelineConfig
    if name == "PipelineRunResult":
        from .pipeline import PipelineRunResult

        return PipelineRunResult
    if name == "ProcessPatchConfig":
        from .process_api import ProcessPatchConfig

        return ProcessPatchConfig
    if name in {
        "FORCE_BOA_DOMAINS",
        "FORCE_REFLECTANCE_BAND_MAP",
        "ForceMonthlyDiscoveryResult",
        "MONTHLY_BAND_NAMES",
        "MONTHLY_INDEX_NAMES",
        "MonthlyCompositeConfig",
        "MonthlyCompositeProcessor",
        "MonthlyCompositeResult",
        "discover_force_month",
    }:
        from .monthly import (
            FORCE_BOA_DOMAINS,
            FORCE_REFLECTANCE_BAND_MAP,
            MONTHLY_BAND_NAMES,
            MONTHLY_INDEX_NAMES,
            ForceMonthlyDiscoveryResult,
            MonthlyCompositeConfig,
            MonthlyCompositeProcessor,
            MonthlyCompositeResult,
            discover_force_month,
        )

        return {
            "FORCE_BOA_DOMAINS": FORCE_BOA_DOMAINS,
            "FORCE_REFLECTANCE_BAND_MAP": FORCE_REFLECTANCE_BAND_MAP,
            "ForceMonthlyDiscoveryResult": ForceMonthlyDiscoveryResult,
            "MONTHLY_BAND_NAMES": MONTHLY_BAND_NAMES,
            "MONTHLY_INDEX_NAMES": MONTHLY_INDEX_NAMES,
            "MonthlyCompositeConfig": MonthlyCompositeConfig,
            "MonthlyCompositeProcessor": MonthlyCompositeProcessor,
            "MonthlyCompositeResult": MonthlyCompositeResult,
            "discover_force_month": discover_force_month,
        }[name]
    if name == "VEGETATION_INDEX_DEFINITIONS":
        from .vegetation import VEGETATION_INDEX_DEFINITIONS

        return VEGETATION_INDEX_DEFINITIONS
    if name in {
        "CopernicusMonthlyConfig",
        "CopernicusMonthlyProcessor",
        "CopernicusMonthlyResult",
        "select_copernicus_scenes",
    }:
        from .copernicus_monthly import (
            CopernicusMonthlyConfig,
            CopernicusMonthlyProcessor,
            CopernicusMonthlyResult,
            select_copernicus_scenes,
        )

        return {
            "CopernicusMonthlyConfig": CopernicusMonthlyConfig,
            "CopernicusMonthlyProcessor": CopernicusMonthlyProcessor,
            "CopernicusMonthlyResult": CopernicusMonthlyResult,
            "select_copernicus_scenes": select_copernicus_scenes,
        }[name]
    if name in {
        "SwissEOMonthlyConfig",
        "SwissEOMonthlyProcessor",
        "SwissEOMonthlyResult",
    }:
        from .swisseo_monthly import (
            SwissEOMonthlyConfig,
            SwissEOMonthlyProcessor,
            SwissEOMonthlyResult,
        )

        return {
            "SwissEOMonthlyConfig": SwissEOMonthlyConfig,
            "SwissEOMonthlyProcessor": SwissEOMonthlyProcessor,
            "SwissEOMonthlyResult": SwissEOMonthlyResult,
        }[name]
    if name in {
        "COMPARISON_SOURCES",
        "ComparisonConfig",
        "ComparisonProduct",
        "ComparisonResult",
        "MonthlyComparisonRenderer",
        "discover_comparison_products",
    }:
        from .comparison import (
            COMPARISON_SOURCES,
            ComparisonConfig,
            ComparisonProduct,
            ComparisonResult,
            MonthlyComparisonRenderer,
            discover_comparison_products,
        )

        return {
            "COMPARISON_SOURCES": COMPARISON_SOURCES,
            "ComparisonConfig": ComparisonConfig,
            "ComparisonProduct": ComparisonProduct,
            "ComparisonResult": ComparisonResult,
            "MonthlyComparisonRenderer": MonthlyComparisonRenderer,
            "discover_comparison_products": discover_comparison_products,
        }[name]
    if name in {
        "SwissEOClient",
        "SwissEODiscoveryResult",
        "SwissEOImageConfig",
        "SwissEOImageProcessor",
        "SwissEOImageResult",
        "discover_swisseo_month",
    }:
        from .swisseo import (
            SwissEOClient,
            SwissEODiscoveryResult,
            SwissEOImageConfig,
            SwissEOImageProcessor,
            SwissEOImageResult,
            discover_swisseo_month,
        )

        return {
            "SwissEOClient": SwissEOClient,
            "SwissEODiscoveryResult": SwissEODiscoveryResult,
            "SwissEOImageConfig": SwissEOImageConfig,
            "SwissEOImageProcessor": SwissEOImageProcessor,
            "SwissEOImageResult": SwissEOImageResult,
            "discover_swisseo_month": discover_swisseo_month,
        }[name]
    if name in {
        "NDVI_L2A_ASSET_KEYS",
        "VEGETATION_L2A_ASSET_KEYS",
        "RegionOfInterest",
        "RollingConfig",
        "RollingIngestor",
        "load_roi",
    }:
        from .rolling import (
            NDVI_L2A_ASSET_KEYS,
            VEGETATION_L2A_ASSET_KEYS,
            RegionOfInterest,
            RollingConfig,
            RollingIngestor,
            load_roi,
        )

        return {
            "NDVI_L2A_ASSET_KEYS": NDVI_L2A_ASSET_KEYS,
            "VEGETATION_L2A_ASSET_KEYS": VEGETATION_L2A_ASSET_KEYS,
            "RegionOfInterest": RegionOfInterest,
            "RollingConfig": RollingConfig,
            "RollingIngestor": RollingIngestor,
            "load_roi": load_roi,
        }[name]
    if name == "fetch_sentinel2_patch":
        from .process_api import fetch_sentinel2_patch

        return fetch_sentinel2_patch
    if name == "StateManager":
        from .state import StateManager

        return StateManager
    if name == "StorageManager":
        from .storage import StorageManager

        return StorageManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
