# Postprocessing examples

This directory contains two deliberately separate FORCE entry points.

## L2A external-feature bridge

`force_switzerland.py` is the end-to-end Switzerland L2A bridge:

1. select the latest complete B04/B08/SCL/CLD item per MGRS tile from the
   local DuckDB catalogue;
2. stream one aligned 10 m Swiss COG without loading the country into Python;
3. import that COG as a multiband external feature into a tiled FORCE
   datacube;
4. build a FORCE VRT mosaic and record durable job metadata.

Plan the national extraction first:

```bash
python examples/postprocessing/force_switzerland.py --dry-run
```

Run it:

```bash
python examples/postprocessing/force_switzerland.py
```

Or import an existing TerraVault COG without extracting it again:

```bash
python examples/postprocessing/force_switzerland.py \
  --input satellite_data/switzerland_ndvi/exports/switzerland_inputs.tif
```

Visualize the resulting FORCE mosaic:

```bash
python examples/postprocessing/visualize_force.py \
  --input satellite_data/switzerland_ndvi/force/datacube/mosaic/FEATURE.vrt
```

The visualization step computes `(B08 - B04) / (B08 + B04)` as a Float32
Cloud Optimized GeoTIFF, masks invalid/cloudy pixels using SCL and CLD, and
writes a colorized PNG plus world file and provenance manifest. It crops away
FORCE tile padding when the original TerraVault input is recorded in the job
manifest. A labelled `*_before_after.png` uses the same palette to compare
raw NDVI with the SCL/CLD-masked output and reports how many valid-pixel
percentage points the mask removed.

This result is a FORCE-tiled external feature, not FORCE BOA/QAI.

## Native L1C → FORCE BOA/QAI

`force_level2.py` is the thin example wrapper for `terravault force-level2`.
It accepts one complete Sentinel-2 L1C SAFE directory or SAFE ZIP:

```bash
python examples/postprocessing/force_level2.py \
  --input /data/l1c/S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T142404.SAFE.zip \
  --output-root /data/force-native \
  --runtime docker \
  --dem /data/reference/switzerland_dem.tif
```

The input cannot be a selected L2A raster. FORCE requires the complete SAFE
hierarchy, its product/granule metadata, and every L1C band including B10.
TerraVault then runs native L2PS and verifies tiled BOA and bit-packed QAI COGs
plus their mosaic VRTs and per-tile OVV JPEG quicklooks. Verified outputs are
published below `OUTPUT_ROOT/level2/products/SAFE_STEM/`; another SAFE never
shares that published directory. Processing occurs in a per-SAFE attempt
directory; a retry discards only that incomplete attempt, and publication
happens only after the cube definition, CRS/grid, BOA/QAI/OVV chips, and both
VRT source sets verify.

Alternatively, save one Sentinel-2 L1C STAC Item as JSON and let the command
download the complete product first:

```bash
python examples/postprocessing/force_level2.py \
  --stac-item /data/stac/S2A_MSIL1C_item.json \
  --output-root /data/force-native \
  --runtime docker \
  --dem /data/reference/switzerland_dem.tif
```

When both CDSE S3 keys are configured, the item's `safe_manifest` prefix is
preferred and every SAFE file is downloaded into a staging tree. The exact
file list, sizes, per-object SHA-256 values, and SAFE hierarchy are verified
before the previous publication is atomically replaced. Otherwise a current
CDSE bearer token or account credentials can Range-resume the authenticated
`Product` ZIP. Sentinel Hub OAuth client credentials do not authenticate
either route.
Ctrl-C and SIGTERM stop the active process group, record `interrupted`, and
retain partial download/job state for the next identical invocation.

The default Swiss grid is EPSG:2056 at 10 m with 30 km tiles and an origin at
5.5° E / 48.0° N. Supply a DEM covering the scene whenever possible. Without
it, FORCE runs with topographic correction disabled and reduced atmospheric
and cloud-shadow quality. `_terravault/force-l2/cube.json` makes the grid
immutable for the output root; a grid change requires a new root.

One invocation processes one product. SAFE products may be run sequentially
under the same output root, but their publications stay isolated—even for the
same date and sensor. This command does not generate a pooled acquisition or
Switzerland-wide FORCE mosaic. It is also not connected to `watch` or
`historic`; a native Swiss rolling scheduler remains future work.

The same local-SAFE operation is available as a Python API:

```python
from pathlib import Path

from terravault import ForceLevel2Config, ForceLevel2Processor

result = ForceLevel2Processor(
    ForceLevel2Config(
        input_path=Path(
            "/data/l1c/"
            "S2B_MSIL1C_20260717T103029_N0512_R108_T32TMT_20260717T142404.SAFE.zip"
        ),
        output_root=Path("/data/force-native"),
        runtime="docker",
        dem_path=Path("/data/reference/switzerland_dem.tif"),
    )
).run()

print(
    result.status,
    result.boa_mosaic_path,
    result.qai_mosaic_path,
    result.overview_paths,
)
```

## Raw/CDSE/FORCE comparison

Compare native FORCE QAI with the existing L2A SCL/CLD policy:

```bash
python examples/postprocessing/visualize_force.py \
  --input FORCE_EXTERNAL_ROOT/datacube/mosaic/FEATURE.vrt \
  --force-qai FORCE_NATIVE_ROOT/level2/products/SAFE_STEM/mosaic/YYYYMMDD_LEVEL2_SEN2A_QAI.vrt
```

The QAI path is per SAFE. There is no root-level native QAI mosaic across all
SAFE publications yet, so choose a QAI whose date, sensor, and footprint match
the L2A input being compared.

The resulting `*_raw_cdse_force.png` shows raw L2A NDVI, the same pixels with
CDSE SCL+CLD masking, and the same pixels with FORCE QAI masking. It does not
use FORCE BOA for the third panel, so the comparison isolates masking. The
default `--force-qai-mask 0x031F` screens nodata, all cloud states, shadow,
snow, subzero, and saturation. Without `--force-qai`, the existing two-panel
`*_before_after.png` output remains unchanged.

The current Zurich example validates the external-feature route only. A live
native Zurich L2PS run is not claimed because the 2026-08-04 preflight returned
`InvalidAccessKeyId` for the configured S3 pair and no Product bearer/account
fallback was configured before the L1C SAFE download.

See [`docs/FORCE_POSTPROCESSING.md`](../../docs/FORCE_POSTPROCESSING.md) for
the compatibility boundary, Docker and submodule setup, restart semantics,
cron use and output structure.
