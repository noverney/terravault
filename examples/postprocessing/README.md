# Postprocessing examples

`force_switzerland.py` is the end-to-end Switzerland bridge:

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
manifest.

See [`docs/FORCE_POSTPROCESSING.md`](../../docs/FORCE_POSTPROCESSING.md) for
the compatibility boundary, Docker and submodule setup, restart semantics,
cron use and output structure.
