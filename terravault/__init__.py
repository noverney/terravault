"""TerraVault – open satellite data ingestion pipeline."""

__all__ = [
    "CDSEDownloadAuthConfig",
    "CatalogClient",
    "DatasetCatalog",
    "ExtractionConfig",
    "ExtractionResult",
    "FORCE_DOCKER_IMAGE",
    "FORCE_DOCKER_PLATFORM",
    "FORCE_VERSION",
    "ForceConfig",
    "ForcePostprocessor",
    "ForceResult",
    "HistoricalConfig",
    "HistoricalIngestor",
    "HistoricalRunResult",
    "NDVI_L2A_ASSET_KEYS",
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
    "load_roi",
    "parse_utc_date",
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
        "NDVI_L2A_ASSET_KEYS",
        "RegionOfInterest",
        "RollingConfig",
        "RollingIngestor",
        "load_roi",
    }:
        from .rolling import (
            NDVI_L2A_ASSET_KEYS,
            RegionOfInterest,
            RollingConfig,
            RollingIngestor,
            load_roi,
        )

        return {
            "NDVI_L2A_ASSET_KEYS": NDVI_L2A_ASSET_KEYS,
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
