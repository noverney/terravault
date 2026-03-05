"""TerraVault – open satellite data ingestion pipeline."""

from .catalog import CatalogClient
from .downloader import AssetDownloader
from .pipeline import Pipeline
from .state import StateManager
from .storage import StorageManager

__all__ = [
    "CatalogClient",
    "AssetDownloader",
    "Pipeline",
    "StateManager",
    "StorageManager",
]

__version__ = "0.1.0"
