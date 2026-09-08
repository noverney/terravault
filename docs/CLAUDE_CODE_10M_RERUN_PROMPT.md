# Starting prompt — 10 m rerun of the July 2024 Switzerland composites

Paste the block below into Claude Code, started from `~/github/terravault`
with the `terra` conda environment active and the T7 attached for the first
stage only.

---

I want to regenerate the July 2024 Switzerland monthly vegetation composites on a
10 m grid instead of the 20 m grid used for the existing products. Everything is
prepared; your job is to run it, watch it, and report what it actually cost.

**Environment.** Repo is `~/github/terravault`, conda env `terra` (GDAL 3.12.2,
NumPy 2.5.2, DuckDB 1.5.5). Run `bash scripts/run_july_2024_10m.sh` — it has
stages `fetch-db`, `preflight`, `swisseo`, `clean`, `copernicus`, `publish`. Read
that script before you start; it documents the reasoning behind every flag.

**What exists.** The 20 m products are on the upscale share at
`/Volumes/upscale/working_package_4/terravault/data/{copernicus,swisseo}-monthly/year=2024/month=07/`.
Do not touch, move or overwrite them — output filenames carry the resolution, so
the 10 m products land beside them as `*_10m_*.tif`. The Copernicus L2A inputs
(520 JP2 assets, ~23 GB) are on the same share under `data/copernicus-l2a/`.

**Plan.** Run the stages one at a time, in order, and stop to report if anything
looks wrong:

1. `fetch-db` — copies `copernicus-dataset.duckdb` off the T7 to
   `~/terravault-10m/`. The route refuses to start without it, and DuckDB cannot
   live on the SMB share. After this the T7 can be unplugged.
2. `preflight` — checks inputs, free space, GDAL, and dry-runs both routes.
   Do not proceed if it reports anything missing.
3. `swisseo` — roughly 4–5 hours.
4. `clean` — frees the swissEO scratch (~127 GiB) before the next stage.
5. `copernicus` — roughly 10 hours.
6. `publish` — rsyncs the finished products to the share.

**What to watch.**

- The Copernicus route publishes no temporary-storage estimate and its per-scene
  and per-tile intermediates are 4× larger at 10 m. The 20 m run already hit
  "No space left on device" once. Poll `du -sh ~/terravault-10m/scratch` every
  few minutes during that stage and stop it if free space drops below ~40 GiB.
- swissEO streams its 16 orbit mosaics from the swisstopo STAC on every run, so
  that stage needs network throughout and re-downloads roughly 10 GiB per orbit.
- The upscale mount drops when the laptop sleeps or the VPN changes, which would
  kill a run mid-write. Wrap the long stages in `caffeinate -i`, and re-check the
  mount before each stage.
- The default guards are sized for 20 m and the script already raises them
  (`--max-output-gib 48`, `--max-temporary-gib 192`). If either still trips,
  report the number rather than raising it further without checking free space.

**Report back, per route:** wall-clock per stage, peak scratch actually used,
final file sizes, and the width × height the manifest records (expect about
34,846 × 22,068). The peak-scratch figure matters most — nothing in the pipeline
measures it, and it is what decides whether a full twelve-month year is feasible.

**Afterwards.** Render the comparison with
`python -m terravault.cli compare-monthly --year 2024 --months 7 --product-resolution 10 ...`
— the `--product-resolution` flag exists precisely because both resolutions now
share a month directory and discovery would otherwise be ambiguous.

**One caveat to keep in mind when interpreting the result.** Only NDVI and GNDVI
gain real detail at 10 m, since they use B03, B04 and B08, which are natively
10 m. NDRE and NDWI depend on B05, B8A and B11, which are natively 20 m and are
bilinear-upsampled — no new information. Separately, the swissEO quality masks
are natively 10 m and are currently `max`-aggregated down to 20 m, so at a 10 m
target that conservative aggregation stops discarding good pixels: expect
genuinely higher valid-pixel coverage, not just a finer grid. That is a real
difference between the two products, not a resampling artefact, and it should be
stated as such in any comparison.
