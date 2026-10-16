# Copernicus-backed FORCE pipeline

`terravault force-pipeline` connects TerraVault's raw Copernicus L1C transfer
to native FORCE L2PS. It is a bounded, restartable run over one AOI and time
range—not a replacement for the continuous L2A `watch` worker.

## Selection

Selection happens before a complete SAFE is downloaded:

1. query the `sentinel-2-l1c` CDSE STAC collection by WGS84 bbox/geometry and
   acquisition range;
2. apply `eo:cloud_cover <= max_cloud_cover`;
3. keep only the requested S2A/S2B/S2C platforms;
4. for duplicate publications of one platform/acquisition/orbit/MGRS tile,
   keep the highest processing baseline, then latest production timestamp;
5. optionally retain only the newest `max_scenes` as a download safety cap.

This reproduces the useful input-selection behavior of FORCE's Level-1
archiving workflow while retaining CDSE STAC metadata and the existing
resumable S3/Product-ZIP download routes.
See FORCE's official
[Level-1 Cloud Storage Downloader](https://force-eo.readthedocs.io/en/latest/howto/level1-csd.html)
description for the original cloud/date/sensor and duplicate-selection model.

There are two independent cloud policies:

- `max_cloud_cover` is the catalogue estimate used to avoid downloads.
- `max_cloud_cover_frame` and `max_cloud_cover_tile` use FORCE's cloud
  detection after the L1C product has been downloaded and analyzed.

## CLI

```bash
terravault force-pipeline \
  --roi /data/roi/switzerland.geojson \
  --start-date 2026-07-01 \
  --end-date 2026-07-31 \
  --max-cloud-cover 20 \
  --sensors S2A S2B S2C \
  --output-root /data/force-native \
  --runtime docker \
  --dem /data/reference/switzerland_dem.tif \
  --scene-workers 2 \
  --clip-to-roi
```

`--scene-workers N` processes up to N downloaded SAFE products with FORCE at
the same time while later downloads continue. Each scene needs roughly 8 GiB
of memory, so on Docker Desktop raise the VM memory limit before raising N.
`--threads` defaults to the CPU count divided by N. `--clip-to-roi` passes the
`--roi`/`--bbox` geometry to FORCE as its AOI: pixels outside it become nodata
and tiles outside it are not written. On T32TMT with the Swiss boundary this
wrote 22 instead of 25 tiles and 21% fewer bytes, but saved only ~3% of FORCE
time, because cloud, aerosol and water-vapour estimation still cover the whole
scene. FORCE applies the AOI before its adjacency correction and resolution
merge, so reflectance within ~3 km of the AOI edge differs from an unclipped run
(most changes above 10 DN lie within 1 km). It changes the products, so an
existing output root processed without it needs `--overwrite` or a new root.

Use `--discover-only` to persist selection/metadata without pixels. Use
`--download-only` to build and validate the complete Level-1 pool without
starting L2PS. Both modes still create the catalogue and FORCE queue.

CDSE S3 credentials use `TERRAVAULT_CDSE_S3_ACCESS_KEY` and
`TERRAVAULT_CDSE_S3_SECRET_KEY`. When both are unavailable, the downloader can
use a CDSE bearer token or account credentials from the existing
`TERRAVAULT_CDSE_*` environment variables for the Product ZIP route.

## Small patches

FORCE always corrects a whole Sentinel-2 granule, so a patch still costs one
full L2PS run per acquisition. What a patch can save is everything around that
run. Measured on a 23 × 20 km patch around Lucerne (8.17–8.47° E,
46.975–47.155° N), July 2024, on an Apple M2 Max (12 cores) with Docker Desktop
limited to 23.4 GiB:

1. **Discover by the patch.** `--bbox`/`--roi` set to the patch selects only the
   granules that cover it. Keep the patch inside one MGRS tile's exclusive area
   (away from the ~10 km tile overlaps) and every acquisition is one SAFE: 12
   L1C products (8.6 GB, 6 min 15 s) for the month.
2. **Pass `--max-cloud-cover 100`** when comparing cloud masks. The catalogue
   estimate is granule-wide: only 2 of the 12 July 2024 acquisitions were under
   20%, but the per-pixel QAI decides what is usable inside the patch.
3. **Use a DEM** (`--dem`). Without one FORCE skips topographic correction and
   its cloud-shadow matching is weaker, which matters in the Alps. A Copernicus
   GLO-30 mosaic warped to Int16 LV95 with nodata `-32767` works.
4. **Clip with `--force-aoi` set to the patch buffered by ~5 km**, not
   `--clip-to-roi`. The buffer keeps FORCE's AOI edge effect outside the patch.
   Per scene this took 164 s instead of 205 s, wrote 168 MB instead of
   1.69 GB and 4 instead of 23 FORCE tiles. Inside the patch BOA differed from
   the unclipped run by at most 12 DN (0.0012 reflectance; more than 10 DN in
   0.0001% of pixels) and QAI was identical for 99.999% of pixels.
5. **Leave `--scene-workers` at 1 with all threads on this hardware.** One scene
   took 164 s at 12 threads, 206 s at 6 and 269 s at 4. Two workers at 6
   threads each slowed one another to 282 s per scene, 155 s of wall time per
   scene against 164–170 s for one worker — a 7% gain, and one of the 12 scenes
   failed with a FORCE "Error creating file" in its output tile under that
   load. It succeeded on `--retry-failed` alone. Each concurrent scene peaked at
   about 6.4 GiB of container memory.
6. **Leave the other L2PS settings at their defaults.** `--parallel-reads`,
   `--no-force-overview` and keeping FORCE's files off the bind mount gave the
   same products and no speed-up beyond run-to-run noise. The only faster
   resolution merge, `NONE`, saves about 6% and changes the products; see
   [L2PS settings on one scene](#l2ps-settings-on-one-scene).

The complete patch run:

```bash
terravault force-pipeline \
  --bbox 8.17 46.975 8.47 47.155 \
  --start-date 2024-07-01 --end-date 2024-07-31 \
  --max-cloud-cover 100 \
  --output-root ~/terravault-patch/lucerne/force-native \
  --runtime docker \
  --dem ~/terravault-patch/lucerne/reference/dem_t32tmt_glo30_lv95.tif \
  --force-aoi ~/terravault-patch/lucerne/roi/lucerne-patch-aoi-5km.geojson \
  --scene-workers 1 --threads 12 \
  --mount-root ~/terravault-patch/lucerne
```

`terravault monthly --bbox` then windows each acquisition to the patch before
screening it; see
[the monthly composites guide](SWITZERLAND_MONTHLY_COMPOSITES.md#small-patches).

### L2PS settings on one scene

Measured on 16 October 2026 with the scene, DEM and 5 km cutline of item 4
(S2A, 19 July 2024, T32TMT), `--threads 12`, one run at a time on an otherwise
idle machine. Seconds are `force-level2` wall time, with FORCE's own processing
time in brackets; MB is `du` of `level2`. Products are compared with the
default run inside the patch after warping each mosaic to EPSG:2056 at 10 m
(nearest). BOA columns count all 10 bands of every pixel.

| Variant | Seconds | MB | BOA identical | BOA max \|Δ\| (DN) | BOA > 10 DN | QAI identical |
|---|---|---:|---:|---:|---:|---:|
| Default (`IMPROPHE`, overview, serial reads), 3 runs | 167, 160, 159 (163, 157, 155) | 168.0 | 100% | 0 | 0% | 100% |
| `--no-force-overview`, 2 runs | 161, 166 (158, 163) | 167.9 | 100% | 0 | 0% | 100% |
| `--parallel-reads`, 2 runs | 157, 166 (154, 163) | 168.0 | 100% | 0 | 0% | 100% |
| `--parallel-reads --no-force-overview` | 158 (155) | 167.9 | 100% | 0 | 0% | 100% |
| Default, SAFE and all FORCE output on a container tmpfs¹ | (154) | 167.9 | 100% | 0 | 0% | 100% |
| `--resolution-merge NONE`, 2 runs² | 158, 148 (154, 145) | 168.7 | 14.7% | 10,512 | 41.6% | 99.81% |
| `--resolution-merge STARFM` | 219 (216) | 167.9 | 9.1% | 12,340 | 46.1% | 99.88% |
| `--resolution-merge REGRESSION` | 327 (323) | 166.7 | 12.3% | 15,454 | 45.7% | 99.88% |

¹ A hand-run `docker run` of the default parameter file with `DIR_LEVEL2`,
`DIR_TEMP`, `DIR_LOG`, `DIR_PROVENANCE`, the queue and a copy of the SAFE on a
tmpfs inside the container; the wrapper was not changed. Copying the 693 MB
SAFE in took 0.7 s and the 168 MB of output out 0.3 s.
² The second run also had `--parallel-reads --no-force-overview`.

**Speed only.** `--no-force-overview`, `--parallel-reads` and the tmpfs run
gave bit-identical BOA and QAI and stayed inside the default's 159–167 s range
(164 s on 15 October), so none of them helps. The overview is four quicklook
JPEGs (32 kB). Docker Desktop file sharing is not the bottleneck: FORCE kept
about 9 of the 12 cores busy on average (830–1020% in `docker stats` samples).
The rest is a ~30 s phase on exactly two cores and a few single-core steps,
which looked the same with parallel reads and on tmpfs, so it is FORCE's own
serial work.

**Resolution merge changes the products.** `NONE` saves about 9 s per scene
(6%). `STARFM` costs 35% more, mostly a minute on one core, and `REGRESSION`
twice the time. Every choice changes all ten bands, not only the six 20 m ones:

- 20 m bands (B05–B07, B8A, B11, B12): mean |Δ| 26–84 DN and 99th percentile
  180–470 DN (0.018–0.047 reflectance); 52–76% of pixels differ by more than
  10 DN.
- 10 m bands: B02–B04 mean |Δ| 2–4 DN (99th percentile 20–38 DN), B08
  13–16 DN (130–160 DN). QAI's water-vapour bit changes for about 0.1% of
  pixels, so the merged B8A probably reaches the 10 m bands through FORCE's
  per-pixel water-vapour estimate.
- There is almost no bias: the mean signed difference is under 4 DN in every
  band. On clear pixels NDRE and NDWI shift by at most ±0.002 on average but
  by 0.012–0.015 mean |Δ| and 0.08–0.10 at the 99th percentile per pixel.
  TerraVault's NDRE (B8A, B05), NDWI (B08, B11) and B05/B8A/B11 composite bands
  therefore depend on the setting; NDVI and GNDVI depend on it only through the
  small 10 m changes.
- QAI cloud, shadow and snow bits are identical. Only the subzero (8),
  saturation (9) and water-vapour (14) bits change. Under the monthly default
  mask `0x031F`, which includes bits 8 and 9, 0.04–0.08% of patch pixels
  switch between usable and masked.

These numbers measure how far the merges differ from each other, not which is
more accurate. Keep the default `IMPROPHE`, overview on and serial reads; at
about 160 s per scene this is as fast as these settings make L2PS on this Mac.
`NONE` is not worth 6% for a product that reports NDRE and NDWI, and choose
`STARFM` or `REGRESSION` only for scientific reasons.

## Python API

The convenience function exposes the arguments normally changed between runs:

```python
from datetime import datetime, timezone

from terravault import run_force_pipeline

result = run_force_pipeline(
    output_root="/data/force-native",
    start_datetime=datetime(2026, 7, 1, tzinfo=timezone.utc),
    end_datetime=datetime(2026, 8, 1, tzinfo=timezone.utc),
    bbox=(5.96, 45.82, 10.49, 47.81),
    max_cloud_cover=20,
    sensors=("S2A", "S2B", "S2C"),
    max_scenes=20,
    dem_path="/data/reference/switzerland_dem.tif",
    runtime="docker",
    target_crs="EPSG:2056",
    resolution=10,
    max_cloud_cover_frame=80,
    max_cloud_cover_tile=60,
    scene_workers=2,
    clip_to_selection=True,
)
```

Use the configuration classes when more FORCE tuning is needed:

```python
import os
from datetime import datetime, timezone
from pathlib import Path

from terravault import (
    CDSEDownloadAuthConfig,
    ForceDownloadOptions,
    ForceLevel2Options,
    ForcePipeline,
    ForcePipelineConfig,
)

config = ForcePipelineConfig(
    output_root=Path("/data/force-native"),
    start_datetime=datetime(2026, 7, 1, tzinfo=timezone.utc),
    end_datetime=datetime(2026, 8, 1, tzinfo=timezone.utc),
    bbox=(5.96, 45.82, 10.49, 47.81),
    max_cloud_cover=20,
    database_path=Path("/data/catalogues/swiss_force.duckdb"),
    auth=CDSEDownloadAuthConfig.from_env(os.environ),
    download=ForceDownloadOptions(max_retries=8),
    force=ForceLevel2Options(
        runtime="docker",
        dem_path=Path("/data/reference/switzerland_dem.tif"),
        cloud_buffer=300,
        shadow_buffer=90,
        max_cloud_cover_frame=80,
        max_cloud_cover_tile=60,
        scene_workers=2,
    ),
    clip_to_selection=True,
)

with ForcePipeline(config) as pipeline:
    result = pipeline.run()
```

`ForcePipeline.discover()` returns the raw catalogue count and de-duplicated
STAC items without writing anything. `ForcePipeline.run(download=False,
process=False)` is the durable metadata-only form.

## Catalogue and queue

The default DuckDB database is `OUTPUT_ROOT/force_images.duckdb`:

- `runs` records the exact selection policy and run counts;
- `scenes` records item/product identity, acquisition, tile, catalogue cloud
  cover, STAC metadata path, SAFE path, transfer state and FORCE state;
- `images` records local `L1C_SAFE`, `BOA`, `QAI`, `OVERVIEW`, `BOA_MOSAIC`
  and `QAI_MOSAIC` paths and file sizes.

Query it from Python:

```python
from terravault import ForcePipelineDatabase

with ForcePipelineDatabase("/data/force-native/force_images.duckdb") as database:
    clear_boa = database.images(image_type="BOA")
    scenes = database.scenes()
```

DuckDB holds queryable metadata and local paths, not large raster blobs. Build
an audit JPEG directly from the exact saved scene records (without another
catalogue query) with:

```bash
python examples/switzerland_patch/build_switzerland_overview.py \
  --dataset-db /data/force-native/force_images.duckdb \
  --database-selection all \
  --max-cloud-cover 20 \
  --output /data/force-native/switzerland_force_overview.jpg
```

Use the default `--database-selection latest-per-tile` for a less cluttered
companion view. Both modes write a JSON source manifest and register their
paths and source item IDs back into DuckDB.

`OUTPUT_ROOT/level1/queue.txt` is a standard FORCE file queue. A complete
download is `QUEUED`, a verified L2PS publication is `DONE`, and a processing
failure is `FAIL`. TerraVault's high-level wrapper hands each complete SAFE to
up to `scene_workers` isolated per-SAFE processors while downloads continue;
catalogue writes stay on the calling thread. On interruption, running FORCE
jobs are cancelled (their containers stopped) and recorded as `interrupted`.
The aggregate queue also remains usable by external FORCE tooling. The format follows the
official [FORCE file queue](https://force-eo.readthedocs.io/en/latest/components/lower-level/level1/queue.html)
contract.

## Output boundary

Each SAFE remains isolated under `level2/products/SAFE_STEM/`. The wrapper does
not combine products from the same acquisition into a country mosaic. It also
does not turn the native FORCE path into a permanent polling daemon; rerun the
same bounded command to reconcile a time range, or schedule it externally.
