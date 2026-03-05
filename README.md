# terravault

> **Open satellite data ingestion pipeline** — STAC-based discovery, deduplication, and download for Sentinel and future satellite imagery.

TerraVault is a lightweight Python toolkit that connects to any STAC-compliant catalog, discovers new satellite scenes for a configurable area of interest, persists their metadata locally, and downloads the assets you need — all while keeping track of what has already been ingested so each run processes only *new* data.

---

## Features

| Feature | Details |
|---|---|
| **STAC-native discovery** | Uses `pystac_client` against any STAC API (default: Copernicus Data Space) |
| **Switzerland out of the box** | Pre-configured bounding box `[5.96, 45.82, 10.49, 47.81]` covering Switzerland |
| **Cloud-cover filter** | Skips scenes above a configurable threshold (default: 20 %) |
| **State tracking** | SQLite (default) or JSON state file — each run ingests only *new* scenes |
| **Organised storage** | `satellite_data/sentinel2/YYYY/MM/DD/TILE_ID/` directory tree |
| **Retry & back-off** | Exponential back-off with jitter on transient download failures |
| **Bounded parallelism** | Configurable thread-pool (`max_workers=4` default) respects API limits |
| **Asset filtering** | Download all assets or a named subset (e.g. `B04`, `B08`) |
| **CLI** | `terravault run` / `terravault collections` entry-points |

---

## Installation

```bash
pip install terravault
```

Optional extras:

```bash
pip install "terravault[raster]"    # adds rasterio
pip install "terravault[postgres]"  # adds psycopg2-binary
pip install "terravault[dev]"       # adds pytest + responses for development
```

---

## Quick start

### Python API

```python
from terravault import Pipeline, PipelineConfig
from terravault.downloader import DownloadConfig

cfg = PipelineConfig(
    collections=["sentinel-2-l2a"],
    max_cloud_cover=20.0,
    lookback_hours=48,           # look back 48 h on first run
    asset_keys=["B04", "B08"],   # download only red + NIR bands
    download=DownloadConfig(max_workers=4),
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

---

## Command-line interface

```text
Usage: terravault [OPTIONS] COMMAND [ARGS]...

Commands:
  run          Execute one pipeline ingestion run
  collections  List collections available in the STAC catalog
```

### Examples

```bash
# Metadata-only dry run (no assets downloaded)
terravault run --no-download

# Download red and NIR bands only
terravault run --asset-keys B04 B08

# Use a different STAC catalog
terravault run --catalog-url https://stac.dataspace.copernicus.eu/v1

# Increase lookback window to 7 days on first run
terravault run --lookback-hours 168

# List all collections in the catalog
terravault collections
```

Full option reference:

```text
terravault run --help
```

---

## Storage layout

Downloaded data is organised as:

```
satellite_data/
  sentinel2/
    2024/
      06/
        15/
          32TNT/
            scene_metadata.json   ← full STAC item JSON
            B04.tif
            B08.tif
```

The tile directory name is derived from the `s2:mgrs_tile` STAC property (e.g. `32TNT`).

---

## State tracking

TerraVault stores ingestion state in a local SQLite database (`terravault_state.db` by default).

* Each processed item ID is recorded so it is never ingested twice.
* The last-processed timestamp drives the search window on subsequent runs.

A lightweight JSON alternative is also available:

```python
from terravault.state import JSONStateManager

state = JSONStateManager("my_state.json")
state.mark_processed("S2A_...", item_datetime)
print(state.last_processed)
```

---

## Scheduling

### cron (daily at 02:00)

```cron
0 2 * * *  cd /opt/terravault && /usr/local/bin/terravault run --asset-keys B04 B08 >> /var/log/terravault.log 2>&1
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
git clone https://github.com/noverney/terravault
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

---

## Supported STAC collections (Copernicus Data Space)

| Collection ID | Description |
|---|---|
| `sentinel-2-l2a` | Sentinel-2 Level-2A (surface reflectance) |
| `sentinel-2-l1c` | Sentinel-2 Level-1C (top of atmosphere) |

Future collections (Sentinel-1, Sentinel-3, Landsat) work out of the box —
just pass their collection IDs in `PipelineConfig.collections`.
