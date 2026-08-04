# Switzerland-wide Sentinel-2 extraction plan

## Outcome

Use two related but deliberately separate workflows:

1. A small, reproducible country overview for visual inspection, using a
   same-day mosaic at reduced resolution.
2. A rolling, analysis-ready archive that stores each new Sentinel-2 L2A
   granule at its native resolution and builds country products downstream.

The test date is **2026-07-04 UTC**. Live CDSE STAC discovery found clear
coverage from adjacent Sentinel-2 swaths on that day. It is a same-day mosaic,
not one simultaneous exposure: no single Sentinel-2 frame covers all of
Switzerland.

## One-date test

### Visual overview without credentials

```bash
python -m pip install -e ".[overview]"
python examples/switzerland_patch/build_switzerland_overview.py \
  --date 2026-07-04 \
  --width 2400
```

This combines public CDSE STAC thumbnails into a large JPEG and writes a JSON
source manifest. It is a quicklook only, not a geospatial analysis raster.

### All documented L2A Process API layers

First generate and inspect the request bodies without using credentials or
processing units:

```bash
python examples/switzerland_patch/fetch_switzerland_snapshot.py \
  --date 2026-07-04 \
  --max-dimension 2000 \
  --dry-run
```

For the real export, create a Sentinel Hub OAuth client in the CDSE Dashboard
and set:

```text
TERRAVAULT_CDSE_SH_CLIENT_ID=...
TERRAVAULT_CDSE_SH_CLIENT_SECRET=...
```

Then run:

```bash
python examples/switzerland_patch/fetch_switzerland_snapshot.py \
  --date 2026-07-04 \
  --max-dimension 2000
```

Outputs:

| File | Layers | Type |
|---|---|---|
| `switzerland_spectral.tif` | B01–B09 (except L2A B10), B8A, B11, B12, AOT | UINT16 DN |
| `switzerland_quality.tif` | SCL, snow probability, cloud probability, data mask | UINT8 |
| `switzerland_angles.tif` | sun/view azimuth and zenith means | FLOAT32 degrees |
| `switzerland_overview.png` | B04/B03/B02 | Rendered true colour |

The bands are split because the API layers have different native types and
units. The exact band order, units, requests and file sizes are recorded in
`manifest.json`.

### Verified test result

The authenticated test completed on 2026-07-17:

| Output | Dimensions | Bands/type | Size |
|---|---:|---|---:|
| Spectral | 2000 × 1284 | 13 × UINT16 | 56,945,576 bytes |
| Quality | 2000 × 1284 | 4 × UINT8 | 814,219 bytes |
| Angles | 2000 × 1284 | 4 × FLOAT32 | 662,749 bytes |
| Overview | 2000 × 1284 | RGB PNG | 6,174,326 bytes |

GDAL verified EPSG:4326 georeferencing and the configured
`[5.96, 45.82, 10.49, 47.81]` extent for all three GeoTIFFs. The generated
manifest reports `complete` for every output.

At 2000 pixels on the long side, the output is roughly 2000 × 1280 and about
200 m/pixel. A full Switzerland bbox at 10 m is roughly 35,000 × 22,000
pixels. Resampling all 21 documented API layers to that grid would be on the
order of 35 GB uncompressed per country mosaic and would exceed the
synchronous Process API's 2500-pixel dimension limit. The overview is
therefore intentionally reduced resolution.

