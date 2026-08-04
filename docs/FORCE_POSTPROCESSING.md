# FORCE postprocessing

TerraVault integrates [FORCE](https://github.com/davidfrantz/force) as a
pinned Git submodule and as an optional postprocessing runtime. Apple Silicon
hosts default to TerraVault's native `terravault/force:3.10.04-arm64` image;
other hosts default to `davidfrantz/force:3.10.04`. Both paths verify that
`force-info` reports exactly FORCE 3.10.04 before processing.
A Docker tag is mutable; use a digest or controlled registry if byte-for-byte
image identity is required.

## Compatibility boundary

TerraVault implements two FORCE paths. They solve different problems and must
not be described as equivalent.

### L2A external-feature bridge

```text
TerraVault DuckDB + native JP2 pieces
              │
              ▼
  memory-bounded stitched COG
  B04 | B08 | SCL | CLD, 10 m, Int16
              │
              ▼
       FORCE force-cube
  external-feature tiles, 30 km grid
              │
              ▼
      FORCE force-mosaic VRT
```

This preserves honest provenance: the output is a FORCE-tiled **external
feature**, not a retroactively created FORCE Level-2 product. `force-cube`
supports raster inputs and FORCE documents Byte and Int16 as the types its
higher-level system understands correctly. TerraVault therefore writes
Int16 features with `-9999` nodata and uses nearest-neighbour resampling so
SCL and CLD values are not interpolated.

### Native L1C Level-2 processing

```text
complete Sentinel-2 MSIL1C SAFE / SAFE ZIP
       │  metadata + every band, including B10
       ▼
       FORCE L2PS
  cloud/cirrus/shadow/snow classification
  atmospheric + BRDF correction
       │
       ▼
 level2/products/<SAFE-stem>/
       ├── tiled BOA COGs ──► per-SAFE BOA mosaic VRT
       ├── bit-packed QAI COGs ──► per-SAFE QAI mosaic VRT
       └── per-tile OVV JPEG quicklooks
```

This path is exposed as `terravault force-level2`. It creates genuine FORCE
BOA and QAI products. The input must be exactly one complete
`S2[ABC]_MSIL1C_*.SAFE` directory or `.SAFE.zip`; TerraVault verifies
`manifest.safe`, `MTD_MSIL1C.xml`, every granule's `MTD_TL.xml`, and all 13
L1C bands (B01–B12 plus B8A) before starting FORCE. A selected L2A archive,
the four-band bridge COG, a Process API TIFF, or a renamed partial ZIP cannot
satisfy this requirement.

## Get the pinned source

New clone:

```bash
git clone --recurse-submodules YOUR_TERRAVAULT_REMOTE
cd terravault
```

Existing clone:

```bash
git submodule update --init --recursive
```

The gitlink is pinned to FORCE v3.10.04
(`08dc60f17289c1868bd4f628f6675441b6735776`). The submodule provides the
matching source and documentation. Runtime commands request the matching
official Linux image tag and independently verify its reported FORCE version.
The source commit is fixed by the gitlink; the Docker tag itself is not an
immutable image identifier.

On a case-insensitive macOS filesystem, upstream sensor definitions whose
names differ only by case can make the submodule appear internally modified
after checkout. `.gitmodules` ignores that platform-only dirty indication;
the pinned gitlink remains unchanged. Do not commit replacements inside
`vendor/force`.

## Install and verify the runtime

Docker is the recommended route. On Apple Silicon, build the native image from
the pinned submodule (the build does not modify `vendor/force`):

```bash
docker build --platform linux/arm64 \
  -f docker/force-arm64.Dockerfile \
  -t terravault/force:3.10.04-arm64 .
docker run --rm --platform linux/arm64 \
  terravault/force:3.10.04-arm64 force-info
```

On AMD64 Linux, the official image remains available:

```bash
docker pull --platform linux/amd64 davidfrantz/force:3.10.04
docker run --rm --platform linux/amd64 davidfrantz/force:3.10.04 force-info
```

FORCE is developed and tested on Ubuntu; upstream does not support migrating
it to other operating systems. `--runtime auto` therefore considers a native
FORCE installation only on Linux. On macOS it always selects Docker, even if
commands named `force-*` happen to be on `PATH`. An explicit `--runtime
native` remains available for developers deliberately testing a port.

The official v3.10.04 image is `linux/amd64`, which Docker Desktop would run
through slow QEMU emulation on Apple Silicon. TerraVault's Dockerfile compiles
the pinned FORCE sources for `linux/arm64`, adapts upstream's hard-coded Debian
x86_64 library paths during the image build, and includes every command used by
the external-feature and L2PS workflows. FORCE still runs inside supported
Ubuntu Linux; it is not linked against macOS libraries.

Docker commands pin the host-appropriate Linux platform, mount only the smallest common
parent containing the input and output, map the host user to avoid root-owned
products, and use a writable temporary home for FORCE's GNU Parallel workers.
TerraVault also pins `SHELL=/bin/bash`; otherwise an unlisted host UID can
make GNU Parallel fall back to `/bin/sh`, which cannot execute FORCE's
exported Bash tile worker. Override `--docker-platform` only when using a
tested custom/multi-architecture image. The defaults can be overridden with
`TERRAVAULT_FORCE_DOCKER_IMAGE` and `TERRAVAULT_FORCE_DOCKER_PLATFORM` or the
matching CLI options.

FORCE can also be compiled natively on a supported Linux system by following
its installation documentation. The external-feature path checks
`force-info`, `force-cube-init`, `force-cube`, `force-mosaic`, and GDAL. The
native Level-2 path checks `force-info`, `force-level2`, `force-l2ps`,
`force-mosaic`, `gdalinfo`, and `gdalsrsinfo` before starting, then rejects a
runtime whose reported FORCE version is not exactly 3.10.04.

## Run native L1C → BOA/QAI processing

### Local complete SAFE

Use the helper wrapper or the equivalent CLI command:

```bash
python examples/postprocessing/force_level2.py \
  --input /data/switzerland/l1c/S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T142404.SAFE.zip \
  --output-root /data/switzerland/force-native \
  --runtime docker \
  --dem /data/reference/switzerland_dem.tif
```

`--runtime auto` uses native FORCE only on Linux when all required commands
are present; otherwise it uses Docker. All input, output, AOI, and DEM paths
must be below one safely scoped Docker mount root. Specify `--mount-root` when
their smallest common parent would otherwise be the filesystem root.

### Download from a saved L1C STAC Item

`--stac-item` accepts a local JSON file containing one Sentinel-2 L1C STAC
Feature (or a one-feature FeatureCollection). It is metadata input, not a URL.
The item must contain the complete SAFE product name and at least one complete
download route:

- With both `TERRAVAULT_CDSE_S3_ACCESS_KEY` and
  `TERRAVAULT_CDSE_S3_SECRET_KEY`, TerraVault follows the item's
  `safe_manifest` `s3://` URI and downloads every non-empty object below that
  SAFE prefix into a hidden staging directory, verifies it, then publishes it
  as `level1/PRODUCT.SAFE/`.
- Otherwise, with `TERRAVAULT_CDSE_ACCESS_TOKEN` or CDSE
  username/password/TOTP, TerraVault Range-resumes the item's authenticated
  `Product` asset into `level1/PRODUCT.SAFE.zip`.

```bash
terravault force-level2 \
  --stac-item /data/stac/S2A_MSIL1C_item.json \
  --output-root /data/switzerland/force-native \
  --runtime docker \
  --download-max-retries 5 \
  --dem /data/reference/switzerland_dem.tif
```

The S3 access/secret pair and the CDSE Product bearer/account flow are
alternative download routes. The Sentinel Hub client ID/secret used by the
Process API authenticates neither route. Static bearer tokens expire; refresh
`TERRAVAULT_CDSE_ACCESS_TOKEN` or use the account flow when CDSE rejects one.
Do not use `--dry-run` with `--stac-item`, because the SAFE must exist before
TerraVault can validate and plan it. Download once, then pass the resulting
local `--input` for later dry runs.

CDSE `Retry-After` values, including HTTP-date values, are honoured. When the
server does not supply one, `--download-quota-wait-seconds` supplies the quota
pause; ordinary network errors use exponential backoff from
`--download-retry-base-seconds`. `--download-max-retries` caps both retry
loops. Ctrl-C or SIGTERM records an interrupted state, terminates the active
native process group, and retains resumable `.part` or staging data.

### Swiss grid and processing defaults

The native command defaults to the same stable Swiss cube grid as the
external-feature bridge:

| Setting | Default |
|---|---:|
| CRS | `EPSG:2056` |
| Pixel size | 10 m |
| FORCE tile size | 30,000 m (3000 × 3000 pixels) |
| WGS84 grid origin | 5.5° E, 48.0° N |
| Resolution merge | `IMPROPHE` |
| Concurrent scenes | 1 |
| Threads per scene | 2 |
| Cloud/cirrus/shadow/snow buffers | 300 / 0 / 90 / 30 m |
| Cloud/shadow thresholds | 0.225 / 0.02 |
| Maximum frame/tile cloud cover | 100% / 100% |
| Output format | COG, with FORCE overview enabled |

The 10 m output grid is the highest Sentinel-2 spatial resolution; bands
whose native resolution is coarser are merged according to `--resolution-merge`.
FORCE writes multiband BOA plus a one-band, bit-packed QAI product for each
intersecting FORCE tile.

The generated Level-2 parameters enable atmospheric correction, image-based
AOD estimation, adjacency and multiple-scattering correction, nadir BRDF
correction, and impulse-noise filtering. `ERASE_CLOUDS = FALSE` keeps BOA
reflectance pixels and carries quality decisions in QAI. The standard BOA,
QAI, and overview are emitted; optional standalone AOD, WVP, view-angle, HOT,
and cloud-distance diagnostic products are not enabled by this wrapper.

A DEM is optional syntactically but strongly recommended scientifically.
Without `--dem`, TerraVault emits a warning, writes `FILE_DEM = NULL` and
`DO_TOPO = FALSE`, and FORCE continues with reduced atmospheric and
cloud-shadow quality. Use a DEM covering the complete L1C footprint, with its
actual nodata value supplied through `--dem-nodata` when it differs from
`-32767`.

One invocation processes and publishes one complete SAFE. Multiple products
may share an output root, but each is isolated below
`level2/products/<SAFE-stem>/`. Separate SAFE products from the same date and
sensor can contain identically named FORCE chips; the per-SAFE directories
prevent them from merging or overwriting one another.

`_terravault/force-l2/cube.json` records the CRS/WKT, grid origin, tile size,
resolution, and required FORCE version. That definition is immutable for the
output root—even `--overwrite` cannot change it. Use a new output root for a
different grid or projection. The global worker lock serializes operations
sharing the root, and `--processes 1` remains conservative because one
Sentinel-2 scene can require roughly 8 GiB of memory.

A Swiss-wide date requires several products and is not one simultaneous
exposure. `force-level2` can process those SAFE products one at a time, but it
does not combine their isolated results into an acquisition-level or national
FORCE mosaic. That downstream pool/mosaic layer is still to be implemented.

### Current validation status

The local Zurich artifact in this repository was successfully created through
the L2A external-feature bridge. It is not evidence of native L2PS. A real
Zurich L1C → BOA/QAI run is not claimed at this point. In the 2026-08-04
preflight, CDSE rejected the configured S3 pair with `InvalidAccessKeyId`, and
no Product bearer/account fallback was configured, so the complete SAFE was
not acquired. Create a fresh S3 pair or configure the Product route before
treating a live L2PS run as verified.

## Import an existing TerraVault COG

Plan without changing the cube:

```bash
terravault force \
  --input satellite_data/switzerland_ndvi/exports/zurich_ndvi_inputs_example.tif \
  --output-root satellite_data/switzerland_ndvi/force \
  --runtime docker \
  --dry-run
```

Run:

```bash
terravault force \
  --input satellite_data/switzerland_ndvi/exports/zurich_ndvi_inputs_example.tif \
  --output-root satellite_data/switzerland_ndvi/force \
  --runtime docker
```

The input must be a readable georeferenced raster with nodata defined on
every band. The default Swiss grid is EPSG:2056, 10 m pixels, a 30 km tile
size and a stable WGS84 origin at 5.5° E, 48° N. A tile size must be an exact
multiple of its resolution. Use a different output root if any grid setting
changes.

## L2A external-feature Switzerland snapshot

Plan the full national extraction:

```bash
python examples/postprocessing/force_switzerland.py --dry-run
```

Create the latest-per-tile Swiss COG and FORCE cube:

```bash
python examples/postprocessing/force_switzerland.py \
  --dataset-db satellite_data/switzerland_ndvi/dataset.duckdb \
  --output-root satellite_data/switzerland_ndvi/force \
  --runtime docker
```

For one historical day:

```bash
python examples/postprocessing/force_switzerland.py \
  --start-date 2026-07-17 \
  --end-date 2026-07-17 \
  --staging-raster satellite_data/switzerland_ndvi/staging/switzerland_20260717.tif
```

Sentinel-2 does not observe every Swiss tile at one identical instant. The
date-limited result is a latest-per-tile daily composite; use the extraction
manifest's acquisition times when interpreting it as a country overview.

The convenience bbox includes a small area outside the border. Pass a Swiss
Polygon/MultiPolygon GeoJSON with `--roi` when the output must follow the
national boundary exactly.

The current national dry run is approximately 35,205 × 22,569 pixels with
four Int16 bands, or 5.92 GiB uncompressed. Compression reduces actual files
but varies by scene. Keep enough disk for the immutable JP2 pieces, staging
COG, FORCE chips and temporary files; 15–25 GiB of free working space above
the source archive is a practical starting point. Python never loads this
array: GDAL streams the COG with a configurable `--warp-memory-mib` budget,
and FORCE processes it tile by tile.

## Visualize and compare cloud masks

FORCE mosaics are VRT datasets: they provide a joined georeferenced view but
are not conventional pictures. Create an analysis-ready NDVI COG and a
viewable quicklook with:

```bash
terravault force-visualize \
  --input satellite_data/switzerland_ndvi/force_zurich_example/datacube/mosaic/switzerland_ndvi_20260717T103029Z_a9fbf8e0c6.vrt
```

Equivalent example script:

```bash
python examples/postprocessing/visualize_force.py --input FORCE_MOSAIC.vrt
```

Default outputs are written under `FORCE_ROOT/visualizations/`:

```text
FEATURE_ndvi.tif
FEATURE_ndvi.png
FEATURE_ndvi.wld
FEATURE_ndvi_before_after.png
FEATURE_ndvi.visualization.json
_terravault/logs/force-visualize.log
```

The GeoTIFF is a georeferenced Float32 COG with `-9999` nodata. The PNG is a
compact RGBA color relief, and its world file retains map placement. By
default, `FEATURE_ndvi_before_after.png` places raw B04/B08 NDVI beside the
quality-masked result on the same NDVI color scale. It labels valid-pixel
percentages and the percentage points removed by the mask, making cloud and
classification behavior immediately visible during debugging. This
comparison is a presentation image, not a georeferenced raster; use the COG
or the PNG/world-file pair for spatial work.

NDVI is calculated from physical reflectance, not directly from stored DN.
TerraVault requires and applies the B04/B08 scale and offset recorded by the
extraction band contract (or its exact legacy STAC provenance), casts both
operands to Float32 before arithmetic, and records that contract in the
visualization manifest. An L2A-looking input without durable calibration
provenance is rejected rather than silently assuming an identity transform.

When a genuine FORCE QAI mosaic from `force-level2` represents the same
date/sensor, add it to the same visualization command:

```bash
terravault force-visualize \
  --input FORCE_EXTERNAL_ROOT/datacube/mosaic/FEATURE.vrt \
  --force-qai FORCE_NATIVE_ROOT/level2/products/SAFE_STEM/mosaic/20260717_LEVEL2_SEN2A_QAI.vrt
```

That QAI VRT belongs to one SAFE. Choose an L2A input with matching
date/sensor and spatial provenance. `force-level2` does not currently create a
country-wide QAI mosaic across the sibling directories under `products/`.

The additional outputs are:

```text
FEATURE_ndvi_force_qai.tif
FEATURE_ndvi_force_qai.png
FEATURE_ndvi_force_qai.wld
FEATURE_ndvi_raw_cdse_force.png
```

The three-panel `*_raw_cdse_force.png` has deliberately controlled semantics:

1. **Raw:** NDVI from the external-feature mosaic's L2A B08 and B04, with
   only input nodata and zero denominators removed.
2. **CDSE mask:** the same L2A NDVI with the selected L2A SCL classes and CLD
   probability policy removed.
3. **FORCE QAI mask:** the same L2A NDVI with the aligned FORCE QAI bits
   removed.

The third panel does **not** calculate NDVI from FORCE BOA. Holding the L2A
B04/B08 pixels constant makes differences attributable to masking. A separate
BOA-versus-L2A reflectance comparison would answer a different question about
atmospheric correction. QAI is nearest-neighbour aligned to the L2A grid, and
the visualization manifest records source/aligned sizes, the bit mask, date
matching, statistics, and exact GDAL commands.

The default `--force-qai-mask 0x031F` matches FORCE's standard higher-level
screening set:

| Hex component | FORCE QAI flag(s) screened |
|---:|---|
| `0x0001` | NODATA (bit 0) |
| `0x0006` | Any non-clear two-bit cloud state: opaque, buffer, or cirrus (bits 1–2) |
| `0x0008` | CLOUD_SHADOW (bit 3) |
| `0x0010` | SNOW (bit 4) |
| `0x0100` | SUBZERO reflectance (bit 8) |
| `0x0200` | SATURATION (bit 9) |
| **`0x031F`** | **Combined default** |

Water, aerosol state, low sun, illumination, slope, and missing-water-vapour
flags are intentionally not part of `0x031F`. Supply a different integer or
hex mask only when the analysis policy requires it. When extraction provenance
and a standard FORCE filename are available, TerraVault rejects a date/sensor
mismatch by default; `--allow-force-time-mismatch` is for an explicit
diagnostic, not routine processing.

Install the small plotting dependency with
`pip install "terravault[visualization]"`. Use `--no-debug-plot` to omit the
comparison, or `--debug-plot-width` to control its total width. By default,
the calculation:

- uses band descriptions to locate B04, B08, SCL and CLD;
- converts raw B04/B08 values to physical reflectance with their recorded
  scale and offset before calculating NDVI;
- masks SCL 0, 1, 3, 8, 9, 10 and 11;
- masks CLD values above 50%;
- excludes nodata and zero denominators;
- crops FORCE tile padding to the original TerraVault input bounds;
- records source files, palette, statistics and exact GDAL commands.

Use `--cloud-threshold`, explicit band-number options,
`--no-quality-mask`, `--force-qai-mask`, or `--no-crop-to-force-input` to
change those choices. Unchanged inputs are skipped using the visualization
manifest; use `--overwrite` after intentionally changing parameters.

## Rolling operation and cron

The implemented `terravault watch` worker follows Sentinel-2 **L2A** and the
`force_switzerland.py` external-feature wrapper reads only local data, so that
postprocessing step needs no Copernicus credential:

```cron
*/15 * * * * cd /opt/terravault && /opt/terravault/.venv/bin/terravault watch --once --bbox 5.96 45.82 10.49 47.81 --asset-profile ndvi --storage-root /data/switzerland_ndvi && /opt/terravault/.venv/bin/python examples/postprocessing/force_switzerland.py --dataset-db /data/switzerland_ndvi/dataset.duckdb --staging-raster /data/switzerland_ndvi/staging/switzerland_latest_ndvi_inputs.tif --output-root /data/switzerland_ndvi/force
```

Before creating a country COG, the wrapper compares the exact selected
item/asset set with the existing staging manifest. If nothing new was
selected, it reuses the COG. New source items produce a deterministic FORCE
basename containing the latest acquisition and a source-set digest. Repeating
the same command is therefore idempotent; use `--refresh-staging` only to
rebuild identical source pixels.

For stricter scheduling, run acquisition and postprocessing as separate
systemd timers or jobs and start postprocessing only after `terravault watch
--once` exits successfully. The rolling SQLite lock already prevents two
acquisition workers from sharing one queue. Do not start two FORCE imports
against the same output root simultaneously.

This cron entry does not run native L2PS. `terravault force-level2` is only the
durable one-L1C-product processing primitive. Neither `watch` nor `historic`
currently discovers L1C items and dispatches FORCE jobs, and no active Swiss
rolling FORCE scheduler is included yet. A future scheduler may invoke it for
each saved L1C STAC item, but calls sharing one output root must remain
serialized by `_terravault/force-l2/worker.lock`.

## State, logs and restart behaviour

```text
satellite_data/switzerland_ndvi/
├── dataset.duckdb
├── staging/
│   ├── switzerland_latest_ndvi_inputs.tif
│   └── switzerland_latest_ndvi_inputs.tif.manifest.json
└── force/
    ├── datacube/
    │   ├── datacube-definition.prj
    │   ├── X0007_Y0002/FEATURE_NAME.tif
    │   └── mosaic/FEATURE_NAME.vrt
    └── _terravault/
        ├── force/
        │   ├── cube-config.json
        │   └── jobs/FEATURE_NAME.json
        └── logs/
            ├── force.log
            └── force-switzerland.log
```

Each job manifest is written atomically and records:

- state (`running`, `failed` or `complete`) and attempt count;
- input path, byte count and SHA-256;
- source/grid fingerprint and exact FORCE commands;
- configured FORCE image tag/platform;
- chip paths, mosaic path and error text.

After interruption, rerun the same command. A completed matching job with
present chips is skipped. A failed job increments its attempt counter and
retries. FORCE's shell utilities can return success even if a child tile
worker fails, so TerraVault additionally verifies that chips and the mosaic
VRT actually exist. A completed basename cannot silently change ownership:
choose a new `--basename`, or intentionally pass `--overwrite`.

Logs rotate at 25 MiB with ten generations and contain input, cube, command,
progress and output paths but no Copernicus secrets.

### Native L2PS layout

```text
FORCE_NATIVE_ROOT/
├── level1/
│   └── S2A_MSIL1C_....SAFE[.zip]
├── level2/
│   └── products/
│       └── SAFE_STEM/
│           ├── X####_Y####/
│           │   ├── YYYYMMDD_LEVEL2_SEN2A_BOA.tif
│           │   ├── YYYYMMDD_LEVEL2_SEN2A_QAI.tif
│           │   └── YYYYMMDD_LEVEL2_SEN2A_OVV.jpg
│           ├── datacube-definition.prj
│           └── mosaic/
│               ├── YYYYMMDD_LEVEL2_SEN2A_BOA.vrt
│               └── YYYYMMDD_LEVEL2_SEN2A_QAI.vrt
└── _terravault/
    ├── logs/force-level2.log
    └── force-l2/
        ├── cube.json
        ├── downloads/ITEM_ID.json
        ├── jobs/S2A_MSIL1C_....json
        ├── progress/S2A_MSIL1C_....json
        ├── parameters/S2A_MSIL1C_....prm
        ├── queues/S2A_MSIL1C_....txt
        ├── logs/S2A_MSIL1C_....batch.log
        ├── logs/SAFE_STEM/...
        ├── provenance/SAFE_STEM/...
        ├── attempts/SAFE_STEM/level2/...  # transient; removed after publish
        └── worker.lock
```

The per-SAFE `*.batch.log` is written continuously while the FORCE child runs,
including its exact command, timestamps, output, interruption marker, and exit
code. FORCE's own product logs remain alongside it under `logs/SAFE_STEM/`.

For a concise live snapshot from a second shell:

```bash
terravault force-status \
  --output-root FORCE_NATIVE_ROOT \
  --job SAFE_STEM
```

Add `--json` for monitoring software. Each new `force-level2` run also writes
`_terravault/force-l2/progress/SAFE_STEM.json` and emits a heartbeat at the
configurable `--progress-interval-seconds` interval. Observable progress
includes phase, elapsed seconds, queue state, BOA/QAI/OVV counts, bytes, and
last output time. `within_scene_percent` is deliberately null: `force-l2ps`
provides no reliable internal percentage, and GNU Parallel's one-scene `100%`
describes a busy slot rather than completed image processing.

Download manifests are atomically replaced after each completed S3 object or
Product ZIP transition and record the source mode, source fingerprint,
expected/completed files and bytes, current status, error, staging path, and
per-object SHA-256 values. S3 objects are individually resumable in
`level1/.PRODUCT.SAFE.partial/`; an exact file-list, size, hash, and complete
SAFE-structure check is required before the directory replaces the last
verified publication. A legacy manifest without hashes is rebuilt once rather
than accepted from file sizes. HTTP Product downloads retain a `.part` file,
use Range requests, and validate an advertised SHA-256 when available.

The native processing job manifest records the complete input and auxiliary
signatures, settings fingerprint, required and reported FORCE versions,
configured image/runtime, product identity, `cube.json`, attempt count, queue
state, parameter/log/provenance paths, exact commands, BOA/QAI paths, mosaics,
and any error. FORCE returning success is insufficient: TerraVault requires
queue state `DONE`, non-empty BOA and QAI chips with identical tile coverage,
the requested CRS, north-up pixel grid, exact tile dimensions/alignment,
matching OVV coverage and cube-aligned origins when enabled, strict BOA/QAI/OVV
band metadata, and valid VRTs whose source lists exactly match those chips.

Pixels are first written below
`_terravault/force-l2/attempts/<SAFE-stem>/level2/`. Only after all checks pass
is that directory atomically published to
`level2/products/<SAFE-stem>/`. An interruption or failed attempt cannot
damage another SAFE or replace the last verified publication. On retry,
TerraVault discards only that SAFE's partial attempt and starts its FORCE work
cleanly; a failed manifest requires `--retry-failed`.

Rerunning an identical complete job skips it. Changed input, DEM/AOI, or
processing settings require `--overwrite`; replacement is still confined to
that SAFE. Grid changes are different: `cube.json` rejects them regardless of
`--overwrite`, so they require a new output root. The lock rejects a second
worker rather than risking concurrent state writes. `--overwrite` is
intentional per-product replacement, not a quota/network retry switch.

Sibling SAFE directories—even when their FORCE filenames share the same date
and sensor—are never merged by this publication step. There is intentionally
no pooled `level2/mosaic/` at the output-root level.

## Country-wide native rollout plan

The repository now has the resumable per-product L1C download/L2PS building
block, but not a Swiss native FORCE scheduler or pooled dataset. A production
service should add the following orchestration around it:

1. discover `sentinel-2-l1c` STAC items intersecting the versioned Swiss
   polygon, using an overlap window and item-ID deduplication;
2. persist each complete STAC Feature and a durable per-product queue entry;
3. acquire the complete SAFE via S3 (preferred) or the Product ZIP fallback;
4. invoke `force-level2` sequentially into one root with an immutable
   EPSG:2056 `cube.json`, treating only each verified per-SAFE BOA/QAI/OVV
   publication and its two BOA/QAI mosaics as complete;
5. on interruption, resume the saved download/job manifests; on quota errors,
   honour `Retry-After` and retain the queue state rather than discarding it;
6. build a separate, provenance-aware acquisition/national pool or virtual
   mosaic across the isolated SAFE publications; this does not exist today;
7. compare QAI only with an L2A extraction whose date, sensor, and footprint
   provenance match the selected per-SAFE QAI;
8. alert on expired credentials, stale queue age, failed jobs, missing DEM
   coverage, and unexpectedly absent Swiss tiles.

Each per-SAFE publication contains genuine native BOA/QAI tiles and mosaics,
but `level2/products/` is not itself a pooled FORCE time-series cube. A future
aggregation step must construct that input before country-wide FORCE Higher
Level Processing. The imported L2A external-feature cube remains separate and
is suitable only for operations that explicitly accept that feature layout.

## When a FORCE fork is warranted

No upstream FORCE source has been modified by this integration. The macOS
handling, WKT normalization, container environment and child-output checks
live in TerraVault's wrapper, while the submodule still points to
`davidfrantz/force`.

Create a fork only when a required fix belongs inside FORCE itself—for
example, a multi-architecture Dockerfile, a portable replacement for a
Linux-only dependency, or an accepted change to `force-cube`. At that point:

1. fork `davidfrantz/force` under the project/user GitHub account;
2. create a versioned branch such as `terravault-v3.10.04`;
3. add tests and retain the upstream remote for rebasing;
4. change `.gitmodules` to the fork URL and pin an exact tested commit;
5. build a versioned image such as `PROJECT/force:3.10.04-terravault.1`;
6. update `FORCE_DOCKER_IMAGE`, `FORCE_DOCKER_PLATFORM` and the integration
   tests together.

Do not point the submodule at an unpinned development branch. This keeps the
scientific runtime and its provenance reproducible.
