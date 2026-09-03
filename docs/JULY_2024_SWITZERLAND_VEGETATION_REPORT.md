---
title: "Switzerland vegetation-condition comparison"
subtitle: "Copernicus Sentinel-2 L2A and swissEO S2-SR, July 2024"
date: "2026-09-03"
---

# Executive summary

This pilot compares two independently produced July 2024 vegetation-condition
products for Switzerland:

1. direct Copernicus Sentinel-2 Level-2A surface reflectance; and
2. published swissEO S2-SR orbit mosaics.

Both final products use the same Swiss boundary, 20 m output grid, physical
reflectance band order and four vegetation-index equations. The final scientific
rasters and report images are stored on the ETH `upscale` SMB share. A third route,
native FORCE BOA/QAI, was evaluated but excluded from this comparison because its
current monthly aggregation implementation required excessive temporary storage and
did not complete.

The two completed sources show broadly consistent national vegetation patterns:
strong positive vegetation signals across the lowlands and forested regions, lower
values in high-alpine areas, and negative responses over water. Their missing-data
patterns differ because their source products, acquisition groupings and quality
masks are not equivalent.

## Primary comparison figure

![Copernicus L2A versus swissEO S2-SR for NDVI, GNDVI, NDRE and NDWI](</Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/comparison-atlas-copernicus-vs-swisseo-2024-07.png>)

Absolute path:

```text
/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/comparison-atlas-copernicus-vs-swisseo-2024-07.png
```

The atlas is a 2600 × 1600 PNG assembled from the validated per-source report
images. All panels use the same −1 to +1 colour scale. Its SHA-256 checksum is:

```text
887540f7ede7d5859ee0c70107480cc13fa4dfac6780c9c416900700db9e6343
```

# Study definition

| Property | Value |
|---|---|
| Area | Switzerland, exact national Polygon/MultiPolygon cutline |
| Analysis month | July 2024 |
| Final CRS | CH1903+ / LV95 (`EPSG:2056`) |
| Pixel size | 20 m |
| Raster size | 17,423 × 11,034 pixels |
| WGS84 bounding box | 5.955902, 45.817958, 10.492171, 47.808454 |
| Reflectance type | Six-band `Float32` physical reflectance |
| Reflectance bands | B03, B04, B05, B08, B8A, B11 |
| Index type | Four-band `Float32` |
| Index bands | NDVI, GNDVI, NDRE, NDWI |
| Nodata | `-9999` |
| Storage format | DEFLATE-compressed Cloud Optimized GeoTIFF |

The exact ROI is stored at:

```text
/Volumes/upscale/working_package_4/terravault/data/roi/switzerland-boundary.geojson
```

# Shared vegetation-index definitions

After each route standardizes its input to physical surface reflectance, TerraVault
uses the same source-neutral equations:

```text
NDVI  = (B08 - B04) / (B08 + B04)
GNDVI = (B08 - B03) / (B08 + B03)
NDRE  = (B8A - B05) / (B8A + B05)
NDWI  = (B08 - B11) / (B08 + B11)
```

Zero denominators and invalid source pixels become nodata. `NDWI` is the project’s
requested NIR/SWIR formulation. TreeAI is used only as the formula reference; no
TreeAI imagery enters either processing route:
<https://www.treeai.earth/swiss-vegetation-condition>.

# Source 1: direct Copernicus Sentinel-2 L2A

The direct route starts from downloaded and catalogued Sentinel-2 L2A assets. It
uses B03, B04, B05, B08, B8A and B11 together with the Scene Classification Layer
and cloud-probability layer. For each accepted acquisition it masks invalid pixels,
forms a per-pixel monthly median, merges the contributing tiles and clips the result
to Switzerland.

## Copernicus inventory and quality policy

| Property | Value |
|---|---:|
| Accepted scenes | 65 |
| Downloaded band/mask assets | 520 JP2 files |
| Maximum scene cloud cover | 20% |
| Mean catalogue cloud cover of accepted scenes | 11.6445% |
| Maximum accepted CLD probability | 30% |
| Invalid SCL classes | 0, 1, 3, 8, 9, 10, 11 |
| Minimum accepted physical reflectance | 0.0 |
| Composite | SCL/CLD-screened per-pixel monthly median |

Catalogue cloud cover is scene metadata. It is not the percentage of pixels masked
in the final national raster.

