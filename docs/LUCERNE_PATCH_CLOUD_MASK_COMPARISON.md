# Lucerne patch: Copernicus, swissEO and FORCE cloud screening, July 2024

Date: 2026-10-15. First complete run of all three monthly routes on the same
acquisitions, at 10 m, on a 23 × 20 km patch. The national July 2024 comparison
([report](JULY_2024_SWITZERLAND_VEGETATION_REPORT.md)) had to leave FORCE out;
this patch is small enough to include it.

## Study definition

| Property | Value |
|---|---|
| Patch | 8.17–8.47° E, 46.975–47.155° N (Lake Lucerne, Pilatus, Rigi, lowland farmland and forest) |
| Grid | EPSG:2056, 10 m, 2302 × 2028 pixels |
| Month | July 2024 |
| Acquisitions | 12 (6 × S2A, 6 × S2B; relative orbits R065 and R108), all from MGRS tile T32TMT, each covering 100% of the patch |
| Catalogue cloud cover | 4.7–95.0% (L1C); only 2 of 12 under 20%, so no scene-level filter was applied |

The patch lies in the part of T32TMT that no neighbouring tile overlaps, so each
acquisition is exactly one L1C SAFE, one L2A product, and one pixel in each
swissEO orbit mosaic. swissEO v200 publishes all 12 acquisitions; v100 publishes
9 (it omits 7, 12 and 22 July, three of the four cloudiest).

| Route | Input | Quality layer used | Command |
|---|---|---|---|
| Copernicus | CDSE L2A (Sen2Cor) B03/B04/B05/B08/B8A/B11 | SCL ∈ {0,1,3,8,9,10,11} or CLD > 30% | `copernicus-monthly` |
| swissEO v200 | swisstopo S2-SR v200 orbit mosaics | OmniCloudMask ≠ 0, or solar incidence ≥ 90° (incl. cast shadow and no-data, which covers open water) | `swisseo-monthly --product-version v200` |
| swissEO v100 | swisstopo S2-SR v100 orbit mosaics | cloud/shadow mask = 1, terrain shadow = 100, mask no-data | `swisseo-monthly` |
| FORCE | L1C → FORCE 3.10.04 L2PS with GLO-30 DEM | QAI & `0x031F` (cloud states incl. 300 m buffer, shadow, snow, no-data, subzero, saturation) | `force-pipeline`, `monthly` |

All four composites are per-pixel medians of the valid observations, written on
the identical grid.

## Main findings

1. **The three processors agree on most cloud, and disagree in two specific
   situations.** Pooled over all dates, Copernicus, swissEO v200 and FORCE agree
   on 86–89% of pixel-observations (Cohen's κ 0.71–0.78, IoU 0.78–0.84).
   - *Thin, semi-transparent cloud* that is barely visible in true colour
     (9 and 14 July): Copernicus and FORCE flag 28–66% of the patch; both
     swissEO versions flag 2–13%. On 19 and 27 July Copernicus alone also flags
     thin streaks (7–9% against 2–3% for the others).
   - *Broken cumulus with shadows* (4, 24 and 29 July): swissEO and FORCE flag
     far more than Copernicus. On 29 July: Copernicus 18%, swissEO v200 35%,
     v100 43%, FORCE 34% plus a further 35% of 300 m buffer.

   No reference mask was available, so this shows where the processors
   disagree, not which one is right.
2. **FORCE is the most conservative.** Over the 9 dates every source has, the
   share flagged as cloud or cloud shadow is swissEO v200 39.8%, Copernicus
   43.2%, swissEO v100 43.9%, FORCE 52.1% (57.7% invalid including its buffer).
   Almost everything Copernicus flags, FORCE flags too (1.2% of
   pixel-observations are Copernicus-only); FORCE flags another 11.5% that
   Copernicus calls clear, 8.6% without the buffer. FORCE leaves 3.8 clear
   looks per pixel this month against 5.2 for Copernicus.
3. **swissEO v200 and Copernicus give nearly the same composite on land.** NDVI
   bias +0.001, RMSD 0.037, r = 0.983. The haze swissEO accepts does not show
   up in the median here.
4. **FORCE is uniformly higher**: NDVI +0.025 (RMSD 0.045, r = 0.984) against
   Copernicus on land and +0.024 against swissEO v200; NDWI +0.041. The offset is
   spatially even, which points to the radiometric processing (FORCE's own
   atmospheric, topographic and nadir-BRDF corrections) rather than masking.
   Over the lakes and shorelines differences are larger, as expected where NDVI
   is ill-conditioned and adjacency correction differs.
5. **swissEO v100 is offset by about 12 m.** Its composite sits 1.2 pixels east
   of the L2A grid (FORCE and v200 align with L2A within 0.1 px). Shifting it
   back cuts NDVI RMSD against Copernicus from 0.090 to 0.049 on a lowland block.
   This, not cloud masking, drives v100's larger differences (NDVI RMSD 0.077,
   r = 0.926 on land). Which grid is closer to the ground was not tested.
