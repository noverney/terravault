# Switzerland monthly vegetation composites: three explicit sources

TerraVault keeps three monthly processing routes independent so that the
comparison does not confuse upstream products. Each route produces a monthly
Swiss reflectance mosaic and applies the same four vegetation-index equations,
but the input pixels and quality masks are different.

TreeAI is **only the formula reference** for NDVI, GNDVI, NDRE and the requested
NIR/SWIR NDWI. TerraVault does not download TreeAI imagery and none of the
three routes consumes output from a TreeAI pipeline.

| Route and command | Pixels entering the monthly step | Monthly operation | Default output grid |
|---|---|---|---:|
| Direct Copernicus L2A — `copernicus-monthly` | Locally retrieved Sentinel-2 L2A surface-reflectance bands with SCL and CLD | Mask each acquisition, take a per-pixel monthly median, merge tiles and clip to Switzerland | 20 m |
| Post-FORCE — `monthly` | Completed FORCE Level-2 BOA mosaics paired with their QAI mosaics | Apply QAI, take a per-pixel monthly median and clip to Switzerland | 10 m FORCE grid |
| swissEO monthly — `swisseo-monthly` | Every swissEO S2-SR same-orbit mosaic published for the calendar month | Standardize and mask every orbit item, then take their per-pixel median and clip to Switzerland | 20 m |

These are three comparisons of the same month, not three names for one
downstream product. In particular, the direct route does not pass through
FORCE, and the swissEO route does not read FORCE outputs.

## Why one scene is not enough for Switzerland

A Sentinel-2 granule or satellite pass covers only part of Switzerland. The
monthly processors therefore do not select one nominal “best day.” They use
every complete, accepted acquisition in the month and retain nodata wherever
no valid input pixel exists.

The same distinction applies to swissEO. swisstopo first combines granules
acquired on one satellite orbital track into a published swissEO item. A
single item is consequently a real merged orbit mosaic, but it can still leave
parts of Switzerland uncovered. `swisseo-monthly` discovers **all** swissEO
items in the calendar month and merges the quality-masked items. This is the
monthly swissEO option used in the three-source comparison; the older
`swisseo-image` command remains useful for inspecting one exact orbit item but
is not an input substitute for this comparison.

No compositing method can create an observation where every acquisition was
cloudy, masked or outside the orbit footprint. Those pixels remain nodata and
are represented in the final valid-pixel coverage statistic.

## Pilot workflow: June, July and August

Start with one completed year, for example 2024. Use the same Swiss ROI and
calendar months for all three routes. An exact national Polygon/MultiPolygon
cutline is preferred to the default bounding box for production.

### ETH network-drive layout used for the pilot

The mounted share `smb://ites-formdata.ethz.ch/upscale/working_package_4` is
available on macOS as `/Volumes/upscale/working_package_4`. The active pilot
uses this layout so native inputs, temporary national rasters and final COGs do
not consume the laptop's system disk:

```text
/Volumes/upscale/working_package_4/terravault/
├── data/
│   ├── roi/switzerland-boundary.geojson
│   ├── copernicus-l2a/
│   ├── copernicus-monthly/
│   ├── force-native/
│   ├── force-monthly/
│   └── swisseo-monthly/
├── reports/switzerland-monthly-2024/
├── cache/
├── tmp/
└── logs/
```

Pass these absolute paths as `--storage-root`, `--dataset-db`, `--output-root`
and `--log-file`. Do not use the `smb://` URI directly in GDAL or TerraVault
arguments; use its mounted `/Volumes/upscale/...` path. Keep the share mounted
for the entire run. The report retains paths to the large scientific COGs and
copies only compact PNG previews into its source folders.

### 1. Direct Copernicus Sentinel-2 L2A

First retrieve and catalogue the `vegetation` L2A asset profile. Every item
used by the monthly route must contain B03, B04, B05, B08, B8A and B11 plus SCL
and CLD; the 20 m bands are aligned to the common output grid without gaining
new physical detail.

Plan June–August without processing pixels:

```bash
terravault copernicus-monthly \
  --year 2024 \
  --months 6 7 8 \
  --dataset-db satellite_data/dataset.duckdb \
  --output-root satellite_data/copernicus-monthly \
  --roi /path/to/switzerland.geojson \
  --dry-run
```

Remove `--dry-run` when the local catalogue contains every required band and
the output volume has been reviewed. The monthly route screens the configured
invalid SCL classes and CLD probability, creates a median for each tile, then
merges and clips the tiles. It uses L2A surface reflectance directly; it does
not invoke FORCE.

### 2. Monthly composite after FORCE