## Copernicus measured processing time

| Stage | Seconds | Human-readable |
|---|---:|---:|
| Source selection | 42.051 | 0.7 min |
| Monthly mosaic generation | 8,198.196 | 136.6 min |
| Four vegetation indices | 151.244 | 2.5 min |
| Complete monthly command | 9,377.901 | **156.3 min / 2 h 36 min** |

This timing excludes the earlier L2A acquisition/download phase. The total is
larger than the named stage sum because it also includes validation, file assembly,
publication and other command overhead.

## Copernicus index statistics retained in the report summary

| Index | Minimum | Maximum | Mean | Standard deviation |
|---|---:|---:|---:|---:|
| NDVI | -1.000 | 1.000 | 0.616 | 0.309 |
| GNDVI | -1.000 | 0.999 | 0.543 | 0.305 |
| NDRE | -1.000 | 1.000 | 0.446 | 0.231 |
| NDWI | -1.000 | 1.000 | 0.235 | 0.196 |

# Source 2: swissEO S2-SR

The swissEO route uses every published same-orbit S2-SR mosaic available for July
2024. It reads each item’s metadata to resolve the actual band order, applies the
published quality masks, standardizes reflectance, takes the per-pixel monthly
median and clips the result to Switzerland.

The public source is the swisstopo STAC collection
`ch.swisstopo.swisseo_s2-sr_v100` at
<https://data.geo.admin.ch/api/stac/v1>.

## swissEO inventory and mask contract

| Property | Value |
|---|---:|
| Published orbit products | 16 |
| Mean source-reported `percent_masked` | 43.2375% |
| Published reflectance scale | 0.001 |
| Published reflectance nodata | 9999 |
| Cloud/cloud-shadow mask | band 2, invalid value 1 |
| Terrain-shadow mask | band 1, direct-shadow value 100 |
| Mask nodata | 255 |
| Composite | Quality-masked per-pixel median of all orbit products |
| Conservative planned peak temporary storage | 34,233,477,120 bytes / 31.9 GiB |

The source-reported `percent_masked` is defined by swissEO and is not directly
comparable to Copernicus catalogue cloud cover or final valid-pixel coverage.

Required attribution:

```text
© swisstopo, Contains modified Copernicus Sentinel data 2024
```

## swissEO measured processing time

| Stage | Seconds | Human-readable |
|---|---:|---:|
| STAC and metadata | 3.614 | 0.1 min |
| Monthly mosaic generation | 3,931.217 | 65.5 min |
| Four vegetation indices | 343.201 | 5.7 min |
| Complete monthly command | 4,279.027 | **71.3 min / 1 h 11 min** |

The swissEO monthly stage reads public remote COGs, so network transfer and remote
alignment can be included in this timing.

# Timing comparison and interpretation

| Measure | Copernicus L2A | swissEO S2-SR | Observed relationship |
|---|---:|---:|---|
| Input units | 65 scenes | 16 orbit mosaics | Not equivalent units |
| Monthly mosaic | 136.6 min | 65.5 min | Copernicus took about 2.1× as long |
| Four indices | 2.5 min | 5.7 min | Copernicus index stage was about 2.3× faster |
| Total monthly command | 156.3 min | 71.3 min | Copernicus took about 2.2× as long |

These are useful operational benchmarks, not a controlled algorithm-speed test.
The routes begin from different upstream products: Copernicus starts from locally
downloaded L2A assets, while swissEO starts from remote, already-assembled orbit
mosaics. Input counts, retrieval scope and mask definitions therefore differ.

# Visual comparison by index

The following panels are the source report images used in the combined atlas. They
share the same dimensions, Swiss cutline and fixed colour scale.

## NDVI

| Copernicus Sentinel-2 L2A | swissEO S2-SR |
|---|---|
| ![Copernicus NDVI July 2024](</Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/sentinel-copernicus/year=2024/month=07/ndvi.png>) | ![swissEO NDVI July 2024](</Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/swisseo/year=2024/month=07/ndvi.png>) |

## GNDVI

| Copernicus Sentinel-2 L2A | swissEO S2-SR |
|---|---|
| ![Copernicus GNDVI July 2024](</Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/sentinel-copernicus/year=2024/month=07/gndvi.png>) | ![swissEO GNDVI July 2024](</Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/swisseo/year=2024/month=07/gndvi.png>) |

