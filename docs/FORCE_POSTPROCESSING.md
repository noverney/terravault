# FORCE postprocessing

TerraVault integrates [FORCE](https://github.com/davidfrantz/force) as a
pinned Git submodule and as an optional postprocessing runtime. The default
runtime is the official `davidfrantz/force:3.10.04` Docker image.

## Compatibility boundary

The local TerraVault archive contains selected, already processed
Sentinel-2 L2A JP2 assets. It is **not** a complete Level-1 product package,
so it must not be passed to `force-level2` or labelled as FORCE BOA/QAI ARD.
FORCE Level-2 begins with complete Level-1 products and performs its own
radiometric/atmospheric processing.

The supported bridge implemented here is:

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
matching source and documentation. Runtime commands default to the matching
official Linux image so all environments execute the same release.

On a case-insensitive macOS filesystem, upstream sensor definitions whose
names differ only by case can make the submodule appear internally modified
after checkout. `.gitmodules` ignores that platform-only dirty indication;
the pinned gitlink remains unchanged. Do not commit replacements inside
`vendor/force`.

## Install and verify the runtime

Docker is the recommended route:

```bash
docker pull --platform linux/amd64 davidfrantz/force:3.10.04
docker run --rm --platform linux/amd64 davidfrantz/force:3.10.04 force-info
```

FORCE is developed and tested on Ubuntu; upstream does not support migrating
it to other operating systems. `--runtime auto` therefore considers a native
FORCE installation only on Linux. On macOS it always selects Docker, even if
commands named `force-*` happen to be on `PATH`. An explicit `--runtime
native` remains available for developers deliberately testing a port.

The official v3.10.04 image used here is `linux/amd64`. On this Apple Silicon
Mac, Docker Desktop runs it through Linux/AMD64 emulation; FORCE itself never
links against macOS ARM64 libraries. The real Zurich integration test
successfully ran this exact combination. It will generally be slower than
native AMD64 Linux for a national job.

Docker commands pin `--platform linux/amd64`, mount only the smallest common
parent containing the input and output, map the host user to avoid root-owned
products, and use a writable temporary home for FORCE's GNU Parallel workers.
TerraVault also pins `SHELL=/bin/bash`; otherwise an unlisted host UID can
make GNU Parallel fall back to `/bin/sh`, which cannot execute FORCE's
exported Bash tile worker. Override `--docker-platform` only when using a
tested custom/multi-architecture image.

FORCE can also be compiled natively on a supported Linux system by following
its installation documentation. TerraVault checks for `force-info`,
`force-cube-init`, `force-cube`, `force-mosaic`, GDAL and a valid input before
starting.

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

## End-to-end Switzerland snapshot

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

## Visualize a FORCE mosaic

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
FEATURE_ndvi.visualization.json
_terravault/logs/force-visualize.log
```

The GeoTIFF is a georeferenced Float32 COG with `-9999` nodata. The PNG is a
compact RGBA color relief, and its world file retains map placement. By
default, the calculation:

- uses band descriptions to locate B04, B08, SCL and CLD;
- masks SCL 0, 1, 3, 8, 9, 10 and 11;
- masks CLD values above 50%;
- excludes nodata and zero denominators;
- crops FORCE tile padding to the original TerraVault input bounds;
- records source files, palette, statistics and exact GDAL commands.

Use `--cloud-threshold`, explicit band-number options,
`--no-quality-mask`, or `--no-crop-to-force-input` to change those choices.
Unchanged inputs are skipped using the visualization manifest; use
`--overwrite` after intentionally changing parameters.

## Rolling operation and cron

The acquisition worker remains responsible for polling CDSE. The
postprocessing wrapper reads only local data and needs no Copernicus
credential:

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
- pinned FORCE image/version;
- chip paths, mosaic path and error text.

After interruption, rerun the same command. A completed matching job with
present chips is skipped. A failed job increments its attempt counter and
retries. FORCE's shell utilities can return success even if a child tile
worker fails, so TerraVault additionally verifies that chips and the mosaic
VRT actually exist. A completed basename cannot silently change ownership:
choose a new `--basename`, or intentionally pass `--overwrite`.

Logs rotate at 25 MiB with ten generations and contain input, cube, command,
progress and output paths but no Copernicus secrets.

## What to do next

FORCE Higher Level Processing parameter files should reference the imported
external-feature datacube only for operations that accept that feature
layout. If the goal is a true FORCE BOA/QAI time-series archive, change the
acquisition workflow to retain complete Sentinel-2 Level-1 products and run
`force-level2`; the selected TerraVault L2A band archive cannot substitute
for those inputs.

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