The FORCE route starts only after the native pipeline has produced paired
BOA/QAI mosaics for all relevant acquisitions:

```bash
terravault force-pipeline \
  --bbox 5.96 45.82 10.49 47.81 \
  --start-date 2024-06-01 \
  --end-date 2024-08-31 \
  --output-root satellite_data/force-native
```

Inspect the available pairs, then plan the monthly products:

```bash
terravault monthly \
  --year 2024 \
  --months 6 7 8 \
  --force-db satellite_data/force-native/force_images.duckdb \
  --inventory-only

terravault monthly \
  --year 2024 \
  --months 6 7 8 \
  --force-db satellite_data/force-native/force_images.duckdb \
  --output-root satellite_data/force-monthly \
  --roi /path/to/switzerland.geojson \
  --dry-run
```

Remove `--dry-run` to apply each paired QAI and build the monthly median. FORCE
BOA is signed 16-bit reflectance scaled by 10,000 with nodata `-9999`. The
default invalid QAI mask is `0x031F`, screening nodata, all non-clear cloud
states, cloud shadow, snow, subzero reflectance and saturation. The source
mapping is:

| Standard band | FORCE domain | BOA band |
|---|---|---:|
| B03 | GREEN | 2 |
| B04 | RED | 3 |
| B05 | REDEDGE1 | 4 |
| B08 | BROADNIR | 7 |
| B8A | NIR | 8 |
| B11 | SWIR1 | 9 |

The monthly time reported by this command excludes L1C retrieval and the
earlier FORCE atmospheric-correction run.

### 3. Monthly merge of every swissEO orbit mosaic

The swissEO STAC API is public; this route does not require token creation:

- STAC root: <https://data.geo.admin.ch/api/stac/v1>
- collection: `ch.swisstopo.swisseo_s2-sr_v100`
- product description: <https://www.swisstopo.admin.ch/en/satelliteimage-swisseo-s2-sr>

Plan the three months:

```bash
terravault swisseo-monthly \
  --year 2024 \
  --months 6 7 8 \
  --output-root satellite_data/swisseo-monthly \
  --temporary-root /private/tmp/terravault-swisseo \
  --publication-wait-seconds 21600 \
  --roi /path/to/switzerland.geojson \
  --dry-run
```

The dry-run manifest reports `estimated_peak_temporary_bytes`, a conservative
upper bound for simultaneous standardized orbit rasters and compositing
intermediates. Before a production run starts, TerraVault checks that estimate
against both the configured temporary-storage limit and free space under
`--temporary-root`. It separately checks the final output volume. It blocks
processing if either check fails; select a coarser resolution or a larger
processing volume instead of beginning a run that is expected to exhaust disk.

Remove `--dry-run` to stream the public COGs. Asset keys are dynamic, so
TerraVault locates them by stable titles (`BANDS-10M`, `BANDS-20M`,
`MASKS-10M`, `METADATA`) and reads every item's metadata JSON for its actual
band order. The applied contract is reflectance scale `0.001`, reflectance
nodata `9999`, cloud/cloud-shadow mask band 2 invalid value `1`, terrain-shadow
mask band 1 direct-shadow value `100`, and mask nodata `255`.

GeoTIFF creation requires random seeks. SMB servers can reject those seeks
even when ordinary file copies work, producing `_tiffSeekProc: Bad file
descriptor`. `swisseo-monthly` therefore builds its VRTs and TIFFs under
`--temporary-root` (the system temporary directory by default), then copies
each completed TIFF sequentially to `--output-root` and atomically renames it.
For a network-drive output, choose a local staging volume with enough free
space for the dry-run's `estimated_peak_temporary_bytes`; never point the work
files themselves at the SMB mount. If the mounted output share disappears
during processing or publication, TerraVault retains the completed local TIFF
and waits for the same output path to reconnect before retrying the copy and
rename. The default wait is six hours; adjust it with
`--publication-wait-seconds`, or set it to `0` to fail immediately. A
disconnect can still interrupt other workflows that read their inputs directly
from the share, so keeping the share mounted remains preferable.

B05, B8A and B11 are native 20 m observations. Requesting a 10 m output only
resamples those bands; it does not add detail. Derived products retain the
required attribution:

```text
© swisstopo, Contains modified Copernicus Sentinel data [Year]
```

## Small patches

All three routes accept `--bbox` (or `--roi`) and do work proportional to it,
not to the source granules or national mosaics:

- **Post-FORCE (`monthly`)** cuts each acquisition's BOA/QAI mosaic to the bbox
  on the native FORCE grid before screening, so values are unchanged, and
  screens the acquisitions in parallel (`TERRAVAULT_FORCE_MONTHLY_SCENE_WORKERS`,
  default 6). Acquisitions that miss the bbox are skipped and listed under
  `scenes_outside_bbox` in the manifest.