The [CDSE Process API](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Process.html)
automatically mosaics source tiles. The
[official FAQ](https://documentation.dataspace.copernicus.eu/FAQ.html)
documents the 2500 × 2500 synchronous limit. The
[Sentinel-2 L2A data reference](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/S2L2A.html)
is the source of the layer list and types.

## Native FORCE cloud-processing branch

The country overview and rolling archive above use Sentinel-2 L2A. TerraVault
also has a separate, native FORCE L2PS branch for experiments that require
FORCE's own atmospheric correction and cloud/cirrus/shadow/snow
classification:

```text
saved Sentinel-2 L1C STAC Item
       │
       ├── safe_manifest + S3 keys ──► complete SAFE directory
       └── Product + CDSE bearer ────► complete SAFE ZIP
                                      │
                                      ▼
                             terravault force-level2
                                      │
                                      ▼
                      level2/products/<SAFE-stem>/
                                      ├── BOA tiles + per-SAFE mosaic
                                      ├── QAI tiles + per-SAFE mosaic
                                      └── OVV tile quicklooks
```

FORCE L2PS cannot use the selected L2A native pieces or a country COG. Each
input must be one complete `S2*_MSIL1C_*.SAFE[.zip]`, including product and
granule metadata and every band (including B10). `force-level2` validates that
boundary before processing.

The defaults are Switzerland-oriented: EPSG:2056, 10 m pixels, 30 km FORCE
tiles, and a 5.5° E / 48.0° N grid origin. Ten metres is the highest native
Sentinel-2 resolution; FORCE merges coarser bands to that grid. Use a DEM
covering every input footprint. Without one the job continues, but
topographic correction is disabled and atmospheric/cloud-shadow quality is
reduced. The output root's `_terravault/force-l2/cube.json` fixes this grid;
changing it requires a new root.

One product is only a processing unit, not Swiss-wide coverage. For a dated
country result, select every L1C STAC item intersecting the versioned Swiss
polygon and run those products sequentially. Each SAFE is published in its own
directory; same-date/sensor granules do not merge. The command persists each
download, queue, parameter file, batch log, input fingerprint, BOA/QAI list,
OVV list, and per-SAFE mosaics so a stopped job can be rerun safely. Completion
requires the expected cube definition, CRS, tile dimensions/alignment, and
matching BOA/QAI/OVV tile coverage.

Complete S3 SAFE trees are downloaded into a hidden per-product staging
directory. The previous verified publication stays available until the exact
object list, sizes, per-object SHA-256 values, SAFE hierarchy, and all 13 L1C
bands pass validation; only then is the staged directory promoted.

This command does not build an acquisition-level or national native FORCE
mosaic, and it is not integrated into `watch` or `historic`. It is the durable
per-product primitive around which L1C discovery, scheduling, and downstream
pooling still need to be built.

For a controlled cloud-mask comparison, `force-visualize --force-qai` aligns a
matching FORCE QAI mosaic and creates a three-panel raw/CDSE/FORCE diagnostic.
Every panel uses the same L2A B04/B08 NDVI; only the quality policy changes.
The default FORCE mask `0x031F` screens nodata, all cloud states, shadow, snow,
subzero, and saturation. Today that QAI mosaic is selected from one per-SAFE
publication, not from a country-wide native mosaic.

The existing real Zurich FORCE artifact validates only the L2A
external-feature bridge. Native Zurich L2PS is not yet claimed as a successful
live run because the 2026-08-04 preflight returned `InvalidAccessKeyId` for the
configured S3 pair and no Product bearer/account fallback was configured. See
[FORCE_POSTPROCESSING.md](FORCE_POSTPROCESSING.md) for commands, credential
routes, QAI bits, and state layout.

## Rolling production architecture

```text
CDSE subscription + STAC overlap scan
                  |
                  v
          durable product queue
                  |
                  v
       S3 native-asset acquisition
                  |
                  v
       checksum + raster QA + retry
                  |
                  v
   immutable native archive (JP2/XML)
                  |
                  v
   COG/Zarr conversion and Swiss mask
                  |
                  v
  per-acquisition catalogue + country views
```

### 1. Version the area of interest

Use the official
[swissBOUNDARIES3D](https://www.swisstopo.admin.ch/en/landscape-model-swissboundaries3d)
country polygon, reprojected to EPSG:4326, for discovery and to EPSG:2056 for
Swiss products. Keep its edition/date beside every output manifest. The
current repository bbox is useful for initial discovery but includes parts of
France, Germany, Austria, Italy and Liechtenstein.

### 2. Detect newly published products

Create a CDSE OData subscription for:

- collection `SENTINEL-2`;
- product type `S2MSI2A`;
- `created` and `modified` events;
- intersection with the Swiss polygon.

Poll a pull subscription every 5–10 minutes and acknowledge messages only
after they have been durably inserted into the local job database. CDSE keeps
full pull-notification payloads for three days, so monitoring must alert well
before that window expires. The
[Subscriptions API](https://documentation.dataspace.copernicus.eu/APIs/Subscriptions.html)
also supports push delivery.

Run a second, independent STAC reconciliation scan every hour with at least a
72-hour acquisition-time overlap. Deduplicate by product/item ID. This catches
subscription outages, delayed publications and replayed messages.

### 3. Use a durable state machine

Track each product with these states:

```text
discovered -> queued -> downloading -> downloaded -> verified -> published
                                \-> retry_wait
                                \-> dead_letter
```

Store product ID, item ID, acquisition/publication/modification timestamps,
MGRS tile, processing baseline, source URLs, expected sizes/checksums,
attempt count, last error and output manifest. A job becomes complete only
after every required asset verifies.

SQLite is sufficient for one worker. Use PostgreSQL plus `FOR UPDATE SKIP
LOCKED` when multiple workers are needed.

### 4. Acquire native assets through CDSE S3

For a rolling archive, download native STAC assets from `s3://eodata` with
CDSE S3 access/secret keys and the endpoint
`https://eodata.dataspace.copernicus.eu`. The
[official S3 guide](https://documentation.dataspace.copernicus.eu/APIs/S3.html)
shows both AWS CLI and boto3 configuration.

Keep native resolutions instead of upsampling everything:

- 10 m: B02, B03, B04, B08, AOT/WVP where required;
- 20 m: B05, B06, B07, B8A, B11, B12, SCL, CLD, SNW;
- 60 m: B01, B09;
- metadata: SAFE manifest, product/granule/datastrip XML and checksums.

The STAC item can expose several resolutions for the same band. Define one
canonical asset-key policy in configuration and version it. Downloading the
whole SAFE product is simpler and more complete; downloading selected native
assets reduces storage and requests. Record which policy was used.

### 5. Storage layout

Use immutable, collision-safe keys:

```text
native/sentinel-2-l2a/acquired=YYYY-MM-DD/tile=32TNT/item=ITEM_ID/...
derived/sentinel-2-l2a/date=YYYY-MM-DD/product=switzerland-v1/...
manifests/item=ITEM_ID.json
```

Write to a temporary key, verify, then atomically promote. Never overwrite an
older processing baseline silently; treat a modified/reprocessed product as a
new version linked to the superseded one.

### 6. Verify before publishing

- Compare content length and catalogue checksum where present.
- Open every raster and verify CRS, bounds, dimensions, dtype and nonempty
  data.
- Confirm asset coverage intersects the Swiss polygon.
- Check that required asset keys are complete.
- Generate a small true-colour quicklook for operator review.
- Write a manifest containing source item JSON, software version, AOI version,
  checksums and validation results.

### 7. Build derived country products

Do not rebuild a huge monolith for every arriving granule. Maintain a
tile-indexed collection of Cloud Optimized GeoTIFFs or Zarr chunks and expose
a virtual mosaic. For a dated Switzerland product:

1. select the acquisition window and mosaicking rule;
2. reproject to EPSG:2056 at the requested resolution;
3. use SCL/CLD for cloud policy;
4. mosaic deterministically;
5. crop/mask to the Swiss polygon;
6. build overviews and publish a STAC item.

For large managed processing, CDSE
[Batch Processing V2](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/BatchV2.html)
is designed for large areas, but it requires an eligible Copernicus Service
account and object storage. General-user quotas do not include Batch V2, so
native S3 ingestion is the dependable baseline.

## Operations

- Poll subscription: every 5 minutes.
- STAC reconciliation: hourly, 72-hour overlap.
- Deep reconciliation: daily, 14-day overlap.
- Exponential retry with jitter; honour HTTP `Retry-After`.
- Alert on queue age, three consecutive failures, missing required assets,
  subscription silence and quota exhaustion.
- Back up the job database and manifests; native rasters can be recovered from
  CDSE, but processing history and provenance should not be reconstructed by
  guesswork.

## Implementation phases

1. **Completed:** live STAC discovery, overlap/dedup, collision-safe paths,
   country overview and all-layer Process API snapshot requests.
2. **Completed:** `terravault watch` with bbox/GeoJSON discovery, native CDSE
   S3 assets, Range-resumable `.part` transfers, SHA-256 provenance, durable
   per-job/per-asset SQLite state, failure sidecars, exponential retry,
   an overlap lock and graceful SIGINT/SIGTERM handling. See
   [`ROLLING_INGESTION.md`](ROLLING_INGESTION.md).
3. **Completed:** `terravault historic` windowed backfill with a durable date
   cursor, progress bars, quota/`Retry-After` waiting and terminal retired-job
   handling. See [`HISTORICAL_INGESTION.md`](HISTORICAL_INGESTION.md).
4. **Completed:** DuckDB spatial/time piece queries and memory-bounded
   latest-per-tile stitched COG extraction with feature selection, reprojection,
   dry-run sizing and a source/band manifest. See
   [`RASTER_QUERY_AND_EXTRACTION.md`](RASTER_QUERY_AND_EXTRACTION.md).
5. **Implemented and unit-tested, live credential validation pending:**
   resumable complete L1C SAFE acquisition plus one-product native FORCE L2PS
   into an isolated, verified per-SAFE BOA/QAI publication, with immutable-grid
   enforcement, atomic restart/provenance state, and the three-way quality-mask
   diagnostic.
6. **Next:** add durable country-wide `sentinel-2-l1c` discovery/queue
   orchestration and a separate acquisition/national pooling or virtual-mosaic
   layer around the per-product L2PS primitive.
7. **Next:** add an OData subscription consumer alongside the implemented
   STAC reconciliation scan.
8. **Then:** version an official Swiss boundary in deployment configuration
   and add raster coverage/format QA.
9. **Finally:** optional per-piece COG/Zarr conversion, monitoring and
   deployment packaging.