6. **swissEO v200 drops all open water** by design (its terrain mask has no value
   over lakes and rivers): 16% of this patch has no valid observation. Its
   headline mean NDVI (0.697 against 0.579 for Copernicus) reflects the
   missing lakes, not a vegetation difference.

## Per-acquisition cloud and shadow flagged (% of patch)

| Date | Copernicus | swissEO v200 | swissEO v100 | FORCE | FORCE buffer |
|---|---:|---:|---:|---:|---:|
| 2024-07-02 | 75.6 | 81.6 | 90.8 | 100.0 | 20.2 |
| 2024-07-04 | 38.8 | 53.0 | 62.5 | 56.5 | 28.1 |
| 2024-07-07 | 100.0 | 98.8 | – | 100.0 | 5.1 |
| 2024-07-09 | 61.9 | 12.9 | 8.9 | 66.3 | 1.2 |
| 2024-07-12 | 99.6 | 100.0 | – | 95.3 | 28.3 |
| 2024-07-14 | 28.1 | 3.3 | 2.2 | 29.6 | 2.0 |
| 2024-07-17 | 85.3 | 96.7 | 99.6 | 98.9 | 12.5 |
| 2024-07-19 | 6.9 | 2.5 | 1.8 | 2.8 | 1.2 |
| 2024-07-22 | 96.2 | 99.8 | – | 100.0 | 3.6 |
| 2024-07-24 | 64.7 | 70.5 | 85.1 | 77.9 | 19.7 |
| 2024-07-27 | 9.3 | 2.2 | 0.4 | 2.9 | 0.7 |
| 2024-07-29 | 18.5 | 35.5 | 43.5 | 33.6 | 34.7 |

"FORCE buffer" is QAI cloud state 1, FORCE's 300 m cloud buffer, which can
overlap with flagged shadow; route totals in `summary.json` count each pixel
once.

## Pooled mask agreement (cloud or shadow, all shared dates)

| Pair | Dates | Agreement | IoU | κ | First only | Second only |
|---|---:|---:|---:|---:|---:|---:|
| Copernicus vs FORCE (with buffer) | 12 | 87.3% | 0.81 | 0.73 | 1.2% | 11.5% |
| Copernicus vs FORCE (no buffer) | 12 | 89.4% | 0.84 | 0.78 | 2.0% | 8.6% |
| Copernicus vs swissEO v200 | 12 | 86.1% | 0.78 | 0.72 | 8.1% | 5.8% |
| Copernicus vs swissEO v100 | 9 | 77.5% | 0.59 | 0.54 | 10.9% | 11.6% |
| swissEO v200 vs FORCE (with buffer) | 12 | 86.0% | 0.79 | 0.71 | 0.7% | 13.3% |
| swissEO v100 vs FORCE (with buffer) | 9 | 83.9% | 0.72 | 0.68 | 1.8% | 14.3% |

Per-date κ is not averaged here: on a nearly overcast scene two masks that both
say "cloud" have κ near zero, so pooled counts are the fairer summary.

## Composite agreement on land (NDVI, pixels valid in all sources and in swissEO v200)

| Pair | Mean difference | RMSD | r | \|Δ\| > 0.1 |
|---|---:|---:|---:|---:|
| swissEO v200 − Copernicus | +0.001 | 0.037 | 0.983 | 2.7% |
| FORCE − Copernicus | +0.025 | 0.045 | 0.984 | 3.8% |
| FORCE − swissEO v200 | +0.024 | 0.044 | 0.984 | 3.4% |
| swissEO v100 − Copernicus | +0.001 | 0.077 | 0.926 | 14.0% |

## Cost on this patch

Apple M2 Max (12 cores), Docker Desktop 23.4 GiB, home network.