- **Copernicus (`copernicus-monthly`)** warps each scene only over the part of
  its tile that the bbox needs, snapped to the output grid.
- **swissEO (`swisseo-monthly`)** keeps only the orbit mosaics whose footprint
  meets the bbox, and when the bbox needs at most a quarter of an asset it
  reads that window from the public COG with HTTP range requests instead of
  downloading the national file (up to ~690 MB per band, ~3 GB per v200
  orbit). The manifest's `asset_fetch_modes` records which path ran.

Measured on the 23 × 20 km Lucerne patch (July 2024, 10 m, 12 acquisitions,
Apple M2 Max), with FORCE products clipped to the patch plus 5 km:

| Post-FORCE monthly | Before | After |
|---|---:|---:|
| QAI screening | 410 s | 26 s |
| Monthly median | 286 s | 47 s |
| Total | 706 s | 82 s |
| Peak temporary storage | 2.7 GiB | 1.4 GiB |

Before, every acquisition was screened serially over all four 30 km FORCE
tiles it covered and the median ran over their union; after, both run on the
2302 × 2028 patch window.

The reflectance and index COGs from both versions are identical except for
float rounding below 1e-7 in a handful of pixels.

On the same patch, `copernicus-monthly` took 96 s for 12 scenes, and
`swisseo-monthly` took 159 s for the 12 v200 orbits over the patch and 65 s
for the 9 v100 orbits, reading only windows (no whole-file downloads, under
1 GiB of temporary storage). Before the bbox filter and windowed reads the same
commands would have fetched every orbit of the month in full: 22 v200 orbits
at ~2.9 GB and 16 v100 orbits at ~3.3 GB each, roughly 63 GB and 53 GB (from
the 29 July 2024 asset sizes).

swissEO keeps its checkpoints under `--temporary-root/checkpoints/<stem>`, and
the stem names the month, version and resolution but not the bbox. Give a
patch run its own `--temporary-root` so it can never resume from a national
run's checkpoints, or the reverse.

## Four shared equations

After each route has standardized its input to physical reflectance in B03,
B04, B05, B08, B8A and B11, it applies exactly these source-neutral equations:

```text
NDVI  = (B08 - B04) / (B08 + B04)
GNDVI = (B08 - B03) / (B08 + B03)
NDRE  = (B8A - B05) / (B8A + B05)
NDWI  = (B08 - B11) / (B08 + B11)
```

Zero denominators and invalid source pixels become nodata. The reference for
the formulas, and only the formulas, is
<https://www.treeai.earth/swiss-vegetation-condition>.

## Products and the comparison atlas

Each source/month run writes a high-resolution six-band physical-reflectance
COG, a four-band NDVI/GNDVI/NDRE/NDWI COG, and a manifest. The FORCE route also
writes its derived STAC item. These COGs are the scientific-resolution
products; the PNGs and atlas are display products.

Once the three routes have produced all nine source/month combinations, run:

```bash
terravault compare-monthly \
  --year 2024 \
  --months 6 7 8 \
  --roi /path/to/switzerland.geojson \
  --copernicus-root satellite_data/copernicus-monthly \
  --force-root satellite_data/force-monthly \
  --swisseo-root satellite_data/swisseo-monthly \
  --output-root artifacts/monthly-comparison
```

The comparison exports:

- nine false-colour mosaic previews, one for each source/month;
- 36 index PNG maps, one for every source/month/equation combination;
- 36 matching single-band index COGs at the originating scientific raster's
  native stored grid and resolution;
- a source-separated report tree with an overview sheet and month folders for
  Sentinel/Copernicus, FORCE and swissEO;
- a lower-resolution 9-by-12 logical atlas, rendered as a landscape canvas;
- machine-readable comparison statistics in JSON and CSV;
- `report-manifest.json`, which points back to each scientific raster and
  monthly timing manifest.

The report folder has this shape:

```text
monthly-report/
├── comparison-atlas-9x12.png
├── comparison-statistics.json
├── comparison-statistics.csv
├── report-manifest.json
└── sources/
    ├── sentinel-copernicus/
    │   ├── overview.png
    │   └── year=YYYY/month=MM/
    │       ├── false-colour.png
    │       ├── ndvi.{png,tif}
    │       ├── gndvi.{png,tif}
    │       ├── ndre.{png,tif}
    │       └── ndwi.{png,tif}
    ├── force/
    │   └── ...
    └── swisseo/
        └── ...
```