## NDRE

| Copernicus Sentinel-2 L2A | swissEO S2-SR |
|---|---|
| ![Copernicus NDRE July 2024](</Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/sentinel-copernicus/year=2024/month=07/ndre.png>) | ![swissEO NDRE July 2024](</Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/swisseo/year=2024/month=07/ndre.png>) |

## NDWI

| Copernicus Sentinel-2 L2A | swissEO S2-SR |
|---|---|
| ![Copernicus NDWI July 2024](</Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/sentinel-copernicus/year=2024/month=07/ndwi.png>) | ![swissEO NDWI July 2024](</Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/swisseo/year=2024/month=07/ndwi.png>) |

## Qualitative observations

- Both products reproduce the same major spatial pattern: positive vegetation
  indices across forested and agricultural regions and lower signals in the Alps.
- Lakes and other water bodies are clearly distinguishable and generally occupy the
  negative end of the NDVI/GNDVI/NDRE colour scale.
- The Copernicus result contains a conspicuous rectangular missing-data region in
  western/northwestern Switzerland. The swissEO result has a different, more
  fragmented missing-data pattern in that area.
- Those differences should be interpreted first as coverage and masking differences,
  not as vegetation change. Cloud, cloud shadow, terrain shadow, snow, nodata and
  source-orbit coverage can all remove pixels.
- A formal cross-source coverage percentage was not added to the lightweight atlas.
  The atlas is a visual comparison of validated source products, while the source
  manifests retain the processing contracts and provenance.

# Validation record

The four retained scientific COGs were inspected after publication. All four have:

- `LAYOUT=COG`;
- DEFLATE compression;
- band-interleaved storage and internal cubic overviews;
- `Float32` pixels;
- `EPSG:2056` coordinates;
- 20 m pixels on the same 17,423 × 11,034 grid; and
- nodata `-9999` on every band.

The reflectance band descriptions are `B03`, `B04`, `B05`, `B08`, `B8A`, `B11`.
The index band descriptions are `NDVI`, `GNDVI`, `NDRE`, `NDWI`. The generated
index equations are bounded to the expected −1 to +1 domain for valid pixels.

# FORCE evaluation and exclusion from the final report

FORCE was genuinely run for July 2024; it was not merely planned. The native ARM64
image completed Level-2 processing for all 83 Sentinel-2 L1C tile-scenes. The
resulting database contained 83 completed BOA/QAI scene mosaics and 1,403 FORCE
30 km tiles. One corrupt Sentinel-2 B12 JP2 was re-fetched from the public AWS
Sentinel-2 archive, validated, and successfully reprocessed.

The monthly FORCE aggregation then screened all 83 scenes but did not complete the
national median. The current implementation materialized a large six-band
`Float32` raster for every screened scene and calculated its median over the full
51,000 × 45,000-pixel union grid before clipping to Switzerland. Approximate peak
storage was:

| FORCE component | Approximate size |
|---|---:|
| Completed Level-2 BOA/QAI inputs | 129 GB |
| 83 screened temporary scene rasters | 247 GB |
| Growing union-grid median at failure | more than 58 GB |
| Observed FORCE workspace near failure | more than 434 GB |

The monthly run began at 2026-09-02 21:38 CEST and failed at 2026-09-03 14:34 CEST
with `No space left on device` during the median calculation. This was still a
July-only run; the storage consumption was caused by full-grid intermediates, not by
accidentally processing several months and not primarily by lack of parallelism.

The project decision was to exclude FORCE from the present comparison because the
additional compute and storage investment was not justified for this pilot. The
generated FORCE datasets were removed from the local workspace and upscale share.
They are not recoverable in place but can be regenerated from source data. FORCE
source code and configuration remain in the repository, and these Docker images
were retained:

```text
terravault/force:3.10.04-arm64-native
terravault/force:3.10.04-arm64
```

If FORCE is revisited, the monthly route should first be changed to clip each scene
to the Swiss target extent before materialization, use a bounded or streaming median,
and refuse to start unless a peak-storage preflight succeeds.

# Scientific data paths

The paths below require the `upscale` share to be mounted at `/Volumes/upscale`.

## Copernicus monthly data