| Step | Time | Data |
|---|---:|---:|
| L1C download, 12 SAFEs (CDSE S3) | 6 min 15 s | 8.6 GB |
| L2A vegetation assets, 12 scenes | 3 min 42 s | 5.2 GB |
| FORCE L2PS, per scene, 1 × 12 threads, AOI | 164–170 s | ~160 MB |
| FORCE L2PS, 12 scenes, 2 workers × 6 threads | 28 min 26 s for 11 + 172 s retry | 1.9 GB |
| Post-FORCE monthly | 82 s (was 706 s) | |
| Copernicus monthly | 96 s | |
| swissEO v200 monthly (12 windowed orbits) | 159 s | under 1 GiB temporary |
| swissEO v100 monthly (9 windowed orbits) | 65 s | under 1 GiB temporary |

FORCE remains the expensive route: about 34 min of L2PS for 12 scenes on this
machine, against minutes for the others, because FORCE always corrects whole
granules. The settings behind these numbers, and why `--scene-workers 2` is not
worth it here, are in [FORCE_PIPELINE.md](FORCE_PIPELINE.md#small-patches).

## Caveats

- One month, one patch, one tile. July 2024 was unusually cloudy, which is what
  made it a good mask test, but the clear-look counts do not transfer to other
  months.
- No independent cloud reference. "Disagreement" is not "error" for either side.
- FORCE was run with the GLO-30 DSM, not a swisstopo DTM. The earlier
  single-scene FORCE test in this repository (August 2026) ran without a DEM.
- swissEO v100's three missing acquisitions are the cloudiest ones, so its
  12-date averages are not comparable; use the 9 shared dates.
- Copernicus' CLD > 30% threshold and FORCE's 300 m buffer are TerraVault
  defaults, not tuned for agreement.

## Reproduce

Data and figures live outside the repository in `~/terravault-patch/lucerne/`
(about 20 GB): `roi/`, `reference/` (DEM), `force-native/`, `copernicus-l2a/`,
`out/` (the four monthly products), and `compare/`:

- `compare/cloud-masks/per_date_masks.png` — every acquisition, all masks
- `compare/cloud-masks/composite_ndvi.png`, `composite_ndvi_differences.png`
- `compare/cloud-masks/summary.json`, `per_date.csv`
- `compare/monthly-v200/`, `compare/monthly-v100/` — `compare-monthly` atlas and statistics

```bash
W=~/terravault-patch/lucerne
BBOX="8.17 46.975 8.47 47.155"
terravault force-pipeline --bbox $BBOX --start-date 2024-07-01 --end-date 2024-07-31 \
  --max-cloud-cover 100 --output-root $W/force-native --runtime docker \
  --dem $W/reference/dem_t32tmt_glo30_lv95.tif \
  --force-aoi $W/roi/lucerne-patch-aoi-5km.geojson --scene-workers 1 --threads 12 \
  --mount-root $W
terravault monthly --year 2024 --months 7 --bbox $BBOX --resolution 10 \
  --force-db $W/force-native/force_images.duckdb --output-root $W/out/force-monthly
terravault historic --bbox $BBOX --start-date 2024-07-01 --end-date 2024-07-31 \
  --asset-profile vegetation --storage-root $W/copernicus-l2a
terravault copernicus-monthly --year 2024 --months 7 --bbox $BBOX --resolution 10 \
  --max-cloud-cover 100 --dataset-db $W/copernicus-l2a/dataset.duckdb \
  --output-root $W/out/copernicus-monthly
terravault swisseo-monthly --year 2024 --months 7 --bbox $BBOX --resolution 10 \
  --product-version v200 --output-root $W/out/swisseo-v200-monthly \
  --temporary-root $W/scratch/swisseo-v200
python scripts/compare_patch_cloud_masks.py --bbox $BBOX --year 2024 --month 7 \
  --force-root $W/force-native --copernicus-db $W/copernicus-l2a/dataset.duckdb \
  --output-dir $W/compare/cloud-masks \
  --indices copernicus $W/out/copernicus-monthly/year=2024/month=07/switzerland_copernicus_l2a_2024_07_10m_indices.tif \
  --indices force $W/out/force-monthly/year=2024/month=07/switzerland_force_boa_2024_07_10m_indices.tif
```

The DEM is the six Copernicus GLO-30 tiles N46–N47, E007–E009 from
`s3://copernicus-dem-30m`, warped to LV95 at 30 m, Int16, nodata −32767.
