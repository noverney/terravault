# Historical Sentinel-2 backfill

`terravault historic` gradually downloads complete, native-resolution
Sentinel-2 L2A history for a region. It uses the same required-asset policy,
resumable S3 transfers, checksums and failure sidecars as `terravault watch`.

## Minimal command

The region and start date are the only command-line values required:

```bash
terravault historic \
  --roi config/switzerland.geojson \
  --start-date 2024-01-01
```

Or use a WGS84 bbox:

```bash
terravault historic \
  --bbox 5.96 45.82 10.49 47.81 \
  --start-date 2024-01-01
```

CDSE S3 access and secret keys are loaded from `.env`. The default target end
is the instant the historical database is first created. Use an inclusive
fixed end date if needed:

```bash
terravault historic \
  --roi config/switzerland.geojson \
  --start-date 2024-01-01 \
  --end-date 2024-12-31
```

After the historical target completes, use `terravault watch` to follow new
products.

## Gradual progress and restart

The default window is one UTC day. For each window the worker:

1. discovers every intersecting L2A item without a catalogue result cap;
2. durably queues the complete required asset set;
3. downloads or resumes every item in that window;
4. waits for scheduled retries;
5. advances the durable `history_cursor` only when no active work remains.

Two progress bars show overall date-range progress and current-window item
progress. Stop with Ctrl-C/SIGTERM. The current window and `.part` file remain
restartable, and rerunning the same command continues from the saved cursor.

Use a larger window only when the expected item count and storage are known:

```bash
terravault historic \
  --bbox 5.96 45.82 10.49 47.81 \
  --start-date 2024-01-01 \
  --window-days 3
```

Use `--no-progress` for noninteractive logs.

## Complete native profile

The default profile requires all canonical assets before an item can complete:

- 10 m: B02, B03, B04, B08, AOT and WVP;
- 20 m: B05, B06, B07, B8A, B11, B12, SCL, snow and cloud probability;
- 60 m: B01 and B09;
- SAFE manifest and product, granule, datastrip and INSPIRE metadata.

No 20 m or 60 m layer is silently upsampled. Custom `--asset-keys` and
`--asset-profile metadata-only` are available for deliberate test runs.

## Quota and usage-limit handling

S3 HTTP 429/509 responses and standard throttling/quota error codes are
recognized separately from invalid credentials and ordinary transfer errors.
When a limit is reached, TerraVault:

- prints a visible notice beside the progress bar and writes a warning log;
- records the code, wait and attempt in SQLite and `job_status.json`;
- honours numeric or HTTP-date `Retry-After` values;
- otherwise waits `--quota-retry-seconds` (900 seconds by default);
- keeps the partial object and retries the same job;
- marks the job and current asset `retired` after `--max-attempts`
  (eight by default).

A retired job is final, so the historical cursor can continue rather than
blocking forever. After the quota resets or its cause is addressed, retry
retired/failed work explicitly:

```bash
terravault historic \
  --roi config/switzerland.geojson \
  --start-date 2024-01-01 \
  --retry-retired
```

This resets terminal attempts, revisits the historical windows and skips
already completed/checksummed assets.

## Operational examples

Slower quota fallback and smaller job batches:

```bash
terravault historic \
  --roi config/switzerland.geojson \
  --start-date 2024-01-01 \
  --quota-retry-seconds 3600 \
  --max-attempts 12 \
  --max-jobs-per-batch 20
```

Inspect terminal jobs:

```bash
sqlite3 terravault_history.db \
  "select item_id, status, attempts, last_error from jobs where status in ('failed','retired');"
```

The default transactional database is
`STORAGE_ROOT/_terravault/state/history.db`; use a separate database per ROI
and start date. The database is bound to both values and refuses an
incompatible restart.

All windows write into the shared partitioned dataset tree and top-level
`dataset.duckdb`. The historical retry database remains separate from that
analytical catalogue. Progress and every storage destination are recorded in
`STORAGE_ROOT/_terravault/logs/historic.log`. See
[`DATASET_STORAGE.md`](DATASET_STORAGE.md).
