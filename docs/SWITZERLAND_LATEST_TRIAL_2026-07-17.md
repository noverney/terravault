# Switzerland latest rolling trial — 2026-07-17

This records the live, reproducible validation run performed against the
Copernicus Data Space Ecosystem (CDSE) STAC catalogue on 2026-07-17.

This trial covers Sentinel-2 L2A discovery and the local external-feature
workflow. It does not validate native FORCE L2PS, which requires complete L1C
SAFE products. No successful real Zurich L1C → BOA/QAI run is claimed from
these artifacts. A later 2026-08-04 preflight found the configured S3 pair
rejected with `InvalidAccessKeyId` and no Product bearer/account fallback, so
the complete L1C download did not start.

## Command

The discovery/catalog portion used the full Switzerland convenience bounding
box and intentionally selected `metadata-only`, because the environment had a
Sentinel Hub OAuth client but no CDSE S3 access key/secret:

```bash
terravault watch \
  --once \
  --bbox 5.96 45.82 10.49 47.81 \
  --asset-profile metadata-only \
  --bootstrap-lookback-days 14 \
  --max-jobs-per-cycle 1000 \
  --storage-root /tmp/terravault_switzerland_latest_trial_20260717_v2
```

Do not substitute Sentinel Hub OAuth client credentials for S3 credentials.
Native rolling transfers require both:

```dotenv
TERRAVAULT_CDSE_S3_ACCESS_KEY=...
TERRAVAULT_CDSE_S3_SECRET_KEY=...
```

## Observed result

- catalogue time window: 2026-07-03T11:27:41Z through
  2026-07-17T11:27:41Z;
- 177 Sentinel-2 L2A items intersected the bbox;
- footprint-aware latest-per-tile bootstrap selected 20 MGRS tiles;
- 20 jobs completed, 0 failed, 0 errors;
- acquisition range of selected products: 2026-07-11 through 2026-07-16;
- DuckDB contains 20 completed item rows and the partition tree contains 20
  `scene_metadata.json` documents;
- 157 older bootstrap products are durably marked ignored;
- metadata-only trial size: 6.2 MiB.

Selected tiles:

```text
31TGL 31TGM 31TGN 31UGP
32TLR 32TLS 32TLT 32TMR 32TMS 32TMT
32TNR 32TNS 32TNT 32TPR 32TPS 32TPT
32ULU 32UMU 32UNU 32UPU
```

The exact same command was run a second time. It entered `new-product follow`
mode, reconciled 17 overlap items, queued 0, and produced 0 duplicates. This
validates stop/restart state at the live catalogue boundary.

Trial artifacts:

```text
/tmp/terravault_switzerland_latest_trial_20260717_v2/
├── dataset.duckdb
├── _terravault/state/rolling.db
├── _terravault/logs/watch.log
├── _terravault/logs/query.log
└── pieces/collection=sentinel-2-l2a/...
```

The artifact root is temporary and may be removed by the operating system.

## Native data and stitched-output sizing

Visual inspection of the first literal-newest selection exposed partial
edge-of-swath products that left large black gaps. The implemented bootstrap
now chooses the newest footprint at least 90% as large as the tile's largest
footprint in the lookback window. The rerun recorded here produced a continuous
4000×2568 public-thumbnail overview.

All 20 selected STAC documents expose all 22 keys in TerraVault's native
profile: 17 raster features plus 5 provenance XML assets. Their catalogue
`file:size` values total 16,280,636,337 bytes (15.16 GiB) for this particular
footprint-aware latest-per-tile selection.

At 10 m in EPSG:2056, the Switzerland bounding rectangle is approximately
35,205 by 22,460 pixels. A 17-band UInt16 stitched output is therefore about
25 GiB uncompressed; a three-band RGB result is about 4.4 GiB uncompressed.
Compression changes file size but not the conservative extraction guard.

The current machine reported only 34 GiB free during the trial. Downloading
15.16 GiB of native pieces and immediately building the complete 17-band 10 m
COG would leave little safety margin. Use a larger data volume or first request
RGB/a smaller feature set.

## Continue after S3 credentials are configured

Use a persistent storage root with sufficient free space:

```bash
terravault watch \
  --once \
  --roi config/switzerland.geojson \
  --storage-root /data/terravault/switzerland
```

Changing the metadata-only asset policy to native causes the watcher to rerun
latest-per-tile bootstrap. Transfers are range-resumable and remain partitioned
by date/tile/item.

Inspect an RGB country extraction before writing it:

```bash
terravault extract \
  --dataset-db /data/terravault/switzerland/dataset.duckdb \
  --roi config/switzerland.geojson \
  --asset-keys B04_10m B03_10m B02_10m \
  --output /data/terravault/exports/switzerland_latest_rgb.tif \
  --target-crs EPSG:2056 \
  --resolution 10 \
  --dry-run
```

Repeat without `--dry-run` after checking the manifest estimate. See
[RASTER_QUERY_AND_EXTRACTION.md](RASTER_QUERY_AND_EXTRACTION.md) for the
all-feature command and memory/output-size controls.

## Native ARM64 FORCE L2PS integration result

The retained complete L1C product for MGRS tile `32TMT` was processed with the
repository-built `terravault/force:3.10.04-arm64` image on Apple Silicon. This
is a Zurich-area integration fixture, not the 20-product country-wide run.

- FORCE core processing completed in approximately 6.5 minutes, compared with
  more than 40 minutes without output from the stopped AMD64/QEMU attempt;
- peak observed use was approximately two CPU cores and 7.6 GiB RAM;
- FORCE produced and TerraVault validated 25 BOA, 25 QAI, and 25 OVV tiles;
- validated raster output totaled 2,153,371,382 bytes before VRT publication;
- the queue reached `DONE`, and BOA/QAI mosaics were published atomically under
  `satellite_data/switzerland_ndvi/force_native/level2/products/`;
- the three-panel Zurich diagnostic reported raw, CDSE-masked, and FORCE-masked
  valid coverage of 99.88%, 94.58%, and 89.74% respectively.

The real run also established two FORCE 3.10.04 output contracts now covered
by tests: OVV JPEGs are plain RGB quicklooks without embedded georeferencing,
and ARM64 FORCE metadata can contain non-UTF-8 bytes that must be replaced when
copied into VRT XML. Raster grids, BOA/QAI metadata, tile coverage, VRT source
sets, and the original processing logs remain strictly validated.