The false-colour previews and index maps are clipped to the same supplied
Swiss cutline. Outside-ROI pixels remain transparent in individual PNGs and
are composited onto white in the overview sheets. A rectangular, spatially
uniform image indicates a test fixture, missing cutline, or invalid source
product rather than an acceptable national result.

The TIFF beside each index PNG is a losslessly compressed single-band COG
selected directly from that source/month's four-band index COG. Its base
pixels, raster dimensions, CRS, nodata mask and stored pixel size are retained;
the lower-resolution comparison grid is never used for this export. The report
manifest records the PNG and TIFF paths, source band number/name, native pixel
size, raster dimensions and source multi-band COG for every index. The
reflectance COG is referenced by path and is not duplicated in the report.
When the report root is on SMB, GDAL writes each COG to the local system
temporary directory first. TerraVault then publishes it with one sequential,
buffered copy and an atomic rename, so GDAL never performs random-seek TIFF
writes on the network mount.

`compare-monthly` accepts one to three months. The default atlas has nine data
rows (June, July and August × Copernicus, FORCE and swissEO); a single-month
pilot has three rows. Every atlas has four index columns (NDVI, GNDVI, NDRE and
NDWI). Within each cell,
the index map is accompanied by its value-distribution curve and final-grid
valid-pixel coverage. Each row is annotated with monthly mosaic time, combined
time for all four equations and total command time. Thus “9 × 12” describes
the logical page/canvas; it is not a claim that there are 108 independent
source products.

For cross-source statistics the products are aligned to a common 20 m analysis
grid by default. This avoids presenting a resampled 10 m value as extra native
detail. The original multi-band COGs and report's single-band index COGs remain
at each route's configured output resolution.

## Coverage and cloud metadata are different measurements

The comparison deliberately keeps these quantities separate:

| Quantity | Meaning | Can it be compared directly across routes? |
|---|---|---|
| Final valid-pixel coverage | Valid index pixels divided by every pixel in the common Swiss analysis grid | Yes, when ROI, grid, nodata and index are the same |
| Copernicus/FORCE catalogue cloud cover | Scene-level cloud property inherited from source catalogue items and averaged over selected scenes | No; it is selection metadata, not surviving national pixels |
| swissEO `percent_masked` | swissEO's published per-orbit source metric, averaged over the selected monthly items | No; its denominator and mask definition differ from catalogue cloud and final coverage |

Missing final pixels can reflect cloud, cloud shadow, snow, source nodata,
terrain masking or an orbit that never observed that location. Therefore
`100 - valid coverage` must not be relabelled “average cloud cover.” The atlas
shows the source-reported metric under its own label when available and always
calculates final valid-pixel coverage independently from the output raster.

## Timing fields and benchmark boundary

Every production manifest contains `timings_seconds` with three comparison
fields:

| Field | Meaning |
|---|---|
| `monthly_mosaic_generation` | Route-specific masking, alignment, monthly compositing, national clipping and reflectance COG creation |
| `index_calculation` | One combined wall-clock measurement for calculating and writing all four indices |
| `total` | Entire monthly command, including source selection or STAC/metadata discovery as applicable |

The raw times do **not** have identical input scopes:

- direct Copernicus starts from L2A assets already downloaded and catalogued;
- post-FORCE starts from BOA/QAI products already generated by FORCE;
- swissEO reads remote public COGs during monthly mosaic generation, so
  network transfer and remote alignment can be included.

The direct time therefore excludes L2A retrieval, and the FORCE time excludes
both source retrieval and FORCE Level-2 processing. Compare the three monthly
algorithms only after reporting those boundaries, hardware, storage, network
conditions, input counts and output resolution. The atlas displays the three
times, but it cannot make their upstream scopes equivalent.

A dry run records inventory and planning information. Its production stages
are zero because no mosaic or vegetation index was created; its small `total`
is planning overhead and must not be reported as a production benchmark.

## Scaling from the pilot to 2017–2026

After accepting the June–August pilot's coverage, distributions, resource use
and timings, repeat the three commands once per year for 2017 through 2026 and
run `compare-monthly` for each complete year. Keep the same ROI, resolution,
quality policy and equations if the intention is a longitudinal comparison.

Do not silently treat an unfinished month as final. By default, the monthly
commands reject an incomplete calendar month; an explicitly allowed partial
month must be labelled provisional. Sensor availability, catalogue history
and swissEO publication coverage can also differ by year, so retain item counts
and source metadata with every result.

The repository provides the workflow and comparison outputs, but it does not
claim that real, national, three-source June–August mosaics have already been
produced. Only a completed non-dry run with the full source inventory can
supply those rasters and measured benchmark times.
