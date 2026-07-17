# Restartable rolling Sentinel-2 ingestion

`terravault watch` is the package command for repeatedly discovering and
archiving Sentinel-2 L2A assets for an explicit region of interest. The region
is the only command-line input required; native/all-layer acquisition,
polling, retry, state and storage have defaults. CDSE S3 credentials are read
from `.env`. It can run continuously, or once per cron invocation.

## Install and credentials

Install the S3 extra:

```bash
python -m pip install -e ".[s3]"
```

Generate Copernicus Data Space S3 credentials following the
[official CDSE S3 guide](https://documentation.dataspace.copernicus.eu/APIs/S3.html),
then place them in `.env`:

```text
TERRAVAULT_CDSE_S3_ACCESS_KEY=...
TERRAVAULT_CDSE_S3_SECRET_KEY=...
TERRAVAULT_CDSE_S3_ENDPOINT=https://eodata.dataspace.copernicus.eu
TERRAVAULT_CDSE_S3_REGION=default
```

These are separate from the Sentinel Hub OAuth client ID/secret used by the
Process API. Do not put credentials directly in a cron line.

## Choose the region

Exactly one spatial argument is required. Use a WGS84 bbox:

```bash
terravault watch --bbox 5.96 45.82 10.49 47.81
```

Or use a WGS84 GeoJSON Polygon/MultiPolygon:

```bash
terravault watch --roi config/switzerland.geojson
```

Feature and FeatureCollection wrappers are accepted. A polygon is preferable
to the Switzerland bbox because the bbox includes neighbouring countries.
The state database records a hash of the ROI and refuses to run if that same
database is later used with a different region.

## Run continuously

The default mode polls every 15 minutes:

```bash
terravault watch \
  --roi config/switzerland.geojson \
  --state-db var/switzerland-rolling.db \
  --storage-root data
```

On a new state database, the first cycle searches the preceding 14 days. For
each intersecting MGRS tile it measures the observed STAC geometry footprints,
keeps candidates at least 90% as large as that tile's largest recent footprint,
and selects the newest candidate. This avoids selecting a newer narrow
edge-of-swath sliver that would leave most of a tile empty. It creates a recent
near-full-coverage regional baseline without downloading every historical
pass. All required native-profile assets must be present.

After bootstrap, every newly discovered product is queued. Each successful
scan records its end time. The next scan starts from that durable watermark
minus a 72-hour publication-delay overlap, so a worker restarted after several
days catches up across the downtime. Item IDs and per-asset state make the
overlap idempotent. `--bootstrap-lookback-days`, `--lookback-hours` and
`--poll-seconds` remain optional overrides.

Press Ctrl-C, send `SIGINT`, or send `SIGTERM` to stop. The worker finishes its
current streamed chunk, leaves an atomic `.part` file, changes the job back to
`queued`, and exits. The next start resumes from the partial byte count using
an S3 Range request.

## Run under cron

Use `--once`; the command performs one discovery/retry cycle and exits:

```cron
*/15 * * * * cd /opt/terravault && /opt/terravault/.venv/bin/terravault watch --once --roi config/switzerland.geojson --state-db var/switzerland-rolling.db --storage-root data >> var/rolling.log 2>&1
```

An adjacent lock file prevents overlapping cron invocations from writing the
same database or objects. A locked invocation exits nonzero and reports which
lock is held.

## Native-resolution policy

The default `native` profile downloads one canonical best/native-resolution
asset for every documented L2A analysis layer:

| Resolution | Assets |
|---|---|
| 10 m | B02, B03, B04, B08, AOT, WVP |
| 20 m | B05, B06, B07, B8A, B11, B12, SCL, SNW, CLD |
| 60 m | B01, B09 |
| Metadata | SAFE manifest; product, granule, datastrip and INSPIRE XML |

The exact STAC keys are versioned in
`terravault.rolling.NATIVE_L2A_ASSET_KEYS`. Keeping native resolutions avoids
silently upsampling 20 m and 60 m layers onto a much larger 10 m grid.

For custom acquisition:

```bash
terravault watch \
  --bbox 5.96 45.82 10.49 47.81 \
  --asset-keys B02_10m B03_10m B04_10m SCL_20m
```

To validate discovery/state without S3 credentials or raster downloads:

```bash
terravault watch \
  --bbox 5.96 45.82 10.49 47.81 \
  --asset-profile metadata-only \
  --once
```

By default a job fails visibly if a requested STAC key is absent. Use
`--allow-missing-assets` only when a partial custom profile is acceptable.

## State, errors and restart behaviour

The SQLite database uses WAL mode and contains:

- `runs`: every poll/cron cycle and its outcome;
- `jobs`: item status, attempts, next retry, last error and sidecar paths;
- `assets`: source URI, local path, attempts, byte count and SHA-256;
- `events`: timestamped item/asset errors and operational events;
- `ignored_items`: older first-scan products intentionally excluded in favour
  of the latest product for their tile;
- `settings`: the ROI fingerprint and canonical geometry.

Each scene directory also contains:

```text
scene_metadata.json
job_status.json
ASSET.jp2
ASSET.jp2.part   # only while interrupted/incomplete
```

Scene directories use Hive-style collection/year/month/day/tile/item
partitions. A top-level `dataset.duckdb` indexes every item and raster path;
automatic rotating logs are written below `_terravault/logs/`. See
[`DATASET_STORAGE.md`](DATASET_STORAGE.md).

`job_status.json` is rewritten atomically after meaningful state transitions.
It is the human-readable failure/provenance record; the SQLite database is the
authoritative queue. A job is `completed` only after every required asset has
the expected object length and a local SHA-256.

The asset-policy fingerprint is also stored. Switching the same database from
`metadata-only` or custom keys back to the default native profile automatically
reruns the latest-per-tile bootstrap, so the full baseline is not skipped.

Transient transfer failures move the job to `retry_wait` with exponential
backoff. Configure `--max-attempts`, `--retry-base-seconds` and
`--retry-max-seconds`. A terminal `failed` job remains in the database and
sidecar for investigation. Restart once with `--retry-failed` to explicitly
reset terminal jobs after addressing their cause; the event history remains.
If CDSE later modifies the STAC item so its asset set or href changes,
discovery requeues it automatically.

## Operational checks

```bash
# Full option reference
terravault watch --help

# Recent runs
sqlite3 var/switzerland-rolling.db \
  "select run_id, started_at, status, discovered, completed, failed, error from runs order by run_id desc limit 10;"

# Failed/retrying products
sqlite3 var/switzerland-rolling.db \
  "select item_id, status, attempts, next_attempt_at, last_error from jobs where status in ('retry_wait','failed');"
```

Back up the SQLite database and `job_status.json` files. Native imagery can be
recovered from CDSE, but those files contain the local processing history.

The current command uses polling STAC reconciliation. CDSE subscription
notifications, raster QA, COG/Zarr conversion and country-level virtual
mosaics remain downstream extensions described in
[`SWITZERLAND_SENTINEL2_PIPELINE.md`](SWITZERLAND_SENTINEL2_PIPELINE.md).
