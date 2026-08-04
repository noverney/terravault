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
| `terravault/force.py` | Durable L2A → FORCE external-feature cube/mosaic bridge (not BOA/QAI) |
| `terravault/l1c_download.py` | Resumable complete L1C SAFE-tree or Product-ZIP acquisition from a saved STAC Item |
| `terravault/force_level2.py` | Native FORCE L2PS from complete L1C into atomically published, isolated per-SAFE BOA/QAI/OVV products |
| `terravault/force_visualization.py` | NDVI COGs, quicklooks, and raw/CDSE/FORCE-QAI mask diagnostics |
| `terravault/cli.py` | `run`, `watch`, `historic`, `query`, `extract`, `force`, `force-level2`, `force-status`, `force-visualize` and `collections` |
| `examples/switzerland_patch/` | Small-patch and country-scale runnable examples |
| `examples/query/` | Local DuckDB and stitched-COG API examples |
| `examples/postprocessing/` | FORCE postprocessing orchestration |
| `vendor/force/` | FORCE v3.10.04 source pinned as a Git submodule |

The core `Pipeline` is a STAC asset ingester. The Process API helpers are a
separate path for generated subsets/mosaics. Do not treat the synchronous
Process API as a native-resolution country archive. Likewise, keep the L2A
external-feature bridge separate from native FORCE L2PS: only the latter owns
atmospheric/cloud processing and emits BOA/QAI.

## Development setup

```bash
conda activate terra
python -m pip install -e ".[dev,overview,s3]"
python -m pytest
ruff check .
python -m compileall -q terravault examples
```

The `overview` extra installs Pillow for the public-thumbnail mosaic; the
`visualization` extra installs it for FORCE comparison plots. The `raster`
extra installs rasterio for analysis-ready raster inspection.

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

8. FORCE command plan and container integration:

   ```bash
   terravault force \
     --input satellite_data/switzerland_ndvi/exports/zurich_ndvi_inputs_example.tif \
     --output-root satellite_data/switzerland_ndvi/force \
     --runtime docker \
     --dry-run
   ```

9. FORCE NDVI/PNG visualization:

   ```bash
   python -m pytest -q tests/test_force_visualization.py
   ```

10. Complete L1C acquisition and native FORCE L2PS state/output contracts:

    ```bash
    python -m pytest -q tests/test_l1c_download.py tests/test_force_level2.py
    ```

11. Native L2PS command plan for a locally available complete SAFE:

    ```bash
    terravault force-level2 \
      --input /data/l1c/S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T142404.SAFE.zip \
      --output-root /tmp/terravault-force-l2 \
      --runtime docker \
      --dry-run
    ```

    A dry run validates the SAFE and records the planned commands in terminal
    output; it does not prove a real atmospheric/cloud processing run. Do not
    use `--dry-run` with `--stac-item`, because that route must acquire the
    SAFE first.

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
- Selected TerraVault L2A assets enter FORCE as external features, never as
  FORCE BOA/QAI ARD. Native FORCE Level-2 accepts only a complete Sentinel-2
  L1C SAFE/SAFE ZIP with product/granule metadata and every band including B10.
- Native L2PS uses the stable Swiss grid by default (EPSG:2056, 10 m, 30 km
  tiles, 5.5° E / 48.0° N origin). Missing DEM input is permitted only with a
  warning, disabled topographic correction, and reduced shadow/atmospheric
  quality.
- `_terravault/force-l2/cube.json` makes the native grid immutable for an
  output root; even `--overwrite` cannot change it.
- Native FORCE outputs are isolated below
  `level2/products/<SAFE-stem>/`. Same-date SAFE products do not merge. A retry
  discards only that SAFE's partial attempt and publishes only after cube,
  CRS/grid, BOA/QAI/OVV, and mosaic validation.
- Complete S3 SAFE downloads use a hidden staging tree and retain the prior
  verified SAFE until exact file-list, size, per-object SHA-256, and SAFE
  structure checks pass.
- `force-level2` is a durable per-product primitive. It does not create a
  pooled acquisition/national FORCE mosaic and is not yet dispatched by
  `watch` or `historic`.
- FORCE jobs verify physical chips and mosaics in addition to process exit
  codes, and reuse an identical completed input by fingerprint.
- FORCE visualizations keep pixel calculation in GDAL, retain a georeferenced
  Float32 COG and record masks, colors, statistics and commands. In a
  three-panel diagnostic, raw, CDSE-masked, and FORCE-QAI-masked panels all use
  the same L2A B04/B08 pixels; the FORCE panel must not silently switch to BOA.
- The default FORCE visualization mask is `0x031F`: nodata, every non-clear
  cloud state, cloud shadow, snow, subzero, and saturation. QAI is aligned with
  nearest-neighbour resampling and date/sensor provenance is checked when
  available.
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
