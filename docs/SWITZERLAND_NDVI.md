# Switzerland NDVI dataset

TerraVault's `ndvi` asset profile retains the two native 10 m inputs for
standard Sentinel-2 NDVI and two quality layers:

| Asset | Native resolution | Purpose |
|---|---:|---|
| `B04_10m` | 10 m | Red reflectance/DN |
| `B08_10m` | 10 m | Near-infrared reflectance/DN |
| `SCL_20m` | 20 m | Scene classification for invalid, shadow, cloud and snow masks |
| `CLD_20m` | 20 m | Cloud-probability layer |

Standard NDVI is:

```text
NDVI = (B08 - B04) / (B08 + B04)
```

Compute with a floating-point type. Mask pixels where the denominator is zero
and apply the selected SCL/CLD policy before interpreting the result.

## Credentials

Native pieces come from CDSE S3 and require the S3-specific key pair:

```dotenv
TERRAVAULT_CDSE_S3_ACCESS_KEY=...
TERRAVAULT_CDSE_S3_SECRET_KEY=...
```

The Sentinel Hub OAuth client ID/secret cannot authenticate native S3
transfers.

## Current rolling Switzerland dataset

Use a WGS84 national polygon when one is available. The convenience bbox is:

```bash
terravault watch \
  --bbox 5.96 45.82 10.49 47.81 \
  --asset-profile ndvi \
  --storage-root satellite_data/switzerland_ndvi
```

The first run searches 14 days and chooses the newest near-full-footprint item
for each of the 20 intersecting MGRS tiles. It rejects newer edge-of-swath
slivers. Subsequent cycles ingest every new item, while query-time selection
again prefers recent near-full footprints.

Stop with Ctrl-C or SIGTERM and rerun the same command. Partial S3 objects,
SQLite queue state, the discovery watermark, DuckDB metadata and logs all
resume from the dataset root.

For cron:

```cron
*/15 * * * * cd /opt/terravault && /opt/terravault/.venv/bin/terravault watch --once --bbox 5.96 45.82 10.49 47.81 --asset-profile ndvi --storage-root /data/terravault/switzerland_ndvi
```

## Live size estimate

The footprint-aware CDSE trial on 2026-07-17 selected 20 products. Their
catalogue-reported sizes were:

| Asset | Transfer size |
|---|---:|
| B04 | 2.313 GiB |
| B08 | 2.486 GiB |
| SCL | 0.052 GiB |
| CLD | 0.078 GiB |
| **Total** | **4.929 GiB** |

This is about one third of the 15.16 GiB complete native profile for the same
selection. Future totals vary with the selected products.

## Query or stitch the inputs

Find intersecting paths:

```bash
terravault query \
  --dataset-db satellite_data/switzerland_ndvi/dataset.duckdb \
  --bbox 8.45 47.20 8.65 47.35 \
  --asset-keys B04_10m B08_10m SCL_20m CLD_20m
```

Create one aligned four-band COG without loading it into Python memory:

```bash
terravault extract \
  --dataset-db satellite_data/switzerland_ndvi/dataset.duckdb \
  --bbox 5.96 45.82 10.49 47.81 \
  --asset-keys B04_10m B08_10m SCL_20m CLD_20m \
  --output satellite_data/switzerland_ndvi/exports/switzerland_inputs.tif \
  --target-crs EPSG:2056 \
  --resolution 10 \
  --dry-run
```

The output band order matches `--asset-keys`. SCL and CLD are resampled from
their native 20 m grids; the default nearest-neighbour method preserves their
categorical/probability values. Repeat without `--dry-run` after checking the
reported dimensions and uncompressed-size estimate.

## FORCE feature cube

Plan or run the same latest-per-tile country extraction and import it as a
tiled FORCE external feature:

```bash
python examples/postprocessing/force_switzerland.py --dry-run
python examples/postprocessing/force_switzerland.py --runtime docker
```

This is a four-band TerraVault-derived external feature, not FORCE BOA/QAI
ARD. The wrapper skips an unchanged staging selection, uses stable
source-derived product names and resumes from atomic job manifests. See
[FORCE_POSTPROCESSING.md](FORCE_POSTPROCESSING.md).

Create a colorized, quality-masked NDVI view from the FORCE mosaic:

```bash
terravault force-visualize --input FORCE_ROOT/datacube/mosaic/FEATURE.vrt
```

The resulting PNG is for inspection; use the accompanying Float32 COG for
geospatial analysis. The adjacent `*_before_after.png` debug plot shows raw
NDVI and the quality-masked result side by side on one color scale.
