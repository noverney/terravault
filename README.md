# terravault

> **Open satellite data ingestion pipeline** — STAC-based discovery, deduplication, and download for Sentinel and future satellite imagery.

TerraVault is a lightweight Python toolkit that connects to any STAC-compliant catalog, discovers new satellite scenes for a configurable area of interest, persists their metadata locally, and downloads the assets you need — all while keeping track of what has already been ingested so each run processes only *new* data.

CDSE note: there is no single interchangeable "API key" for these workflows.
Native product ingestion uses S3 access/secret keys, while Sentinel Hub
processing uses an OAuth client ID/secret to obtain short-lived API access
tokens. See [Copernicus credentials](#copernicus-credentials) below.

---

## Features

| Feature | Details |
|---|---|
| **STAC-native discovery** | Uses `pystac_client` against any STAC API (default: Copernicus Data Space) |
| **Switzerland out of the box** | Pre-configured bounding box `[5.96, 45.82, 10.49, 47.81]` covering Switzerland |
| **Cloud-cover filter** | Skips scenes above a configurable threshold (default: 20 %) |
| **State tracking** | SQLite (default) or JSON state file — each run ingests only *new* scenes |
| **Organised storage** | `satellite_data/sentinel2/YYYY/MM/DD/TILE_ID/ITEM_ID/` directory tree |
| **Retry & back-off** | Exponential back-off with jitter on transient download failures |
| **Bounded parallelism** | Configurable thread-pool (`max_workers=4` default) respects API limits |
| **Asset filtering** | Download all assets or a named subset (e.g. `B04`, `B08`) |
| **Restartable rolling ingest** | Native CDSE S3 assets, per-asset SQLite queue, resumable transfers and failure sidecars |
| **DuckDB dataset catalogue** | Top-level spatial/time index for every partitioned local raster piece |
| **Memory-bounded raster extraction** | Query intersecting pieces and stream one aligned multiband COG through GDAL |
| **FORCE postprocessing** | Pinned FORCE submodule, Docker/native bridge, restartable feature-cube imports and mosaics |
| **Operational logs** | Automatic rotating progress, storage, retry, quota and completion logs |
| **Explicit ROI** | Rolling discovery accepts a WGS84 bbox or Polygon/MultiPolygon GeoJSON |
| **Historical backfill** | Windowed progress, durable cursor, quota waits and retired jobs |
| **Compact NDVI profile** | Native 10 m B04/B08 plus SCL/CLD masks via `--asset-profile ndvi` |
| **CLI** | One-shot, rolling, historical, metadata-query and stitched-extraction commands |

---

## Installation

### Install from this repository (recommended for local use)

```bash
cd /path/to/terravault
git submodule update --init --recursive
conda activate terra
python -m pip install --upgrade pip
python -m pip install -e .
```

Install with extras:

```bash
python -m pip install -e ".[raster]"    # adds rasterio
python -m pip install -e ".[overview]"  # adds Pillow for the country quicklook
python -m pip install -e ".[s3]"        # adds boto3 for native rolling ingestion
python -m pip install -e ".[postgres]"  # adds psycopg2-binary
python -m pip install -e ".[dev]"       # adds pytest + responses for development
```

Verify the install:

```bash
terravault --help
python -c "import terravault; print(terravault.__version__)"
```

### Install from PyPI

```bash
pip install terravault
```

Optional extras:

```bash
pip install "terravault[raster]"    # adds rasterio
pip install "terravault[overview]"  # adds Pillow for the country quicklook
pip install "terravault[s3]"        # adds boto3 for native rolling ingestion
pip install "terravault[postgres]"  # adds psycopg2-binary
pip install "terravault[dev]"       # adds pytest + responses for development
```

---

## Copernicus credentials

TerraVault supports two separate Copernicus Data Space Ecosystem (CDSE)
access routes. Create the credential that matches the command you intend to
run; the two credential pairs are not interchangeable.

| Credential | Create it here | Used for |
|---|---|---|
| **S3 access key + secret key** | [CDSE S3 Credentials Manager](https://eodata-s3keysmanager.dataspace.copernicus.eu/panel/s3-credentials) | `terravault watch` and `terravault historic`; downloads original native-resolution Sentinel files into the local partitioned dataset |
| **Sentinel Hub OAuth client ID + client secret** | [CDSE Dashboard → User Settings → OAuth clients](https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings) | Process API examples such as `fetch_raw_patch.py` and `fetch_switzerland_snapshot.py`; requests server-side subsets, reprojection, mosaicking or derived products |

For native S3 ingestion, sign in to the S3 Credentials Manager, choose
**Add Credentials**, select an expiry date and save both values in `.env`:

```dotenv
TERRAVAULT_CDSE_S3_ACCESS_KEY=your_s3_access_key
TERRAVAULT_CDSE_S3_SECRET_KEY=your_s3_secret_key
TERRAVAULT_CDSE_S3_ENDPOINT=https://eodata.dataspace.copernicus.eu
TERRAVAULT_CDSE_S3_REGION=default
```

For Sentinel Hub processing, open the account settings page, create an OAuth
client and save its client ID and secret in `.env`:

```dotenv
TERRAVAULT_CDSE_SH_CLIENT_ID=your_sentinel_hub_oauth_client_id
TERRAVAULT_CDSE_SH_CLIENT_SECRET=your_sentinel_hub_oauth_client_secret
```

Both portals display a newly created secret only once. Copy it immediately,
keep `.env` private and never commit real credentials. TerraVault exchanges
the Sentinel Hub client credentials for a short-lived bearer access token
automatically; do not paste that bearer token into the S3 fields.

Neither credential is intrinsically faster because it selects a different
data path:

- S3 is normally the appropriate high-throughput route for rolling or
  historical downloads of complete, original products and native bands.
- Sentinel Hub can be quicker and transfer much less data for a small area,
  a few bands or a server-computed result, but processing-unit quotas apply.

The official references are the
[CDSE S3 access guide](https://documentation.dataspace.copernicus.eu/APIs/S3.html)
and
[Sentinel Hub authentication guide](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/Authentication.html).

---

## Quick start

### Python API

```python
import os

from terravault import CDSEDownloadAuthConfig, Pipeline, PipelineConfig
from terravault.downloader import DownloadConfig

cfg = PipelineConfig(
    collections=["sentinel-2-l2a"],
    max_cloud_cover=20.0,
    lookback_hours=48,           # look back 48 h on first run
    asset_keys=["B04", "B08"],   # download only red + NIR bands
    download=DownloadConfig(max_workers=4),
    auth=CDSEDownloadAuthConfig(
        username=os.environ["TERRAVAULT_CDSE_USERNAME"],
        password=os.environ["TERRAVAULT_CDSE_PASSWORD"],
    ),
    state_db="terravault_state.db",
    storage_root="satellite_data",
)

result = Pipeline(cfg).run()

print(f"Discovered : {result.items_discovered}")
print(f"Processed  : {result.items_processed}")
print(f"Downloaded : {result.downloads_ok}")
print(f"Failed     : {result.downloads_failed}")
```

### Metadata-only run (no download)

```python
result = Pipeline(cfg).run(download=False)
```

### Small Switzerland patch test script

For a ready-to-run retrieval test over a small Switzerland AOI (Zurich area),
use:

```bash
python examples/switzerland_patch/retrieve_patch.py
```

Outputs are saved under:

```text
examples/switzerland_patch/data/
```

### Raw Sentinel-2 patch fetch via Process API

For an actual raster subset instead of STAC asset download, create a Sentinel Hub OAuth client in the CDSE dashboard and run:

```bash
cp .env.example .env
python examples/switzerland_patch/fetch_raw_patch.py \
  --time-from 2024-06-01T00:00:00Z \
  --time-to 2024-06-30T23:59:59Z \
  --bands B04 B08
```

This writes a GeoTIFF patch to `examples/switzerland_patch/data/raw_patch.tif`.

### Switzerland-wide one-date test and rolling plan

Build a real visual overview from public thumbnails without credentials:

```bash
python -m pip install -e ".[overview]"
python examples/switzerland_patch/build_switzerland_overview.py
```

Generate all-layer country Process API requests without spending processing
units, then run them after configuring a valid Sentinel Hub OAuth client:

```bash
python examples/switzerland_patch/fetch_switzerland_snapshot.py --dry-run
python examples/switzerland_patch/fetch_switzerland_snapshot.py
```

The architecture, data-volume tradeoffs, native S3 ingestion plan and
operational runbook are in
[`docs/SWITZERLAND_SENTINEL2_PIPELINE.md`](docs/SWITZERLAND_SENTINEL2_PIPELINE.md).

### Restartable rolling ingestion

After adding CDSE S3 keys to `.env`, the region is the only required argument:

```bash
python -m pip install -e ".[s3]"
terravault watch --bbox 5.96 45.82 10.49 47.81
```

Use an exact Swiss GeoJSON polygon with `--roi` for production. The default
profile retains each L2A layer at its highest/native resolution (10, 20 or
60 m), plus provenance XML. The first run fetches the latest product whose
footprint is at least 90% of the largest recently observed footprint for each
intersecting MGRS tile; this avoids choosing a newer edge-of-swath sliver.
Later cycles fetch every new product. Stop with
Ctrl-C/SIGTERM and restart the same command: the durable discovery watermark
catches up after downtime, completed assets are skipped and partial S3 objects
resume.

See [`docs/ROLLING_INGESTION.md`](docs/ROLLING_INGESTION.md) for credentials,
asset keys, cron, state tables, retry behaviour and operations.
See [`docs/DATASET_STORAGE.md`](docs/DATASET_STORAGE.md) for the partitioned
layout, DuckDB schema, raster queries and log retention.

Use the downloaded dataset directly from Python without contacting
Copernicus:

```bash
python examples/query/local_dataset_api.py \
  --dataset-db satellite_data/switzerland_ndvi/dataset.duckdb \
  --bbox 8.45 47.20 8.65 47.35
```

The example uses the public `DatasetCatalog` API to return intersecting local
paths and metadata. Add `--output exports/zurich_ndvi_inputs.tif` to stream a
stitched COG, or add `--dry-run` with the output argument to inspect its size
without writing pixels. See
[`examples/query/local_dataset_api.py`](examples/query/local_dataset_api.py) for the full
Python code.

Query a region and return one stitched RGB image while keeping memory bounded:

```bash
terravault extract \
  --dataset-db satellite_data/dataset.duckdb \
  --bbox 8.45 47.20 8.65 47.35 \
  --asset-keys B04_10m B03_10m B02_10m \
  --output exports/zurich_rgb.tif \
  --target-crs EPSG:2056
```

The output is a tiled Cloud Optimized GeoTIFF plus a manifest recording every
source and the feature-to-band mapping. Country-scale requests can be inspected
first with `--dry-run`; GDAL does the pixel work block by block under
`--warp-memory-mib`. See
[`docs/RASTER_QUERY_AND_EXTRACTION.md`](docs/RASTER_QUERY_AND_EXTRACTION.md).

### FORCE postprocessing

FORCE is pinned as the `vendor/force` Git submodule. TerraVault imports its
stitched L2A band products through FORCE's supported external-feature
datacube path; it does not mislabel selected L2A bands as FORCE Level-2 ARD.
Docker is the default portable runtime. FORCE is Linux software; on macOS,
TerraVault explicitly runs the pinned `linux/amd64` image rather than trying
to compile or link FORCE against macOS libraries.

Plan the current Swiss-wide 10 m B04/B08/SCL/CLD extraction:

```bash
python examples/postprocessing/force_switzerland.py --dry-run
```

Run the extraction, FORCE tiling and mosaic:

```bash
python examples/postprocessing/force_switzerland.py --runtime docker
```

For an existing stitched COG, run `terravault force` with `--input INPUT.tif`
and `--output-root OUTPUT_DIR`. Job manifests, input hashes, attempts, exact
commands, chips and mosaics are persisted, so an interrupted or repeated job
can be safely rerun. See
[`docs/FORCE_POSTPROCESSING.md`](docs/FORCE_POSTPROCESSING.md).

### Historical backfill

Backfill the complete native profile from a specified start date:

```bash
terravault historic \
  --bbox 5.96 45.82 10.49 47.81 \
  --start-date 2024-01-01
```

The worker progresses through durable one-day windows, shows overall and item
progress, resumes after interruption, honours S3 `Retry-After` quota responses
and retires a job after eight unsuccessful quota attempts. Details are in
[`docs/HISTORICAL_INGESTION.md`](docs/HISTORICAL_INGESTION.md).

---

## Command-line interface

```text
Usage: terravault [OPTIONS] COMMAND [ARGS]...

Commands:
  run          Execute one pipeline ingestion run
  watch        Run restartable rolling ingestion for an explicit region
  historic     Gradually backfill complete native data from a start date
  query        Query local georeferenced raster pieces from dataset DuckDB
  extract      Stream intersecting pieces into one multiband COG
  force        Import a stitched raster into a FORCE feature datacube
  collections  List collections available in the STAC catalog
```

### Examples

```bash
# Metadata-only dry run (no assets downloaded)
terravault run --no-download

# Download red and NIR bands only
terravault run --asset-keys B04 B08

# Authenticate CDSE product downloads from env vars
export TERRAVAULT_CDSE_USERNAME=...
export TERRAVAULT_CDSE_PASSWORD=...
terravault run --asset-keys thumbnail

# Or copy the template and load credentials from a local .env file
cp .env.example .env
terravault run --env-file .env --asset-keys thumbnail

# Use a different STAC catalog
terravault run --catalog-url https://stac.dataspace.copernicus.eu/v1

# Increase lookback window to 7 days on first run
terravault run --lookback-hours 168

# List all collections in the catalog
terravault collections

# Validate rolling discovery without S3 downloads
terravault watch --bbox 5.96 45.82 10.49 47.81 \
  --asset-profile metadata-only --once
```

Full option reference:

```text
terravault run --help
```

---

## Storage layout

Rolling and historical data is organised into Hive-style partitions:

```
satellite_data/
  dataset.duckdb
  _terravault/logs/
  pieces/
    collection=sentinel-2-l2a/
      year=2024/
        month=06/
          day=15/
            tile=32TNT/
              item=S2A_MSIL2A_.../
              scene_metadata.json   ← full STAC item JSON
              job_status.json
              B04_10m.jp2
              SCL_20m.jp2
```

Every leaf is one product, so large raster pieces are never accumulated in a
single directory. `dataset.duckdb` records WGS84 footprint/time, resolution,
CRS/raster metadata, status, checksum and absolute local path. Query pieces
with `terravault query --bbox ...` or SQL against the `raster_pieces` view.
`terravault extract` creates a requested intersection as a derived stitched COG
without replacing the partitioned source layout.

---

## State tracking

TerraVault stores ingestion state in a local SQLite database (`terravault_state.db` by default).

* Each processed item ID is recorded so it is never ingested twice.
* The last-processed timestamp drives the search window on subsequent runs.
* A configurable resume overlap (72 hours by default) catches catalogue delays.
* Failed or missing requested assets are not marked processed and are retried.

A lightweight JSON alternative is also available:

```python
from terravault.state import JSONStateManager

state = JSONStateManager("my_state.json")
state.mark_processed("S2A_...", item_datetime)
print(state.last_processed)
```

---

## Scheduling

### cron (rolling every 15 minutes)

```cron
*/15 * * * * cd /opt/terravault && /opt/terravault/.venv/bin/terravault watch --once --roi config/switzerland.geojson --state-db var/switzerland-rolling.db --storage-root data >> var/rolling.log 2>&1
```

### Prefect

```python
from prefect import flow
from terravault import Pipeline, PipelineConfig

@flow(name="sentinel-ingestion")
def ingest():
    Pipeline(PipelineConfig(asset_keys=["B04", "B08"])).run()
```

---

## Extending to other collections

```python
cfg = PipelineConfig(
    collections=["sentinel-1-grd", "sentinel-3-olci-l1efr"],
    bbox=[5.96, 45.82, 10.49, 47.81],
    max_cloud_cover=None,   # radar / ocean sensors have no cloud cover
    asset_keys=[],          # download all assets
)
Pipeline(cfg).run()
```

---

## Development

```bash
git clone --recurse-submodules https://github.com/noverney/terravault
cd terravault
pip install -e ".[dev]"
pytest
```

---

## Technology stack

| Library | Role |
|---|---|
| `pystac-client` | STAC API search and item pagination |
| `requests` | HTTP downloads with streaming |
| `tqdm` | Download progress bars |
| `rasterio` *(optional)* | Raster I/O and COG conversion |
| GDAL command-line tools | Block-wise VRT reprojection, mosaicking and COG extraction |
| FORCE v3.10.04 | Tiled external-feature datacubes and VRT mosaics |
| `Pillow` *(optional)* | Public-thumbnail country overview |
| `boto3` *(optional)* | Native CDSE S3 streaming and Range-resume |

---

## Supported STAC collections (Copernicus Data Space)

| Collection ID | Description |
|---|---|
| `sentinel-2-l2a` | Sentinel-2 Level-2A (surface reflectance) |
| `sentinel-2-l1c` | Sentinel-2 Level-1C (top of atmosphere) |

Future collections (Sentinel-1, Sentinel-3, Landsat) work out of the box —
just pass their collection IDs in `PipelineConfig.collections`.

## CDSE authentication modes

Use `TERRAVAULT_CDSE_ACCESS_TOKEN` if you already have a bearer token, or `TERRAVAULT_CDSE_USERNAME` / `TERRAVAULT_CDSE_PASSWORD` to let TerraVault request one with the `cdse-public` client. If MFA is enabled, also set `TERRAVAULT_CDSE_TOTP`.

For Sentinel Hub APIs such as patch extraction, use `TERRAVAULT_CDSE_SH_CLIENT_ID` and `TERRAVAULT_CDSE_SH_CLIENT_SECRET`. These come from the CDSE Dashboard `User Settings` -> `OAuth clients`.

Native rolling downloads use separately generated
`TERRAVAULT_CDSE_S3_ACCESS_KEY` and `TERRAVAULT_CDSE_S3_SECRET_KEY` values.
Sentinel Hub OAuth credentials and CDSE account passwords are not S3 keys.
For small AOIs and ad-hoc raw patches, the Process API remains the simpler
path.

See [`docs/REPOSITORY_GUIDE.md`](docs/REPOSITORY_GUIDE.md) for the code map,
verification commands and repository invariants.
