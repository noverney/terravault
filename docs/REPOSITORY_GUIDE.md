# TerraVault repository guide

This file records the codebase map and the verification workflow so future
changes do not require rediscovering the repository.

## Code map

| Path | Responsibility |
|---|---|
| `terravault/catalog.py` | CDSE STAC discovery, time and cloud filtering |
| `terravault/auth.py` | CDSE account and Sentinel Hub OAuth token providers |
| `terravault/downloader.py` | Bounded HTTP asset downloads, retries and results |
| `terravault/storage.py` | Collision-safe local metadata/asset paths |
| `terravault/state.py` | SQLite and JSON item-ID deduplication |
| `terravault/pipeline.py` | Discovery → metadata → download → completion state |
| `terravault/process_api.py` | Sentinel Hub Process API requests and validation |
| `terravault/s3_downloader.py` | Resumable native S3 transfers and quota classification |
| `terravault/rolling_state.py` | Durable per-job/per-asset SQLite queue and events |
| `terravault/rolling.py` | Latest-then-follow native rolling ingestion |
| `terravault/historical.py` | Windowed historical backfill and progress |
| `terravault/dataset_catalog.py` | Top-level DuckDB raster-piece catalogue |
| `terravault/extractor.py` | Block-wise VRT mosaics and stitched multiband COG queries |
| `terravault/cli.py` | `run`, `watch`, `historic`, `query`, `extract` and `collections` |
| `examples/switzerland_patch/` | Small-patch and country-scale runnable examples |

The core `Pipeline` is a STAC asset ingester. The Process API helpers are a
separate path for generated subsets/mosaics. Do not treat the synchronous
Process API as a native-resolution country archive.

## Development setup

```bash
conda activate terra
python -m pip install -e ".[dev,overview,s3]"
python -m pytest
ruff check .
python -m compileall -q terravault examples
```

The `overview` extra installs Pillow for the public-thumbnail mosaic. The
`raster` extra installs rasterio for analysis-ready raster inspection.

## Verification levels

1. Unit: `python -m pytest`.
2. Syntax/CLI: compile all modules and run `python -m terravault.cli --help`.
3. Live catalogue smoke test:

   ```bash
   python examples/switzerland_patch/retrieve_patch.py \
     --no-download \
     --lookback-hours 168 \
     --state-db /tmp/terravault-smoke.db \
     --storage-root /tmp/terravault-smoke
   ```

4. Credential-free country overview:

   ```bash
   python examples/switzerland_patch/build_switzerland_overview.py
   ```

5. Process API request validation without spending processing units:

   ```bash
   python examples/switzerland_patch/fetch_switzerland_snapshot.py --dry-run
   ```

6. Authenticated country snapshot:

   ```bash
   python examples/switzerland_patch/fetch_switzerland_snapshot.py
   ```

7. Synthetic adjacent-tile COG extraction:

   ```bash
   python -m pytest -q tests/test_extractor.py
   ```

## Invariants

- A failed or missing requested asset is not marked ingested.
- Resume searches overlap the previous cursor; item-ID deduplication makes
  repeated catalogue results harmless.
- Each STAC item has its own directory below acquisition date and MGRS tile.
- Rolling/historical assets use Hive-style collection/date/tile/item
  partitions and absolute paths in top-level `dataset.duckdb`.
- SQLite is the transactional queue; DuckDB is the analytical dataset index.
- Stitched country/intersection rasters are derived query products; source
  pieces remain immutable and partitioned.
- Extraction pixels stay in GDAL's bounded block pipeline and never become a
  country-sized Python array.
- Logs rotate below `STORAGE_ROOT/_terravault/logs/` and never include secrets.
- Process API width and height never exceed 2500 pixels.
- Secrets belong in `.env` or a secret manager and must not be committed.
- The Switzerland bounding box is discovery/test convenience. Production
  selection and output masking use the versioned national polygon.

## Format boundary

Native CDSE Sentinel-2 raster assets are georeferenced JP2 files. DuckDB
catalogues native pieces and any derived GeoTIFF/COG pieces using the same
spatial/time fields. The Process API country snapshot is a separate,
reduced-resolution GeoTIFF mosaic workflow.
