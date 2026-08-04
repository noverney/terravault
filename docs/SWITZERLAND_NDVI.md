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
categorical/probability values. The output and sidecar retain a per-band raw
DN scale/offset and nodata contract. Because cloud probability zero is valid,
a mixed reflectance/SCL/CLD extraction automatically replaces conflicting
output nodata zero with `-9999` and explicitly maps each source's own nodata.
Repeat without `--dry-run` after checking the reported dimensions and
uncompressed-size estimate.

## FORCE L2A external-feature cube

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

This path consumes the rolling **L2A** archive. It preserves the CDSE SCL/CLD
quality information but does not run FORCE cloud classification or create
FORCE BOA/QAI.

## Native FORCE cloud processing

Genuine FORCE cloud, cirrus, shadow, and snow classification starts from a
complete Sentinel-2 **L1C** product, not from the four NDVI-profile assets.
For one complete local product:

```bash
python examples/postprocessing/force_level2.py \
  --input /data/switzerland/l1c/S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T142404.SAFE.zip \
  --output-root /data/switzerland/force-native \
  --runtime docker \
  --dem /data/reference/switzerland_dem.tif
```

Or use a saved L1C STAC Item:

```bash
terravault force-level2 \
  --stac-item /data/stac/S2A_MSIL1C_item.json \
  --output-root /data/switzerland/force-native \
  --runtime docker \
  --dem /data/reference/switzerland_dem.tif
```

With S3 credentials, TerraVault prefers the STAC `safe_manifest` route and
downloads every file below the SAFE prefix. Without S3, a current CDSE bearer
token or account login can download the authenticated `Product` ZIP. Sentinel
Hub OAuth client credentials are not valid for either native download route.

The Swiss defaults are EPSG:2056 at 10 m, 30 km FORCE tiles, and a grid origin
at 5.5° E / 48.0° N. Ten metres is the highest Sentinel-2 spatial resolution;
coarser spectral bands are merged to that grid. A DEM is strongly recommended.
Omitting it disables topographic correction and reduces atmospheric and
cloud-shadow quality, even though the command is allowed to continue. The
output root's `_terravault/force-l2/cube.json` makes the grid immutable; use a
new root to change CRS, origin, tile size, or resolution.

One SAFE generally does not cover all of Switzerland. A country acquisition
is the set of all L1C observations intersecting the Swiss polygon in the
chosen window. `force-level2` can process them one at a time under the same
output root, but publishes each SAFE independently below
`level2/products/<SAFE-stem>/`; same-date products do not merge. It does not
yet create a pooled acquisition or national FORCE mosaic. Each verified
publication contains 10-band BOA COGs, bit-packed QAI COGs, per-tile OVV JPEGs,
and per-SAFE BOA/QAI mosaic VRTs.

The command is the durable per-product primitive, not an active Swiss rolling
FORCE scheduler. Existing `terravault watch` and `terravault historic` remain
L2A ingestion workflows and do not discover L1C products or dispatch L2PS.

## Three-way mask comparison

After a native run has produced a QAI mosaic matching the L2A extraction's
date and sensor:

```bash
terravault force-visualize \
  --input FORCE_EXTERNAL_ROOT/datacube/mosaic/FEATURE.vrt \
  --force-qai FORCE_NATIVE_ROOT/level2/products/SAFE_STEM/mosaic/YYYYMMDD_LEVEL2_SEN2A_QAI.vrt
```

This QAI mosaic belongs to one SAFE; select matching L2A date, sensor, and
footprint provenance. A country-wide native QAI mosaic is not generated by
`force-level2` yet.

The `*_raw_cdse_force.png` output shows, side by side:

1. raw NDVI from the L2A B04/B08 inputs;
2. that same NDVI screened by CDSE SCL+CLD;
3. that same NDVI screened by native FORCE QAI.

All panels intentionally retain the same physical-reflectance basis, applying
the recorded L2A B04/B08 scale and offset, so visible differences isolate
masking. The FORCE panel does not substitute BOA pixels.
The default QAI bit mask is `0x031F`, which excludes nodata, all non-clear
cloud states, cloud shadow, snow, subzero, and saturation. Use
`--force-qai-mask` only to apply a deliberately different quality policy.

The existing Zurich artifact and its before/after plot came from the L2A
external-feature path. A real Zurich L1C → native L2PS result is not yet
claimed. In the 2026-08-04 preflight, CDSE rejected the configured S3 pair
with `InvalidAccessKeyId`, and no Product bearer/account fallback was
configured.
