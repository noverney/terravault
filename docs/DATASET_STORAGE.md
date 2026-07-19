# Dataset storage, DuckDB catalogue and logs

Rolling and historical ingestion use a partitioned dataset layout. Large
raster pieces are never placed together in one flat directory.

```text
satellite_data/
├── dataset.duckdb
├── dataset.duckdb.lock
├── _terravault/
│   ├── state/
│   │   ├── rolling.db
│   │   └── history.db
│   └── logs/
│       ├── watch.log
│       ├── watch.log.1
│       ├── historic.log
│       └── query.log
└── pieces/
    └── collection=sentinel-2-l2a/
        └── year=2026/
            └── month=07/
                └── day=04/
                    └── tile=32TNT/
                        └── item=S2A_MSIL2A_.../
                            ├── scene_metadata.json
                            ├── job_status.json
                            ├── B02_10m.jp2
                            ├── B03_10m.jp2
                            ├── SCL_20m.jp2
                            └── product_metadata.xml
```

The date, MGRS tile and item partitions bound the number of files in every
leaf directory. Native CDSE Sentinel-2 raster assets are georeferenced JP2
pieces; derived GeoTIFF/COG pieces can use the same catalogue fields and
partition structure. The country snapshot workflow remains separate because
its GeoTIFFs are reduced-resolution same-day mosaics rather than native
granules.

## DuckDB at the dataset root

`dataset.duckdb` is the analytical index. The retry-safe worker queue remains
in SQLite because SQLite is better suited to frequent transactional state
changes. Both are written automatically:

- SQLite: due work, attempts, locks, retry time and event history;
- DuckDB: discoverable scene/asset metadata and exact local file locations.

DuckDB tables:

- `dataset_info`: schema version, dataset root and ROI;
- `items`: acquisition time, collection, tile, status, cloud cover, WGS84
  geometry/bounds and metadata paths;
- `assets`: source URI, absolute local path, status, native resolution,
  projection metadata, bytes and SHA-256;
- `runs`: rolling/historical run outcomes and their SQLite database;
- `raster_pieces`: a view joining queryable item and raster-asset fields.

When rasterio is installed, downloaded raster pieces are opened and their
actual driver, dtype, CRS, dimensions and native-coordinate bounds are added.
Without rasterio, STAC projection/shape metadata and WGS84 scene bounds remain
available.

## Query local pieces

Return completed raster file paths intersecting a WGS84 area:

```bash
terravault query \
  --dataset-db satellite_data/dataset.duckdb \
  --bbox 7.0 46.0 9.0 47.5
```

Filter by acquisition date and native layers:

```bash
terravault query \
  --bbox 7.0 46.0 9.0 47.5 \
  --start-date 2026-07-01 \
  --end-date 2026-07-31 \
  --asset-keys B02_10m B03_10m B04_10m SCL_20m
```

Use `--output json` for CRS, resolution, bounds, dimensions, checksum and
status. `--summary` reports scene/asset counts and completed bytes.

Create one stitched query result without loading its pixels into Python:

```bash
terravault extract \
  --dataset-db satellite_data/dataset.duckdb \
  --bbox 7.0 46.0 9.0 47.5 \
  --asset-keys B04_10m B03_10m B02_10m \
  --output exports/rgb.tif \
  --target-crs EPSG:2056
```

The extractor selects the latest completed piece per MGRS tile by default,
builds virtual feature mosaics, and streams an aligned multiband COG in GDAL
blocks. It estimates uncompressed output size before writing and emits a source
and band-order manifest. See
[RASTER_QUERY_AND_EXTRACTION.md](RASTER_QUERY_AND_EXTRACTION.md) for polygon
queries, resolution semantics, dry runs and memory controls.

DuckDB can also be queried directly:

```sql
SELECT
    acquisition_time,
    tile_id,
    asset_key,
    resolution_m,
    proj_epsg,
    local_path
FROM raster_pieces
WHERE status = 'completed'
  AND item_east >= 7.0
  AND item_west <= 9.0
  AND item_north >= 46.0
  AND item_south <= 47.5
ORDER BY acquisition_time, tile_id, asset_key;
```

The WGS84 item footprint is used for spatial selection. Raster-native bounds
remain paired with their `proj_epsg`, avoiding invalid comparisons between
longitude/latitude and UTM coordinates.

## Python API

Querying the local dataset does not contact Copernicus or require credentials:

```python
from pathlib import Path

from terravault import DatasetCatalog

database = Path("satellite_data/switzerland_ndvi/dataset.duckdb")
catalog = DatasetCatalog(database)

print(catalog.summary())
pieces = catalog.query_raster_pieces(
    bbox=(8.45, 47.20, 8.65, 47.35),
    asset_keys=("B04_10m", "B08_10m", "SCL_20m", "CLD_20m"),
)
for piece in pieces:
    print(
        piece["acquisition_time"],
        piece["tile_id"],
        piece["asset_key"],
        piece["local_path"],
    )
```

Create a stitched result through the same public package API:

```python
from terravault import ExtractionConfig, RasterExtractor

result = RasterExtractor(
    ExtractionConfig(
        dataset_db=database,
        bbox=(8.45, 47.20, 8.65, 47.35),
        asset_keys=("B04_10m", "B08_10m", "SCL_20m", "CLD_20m"),
        output_path=Path("exports/zurich_ndvi_inputs.tif"),
        target_crs="EPSG:2056",
        resolution=10,
        warp_memory_mib=256,
        dry_run=True,
    )
).extract()
print(result)
```

Set `dry_run=False` (or remove that argument) to write the COG. GDAL performs
the raster work block by block; Python does not load the entire result into
memory. The complete runnable example, including date filters, JSON output,
selection options and output safeguards, is
[`../examples/local_dataset_api.py`](../examples/local_dataset_api.py).

## Operational logs

Logs are created automatically:

- `STORAGE_ROOT/_terravault/logs/watch.log`;
- `STORAGE_ROOT/_terravault/logs/historic.log`;
- `STORAGE_ROOT/_terravault/logs/run.log`;
- alongside a custom DuckDB for `terravault query` or `terravault extract`.

Each file rotates at 25 MiB with ten retained generations. Logs include:

- state, dataset and DuckDB paths;
- discovery windows and progress;
- every queued item and its partition directory;
- every asset source and destination;
- byte count, SHA-256 and completion path;
- missing assets, transfer failures and retry times;
- quota/usage waits and `Retry-After`;
- interruption, retirement and final run counts.

Use `--log-file PATH` to override the automatic location. Credentials are
never written to the logs.

## Custom roots

```bash
terravault watch \
  --roi config/switzerland.geojson \
  --storage-root /data/sentinel2
```

This automatically uses:

```text
/data/sentinel2/dataset.duckdb
/data/sentinel2/_terravault/logs/watch.log
/data/sentinel2/pieces/...
```

Override only when necessary:

```bash
terravault watch \
  --roi config/switzerland.geojson \
  --storage-root /data/sentinel2 \
  --dataset-db /catalogues/switzerland.duckdb \
  --log-file /var/log/terravault/switzerland.log
```
