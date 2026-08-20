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
  --processes 1 \
  --threads 2
```

Use `--discover-only` to persist selection/metadata without pixels. Use
`--download-only` to build and validate the complete Level-1 pool without
starting L2PS. Both modes still create the catalogue and FORCE queue.

CDSE S3 credentials use `TERRAVAULT_CDSE_S3_ACCESS_KEY` and
`TERRAVAULT_CDSE_S3_SECRET_KEY`. When both are unavailable, the downloader can
use a CDSE bearer token or account credentials from the existing
`TERRAVAULT_CDSE_*` environment variables for the Product ZIP route.

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
    processes=1,
    threads=2,
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
        processes=1,
        threads=2,
    ),
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
failure is `FAIL`. TerraVault's high-level wrapper processes scenes
sequentially through the existing isolated per-SAFE processor; the aggregate
queue also remains usable by external FORCE tooling. The format follows the
official [FORCE file queue](https://force-eo.readthedocs.io/en/latest/components/lower-level/level1/queue.html)
contract.

## Output boundary

Each SAFE remains isolated under `level2/products/SAFE_STEM/`. The wrapper does
not combine products from the same acquisition into a country mosaic. It also
does not turn the native FORCE path into a permanent polling daemon; rerun the
same bounded command to reconcile a time range, or schedule it externally.
