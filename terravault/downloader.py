"""Asset downloader with retry logic and bounded parallelism.

Implements:

* Configurable retry count with exponential back-off and jitter.
* A ``ThreadPoolExecutor`` with a bounded worker count to respect API limits.
* Deduplication – assets whose local path already exists are skipped.
"""

from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests
from tqdm import tqdm

import pystac

from .storage import StorageManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------

@dataclass
class DownloadConfig:
    """Tuning parameters for :class:`AssetDownloader`.

    Parameters
    ----------
    max_workers:
        Maximum number of concurrent download threads.
    max_retries:
        Maximum number of retry attempts per file.
    backoff_base:
        Base sleep time (seconds) for exponential back-off.
    backoff_max:
        Maximum sleep time (seconds) between retries.
    chunk_size:
        HTTP streaming chunk size in bytes.
    timeout:
        Per-request HTTP timeout in seconds.
    asset_keys:
        If non-empty only these asset keys are downloaded from each item.
        An empty list means *all* assets are downloaded.
    """

    max_workers: int = 4
    max_retries: int = 5
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    chunk_size: int = 1024 * 1024  # 1 MB
    timeout: int = 120
    asset_keys: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Download result
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    """Outcome of a single asset download attempt."""

    item_id: str
    asset_key: str
    local_path: Path
    success: bool
    skipped: bool = False  # True when the file already existed
    error: str | None = None


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------