```text
/Volumes/upscale/working_package_4/terravault/data/copernicus-monthly/year=2024/month=07/switzerland_copernicus_l2a_2024_07_20m_reflectance.tif
/Volumes/upscale/working_package_4/terravault/data/copernicus-monthly/year=2024/month=07/switzerland_copernicus_l2a_2024_07_20m_indices.tif
/Volumes/upscale/working_package_4/terravault/data/copernicus-monthly/year=2024/month=07/switzerland_copernicus_l2a_2024_07_20m.manifest.json
```

## swissEO monthly data

```text
/Volumes/upscale/working_package_4/terravault/data/swisseo-monthly/year=2024/month=07/switzerland_swisseo_s2_sr_2024_07_20m_reflectance.tif
/Volumes/upscale/working_package_4/terravault/data/swisseo-monthly/year=2024/month=07/switzerland_swisseo_s2_sr_2024_07_20m_indices.tif
/Volumes/upscale/working_package_4/terravault/data/swisseo-monthly/year=2024/month=07/switzerland_swisseo_s2_sr_2024_07_20m.manifest.json
```

Current final monthly data sizes are approximately 3.8 GB for Copernicus and 3.5 GB
for swissEO. The report preview tree, including the combined atlas, is approximately
19 MB. The scientific COGs are not duplicated inside the report tree.

# Complete report-image manifest

| Figure | Absolute path |
|---|---|
| Combined 2 × 4 atlas | `/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/comparison-atlas-copernicus-vs-swisseo-2024-07.png` |
| Copernicus NDVI | `/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/sentinel-copernicus/year=2024/month=07/ndvi.png` |
| Copernicus GNDVI | `/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/sentinel-copernicus/year=2024/month=07/gndvi.png` |
| Copernicus NDRE | `/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/sentinel-copernicus/year=2024/month=07/ndre.png` |
| Copernicus NDWI | `/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/sentinel-copernicus/year=2024/month=07/ndwi.png` |
| swissEO NDVI | `/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/swisseo/year=2024/month=07/ndvi.png` |
| swissEO GNDVI | `/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/swisseo/year=2024/month=07/gndvi.png` |
| swissEO NDRE | `/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/swisseo/year=2024/month=07/ndre.png` |
| swissEO NDWI | `/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/swisseo/year=2024/month=07/ndwi.png` |

# Summary and log paths

```text
# Source report summaries
/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/sentinel-copernicus/year=2024/month=07/summary.json
/Volumes/upscale/working_package_4/terravault/reports/switzerland-monthly-2024/sources/swisseo/year=2024/month=07/summary.json

# Operational logs retained on upscale
/Volumes/upscale/working_package_4/terravault/logs/copernicus-l2a-2024-07.log
/Volumes/upscale/working_package_4/terravault/logs/copernicus-monthly-2024-07.log
/Volumes/upscale/working_package_4/terravault/logs/copernicus-monthly-2024-07-resume.log
/Volumes/upscale/working_package_4/terravault/logs/swisseo-2024-07.log
```

The FORCE logs were removed with the generated FORCE workspace. The file
`comparison-2024-07-copernicus-swisseo.log` records only the start of an aborted
heavyweight comparison-statistics pass and should not be cited as a completed run.

# Operational notes for reproducibility

- The bulk-data and report root is
  `/Volumes/upscale/working_package_4/terravault/` on an SMB mount.
- In-progress GeoTIFFs should be created on a local volume and copied sequentially
  to SMB after completion; random-seek GeoTIFF writes on SMB were unreliable.
- DuckDB catalogues should not live on SMB because network filesystem locking is not
  supported reliably.
- The Python environment used was the brew Miniforge environment `terra`, with GDAL
  3.12.2, NumPy 2.5.2 and DuckDB 1.5.5.
- The `upscale` mount and any external staging volume can disconnect when the laptop
  sleeps or the network/VPN changes. Mount availability must be checked before a run.
- At the end of cleanup, the laptop had approximately 461 GiB free. No FORCE job or
  continuation monitor remained active.

# Suggested report structure

For a concise final report, use this document in the following order:

1. Executive summary and combined atlas.
2. Study definition and shared index equations.
3. Copernicus and swissEO methods.
4. Timing comparison, with the benchmark-boundary caveat.
5. Four paired index figures and qualitative observations.
6. Validation record and data provenance.
7. FORCE evaluation as an operational appendix rather than a third scientific result.
8. Scientific data and image manifest as the reproducibility appendix.

