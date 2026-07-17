# Raster queries and memory-bounded extraction

TerraVault keeps Sentinel-2 scenes as independent georeferenced pieces and
indexes them in the top-level `dataset.duckdb`. It does not build or keep a
second country-sized raster. A stitched image is a derived query result.

This design provides three useful properties:

- ingestion can add a new scene without rewriting Switzerland;
- a query reads only pieces intersecting its WGS84 region and requested
  features;
- GDAL performs reprojection, mosaicking and Cloud Optimized GeoTIFF (COG)
  creation block by block. Python never loads the country raster into memory.

GDAL command-line tools (`gdalwarp`, `gdalbuildvrt`, `gdal_translate` and
`gdalinfo`) must be installed for `terravault extract`.

## Find pieces without reading pixels

Print paths:

```bash
terravault query \
  --dataset-db /data/switzerland/dataset.duckdb \
  --bbox 8.45 47.20 8.65 47.35 \
  --asset-keys B04_10m B03_10m B02_10m
```

Return the same rows with acquisition, tile, size, CRS and checksum metadata:

```bash
terravault query \
  --dataset-db /data/switzerland/dataset.duckdb \
  --bbox 8.45 47.20 8.65 47.35 \
  --asset-keys B04_10m SCL_20m \
  --output json
```

## Return one stitched image

The following creates a three-band RGB COG for an intersection. The requested
feature order is the output band order:

```bash
terravault extract \
  --dataset-db /data/switzerland/dataset.duckdb \
  --bbox 8.45 47.20 8.65 47.35 \
  --asset-keys B04_10m B03_10m B02_10m \
  --output /data/exports/zurich_rgb.tif \
  --target-crs EPSG:2056
```

For a country boundary rather than a rectangle, provide the same WGS84
Polygon/MultiPolygon GeoJSON used for ingestion:

```bash
terravault extract \
  --dataset-db /data/switzerland/dataset.duckdb \
  --roi switzerland.geojson \
  --asset-keys B04_10m B03_10m B02_10m \
  --output /data/exports/switzerland_rgb.tif \
  --target-crs EPSG:2056 \
  --resolution 10
```

`latest-per-tile` is the default selection. Among items that have every
requested feature completed locally, it finds the largest observed footprint
for each MGRS tile, keeps items at least 90% as large, and chooses the newest.
This rejects newer edge-of-swath slivers that would create large nodata gaps.
All output bands for a tile come from one acquisition instead of silently
mixing dates. `--start-date` and `--end-date` constrain the available
acquisitions. `--selection all` is available for an explicitly time-filtered
mosaic, but overlapping acquisitions are then resolved by GDAL source order
rather than reduced temporally.

The output is:

- a tiled, compressed, BigTIFF-capable COG;
- one aligned band per requested asset key;
- accompanied by `OUTPUT.tif.manifest.json`, which records the band order,
  source item/tile/path/acquisition, requested region, grid, CRS, resampling,
  memory budget and size.

Sentinel-2 L2A uses zero outside valid raster coverage, so the mosaic default
is `--nodata 0`. A different collection can override this explicitly.

## Resolution and feature semantics

If `--resolution` is omitted, the extractor uses the finest native resolution
among the requested features. For example, mixing `B04_10m` and `SCL_20m`
produces a 10 m grid and resamples SCL. The default `near` resampling is safe
for categorical layers such as SCL, CLD and SNW. For reflectance-only output,
`--resampling bilinear` may be preferable.

Sentinel-2 does not make every band natively 10 m. TerraVault preserves the
highest *native* resolution available for every feature:

- 10 m: B02, B03, B04, B08, AOT and WVP;
- 20 m: B05, B06, B07, B8A, B11, B12, SCL, CLD and SNW;
- 60 m: B01 and B09.

Requesting a 10 m output with all features is supported, but 20 m and 60 m
features are upsampled; it does not add spatial information.

## Memory and disk safeguards

`--warp-memory-mib` bounds each GDAL operation (256 MiB by default), and the
final COG is written in 512-pixel blocks with a single compression thread.
Only metadata, paths and the temporary VRT XML are held in Python.

Before writing pixels, TerraVault inspects the aligned virtual raster and
computes:

```text
width × height × band_count × bytes_per_output_sample
```

The default uncompressed safety limit is 16 GiB. A request over the limit stops
with an actionable error. First inspect a large request:

```bash
terravault extract \
  --dataset-db /data/switzerland/dataset.duckdb \
  --roi switzerland.geojson \
  --asset-keys B01_60m B02_10m B03_10m B04_10m B05_20m B06_20m \
               B07_20m B08_10m B8A_20m B09_60m B11_20m B12_20m \
               AOT_10m WVP_10m SCL_20m SNW_20m CLD_20m \
  --output /data/exports/switzerland_all_features.tif \
  --target-crs EPSG:2056 \
  --resolution 10 \
  --dry-run
```

If the reported dimensions and disk budget are acceptable, repeat without
`--dry-run` and explicitly raise the guard, for example
`--max-output-gib 32`. A compressed file may be smaller than the estimate, but
the conservative uncompressed value is used to prevent accidental multi-
terabyte requests.

Operational details are written to
`DATASET_ROOT/_terravault/logs/extract.log` by default. The output is first
written to a hidden partial file in its destination directory and atomically
renamed only after GDAL succeeds. Interrupted partial files from the active
process are cleaned up.