class AssetDownloader:
    """Download assets from STAC items.

    Parameters
    ----------
    storage:
        :class:`~terravault.storage.StorageManager` used to resolve local
        paths and create directories.
    config:
        Download tuning parameters.
    session_factory:
        Optional callable that returns a ``requests.Session``.  Useful for
        injecting authentication headers or proxies.
    """

    def __init__(
        self,
        storage: StorageManager,
        config: DownloadConfig | None = None,
        session_factory: Callable[[], requests.Session] | None = None,
    ) -> None:
        self.storage = storage
        self.config = config or DownloadConfig()
        self._session_factory = session_factory or (lambda: requests.Session())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _backoff_sleep(self, attempt: int) -> None:
        """Sleep with exponential back-off and random jitter."""
        delay = min(self.config.backoff_base * (2 ** attempt), self.config.backoff_max)
        jitter = random.uniform(0, delay * 0.1)
        logger.debug("Back-off sleep %.2fs (attempt %d)", delay + jitter, attempt + 1)
        time.sleep(delay + jitter)

    def _download_url(self, url: str, dest: Path, session: requests.Session) -> None:
        """Stream *url* into *dest* with retry logic."""
        for attempt in range(self.config.max_retries):
            try:
                with session.get(
                    url,
                    stream=True,
                    timeout=self.config.timeout,
                ) as resp:
                    resp.raise_for_status()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    try:
                        with open(tmp, "wb") as fh:
                            for chunk in resp.iter_content(chunk_size=self.config.chunk_size):
                                if chunk:
                                    fh.write(chunk)
                        tmp.rename(dest)
                    except Exception:
                        tmp.unlink(missing_ok=True)
                        raise
                return  # success
            except requests.RequestException as exc:
                logger.warning(
                    "Download attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    self.config.max_retries,
                    url,
                    exc,
                )
                if attempt < self.config.max_retries - 1:
                    self._backoff_sleep(attempt)
                else:
                    raise

    def _download_asset(
        self, item: pystac.Item, asset_key: str, asset: pystac.Asset
    ) -> DownloadResult:
        """Download a single asset, returning a :class:`DownloadResult`."""
        href = asset.href
        if not href:
            return DownloadResult(
                item_id=item.id,
                asset_key=asset_key,
                local_path=Path(),
                success=False,
                error="Asset has no href",
            )

        scheme = urlparse(href).scheme.lower()
        if scheme and scheme not in {"http", "https"}:
            if scheme == "s3":
                error = (
                    "S3 asset hrefs are not supported by the requests-based downloader. "
                    "CDSE raw assets often require S3 credentials or the Sentinel Hub Process API."
                )
            else:
                error = f"Unsupported asset URL scheme: {scheme}"
            return DownloadResult(
                item_id=item.id,
                asset_key=asset_key,
                local_path=Path(),
                success=False,
                error=error,
            )

        # Determine file extension from the href or media type
        suffix = Path(href.split("?")[0]).suffix or ".tif"
        local_path = self.storage.asset_path(item, asset_key, suffix=suffix)

        if local_path.exists():
            logger.debug("Skipping existing asset %s / %s", item.id, asset_key)
            return DownloadResult(
                item_id=item.id,
                asset_key=asset_key,
                local_path=local_path,
                success=True,
                skipped=True,
            )

        logger.info("Downloading %s / %s → %s", item.id, asset_key, local_path)
        try:
            session = self._session_factory()
            self._download_url(href, local_path, session)
            return DownloadResult(
                item_id=item.id,
                asset_key=asset_key,
                local_path=local_path,
                success=True,
            )
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(
                item_id=item.id,
                asset_key=asset_key,
                local_path=local_path,
                success=False,
                error=str(exc),
            )
        finally:
            session = locals().get("session")
            close = getattr(session, "close", None)
            if callable(close):
                close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _assets_to_download(self, item: pystac.Item) -> dict[str, pystac.Asset]:
        """Return the subset of assets to download for *item*."""
        if self.config.asset_keys:
            return {k: v for k, v in item.assets.items() if k in self.config.asset_keys}
        return dict(item.assets)

    def _missing_asset_results(self, item: pystac.Item) -> list[DownloadResult]:
        """Return failure results for explicitly requested keys that are absent."""

        if not self.config.asset_keys:
            return []
        return [
            DownloadResult(
                item_id=item.id,
                asset_key=key,
                local_path=Path(),
                success=False,
                error=f"Requested asset key {key!r} is not present in the STAC item",
            )
            for key in self.config.asset_keys
            if key not in item.assets
        ]

    def download_item(self, item: pystac.Item) -> list[DownloadResult]:
        """Download all configured assets for a single STAC item.

        Downloads run sequentially within this call.  Use
        :meth:`download_items` for parallel processing across multiple items.

        Returns
        -------
        list[DownloadResult]
            One entry per asset attempted.
        """
        assets = self._assets_to_download(item)
        results = self._missing_asset_results(item)
        for key, asset in assets.items():
            results.append(self._download_asset(item, key, asset))
        return results

    def download_items(
        self,
        items: Iterable[pystac.Item],
        progress: bool = True,
    ) -> list[DownloadResult]:
        """Download assets for multiple STAC items in parallel.

        Parameters
        ----------
        items:
            Iterable of STAC items to process.
        progress:
            Show a ``tqdm`` progress bar.

        Returns
        -------
        list[DownloadResult]
            Aggregated results for all assets across all items.
        """
        item_list = list(items)
        all_results: list[DownloadResult] = []

        # Flatten (item, key, asset) triples upfront so we can show an
        # accurate progress bar and dispatch at the asset level.
        tasks: list[tuple[pystac.Item, str, pystac.Asset]] = []
        for item in item_list:
            all_results.extend(self._missing_asset_results(item))
            for key, asset in self._assets_to_download(item).items():
                tasks.append((item, key, asset))

        if not tasks:
            logger.info("No assets to download")
            return all_results

        pbar = tqdm(total=len(tasks), desc="Downloading assets", disable=not progress)

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            futures = {
                executor.submit(self._download_asset, item, key, asset): (item.id, key)
                for item, key, asset in tasks
            }
            for future in as_completed(futures):
                result = future.result()
                all_results.append(result)
                status = "skip" if result.skipped else ("ok" if result.success else "FAIL")
                pbar.set_postfix({"last": f"{result.item_id[:20]}/{result.asset_key}={status}"})
                pbar.update(1)

        pbar.close()

        successes = sum(1 for r in all_results if r.success and not r.skipped)
        skipped = sum(1 for r in all_results if r.skipped)
        failures = sum(1 for r in all_results if not r.success)
        logger.info(
            "Download complete – %d downloaded, %d skipped, %d failed",
            successes,
            skipped,
            failures,
        )
        return all_results
