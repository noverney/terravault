#!/usr/bin/env bash
# Regenerate the July 2024 Switzerland monthly composites on a 10 m grid.
#
# The 20 m products already on the upscale share are NOT touched: the output
# filenames carry the resolution, so these land beside them as *_10m_*.tif.
#
# Run from the repository root inside the `terra` environment:
#     conda activate terra
#     bash scripts/run_july_2024_10m.sh preflight
#     bash scripts/run_july_2024_10m.sh swisseo
#     bash scripts/run_july_2024_10m.sh copernicus
#
# Use `all` to chain both. Expect roughly 4x the 20 m runtime:
# swissEO ~4-5 h, Copernicus ~10 h.
set -euo pipefail

ROOT=/Volumes/upscale/working_package_4/terravault
ROI="$ROOT/data/roi/switzerland-boundary.geojson"
LOGS="$ROOT/logs"
SCRATCH=/Volumes/T7/terravault           # fast local volume for intermediates
DATASET_DB="$SCRATCH/copernicus-dataset.duckdb"

# The 20 m run needed ~32 GiB temporary and produced ~7 GiB of output.
# Both scale with the square of the resolution change, so 10 m needs ~4x:
#   estimated peak temporary (swissEO) ~127 GiB  -> default ceiling of 128 is too tight
#   estimated uncompressed output      ~28.5 GiB -> default ceiling of 16 fails outright
MAX_OUTPUT_GIB=48
MAX_TEMP_GIB=192

need_free () {  # need_free <path> <gib>
  local path="$1" want="$2" avail
  [ -d "$path" ] || { echo "MISSING: $path"; return 1; }
  avail=$(df -g "$path" | awk 'NR==2 {print $4}')
  printf '  %-46s %6s GiB free (need %s)\n' "$path" "$avail" "$want"
  [ "$avail" -ge "$want" ] || { echo "  ^ NOT ENOUGH SPACE"; return 1; }
}

preflight () {
  echo "== paths =="
  for p in "$ROI" "$DATASET_DB"; do
    [ -e "$p" ] && echo "  ok      $p" || { echo "  MISSING $p"; exit 1; }
  done
  echo "== free space =="
  need_free "$SCRATCH" 200          # swissEO temp ~127 GiB + Copernicus staging
  need_free "$ROOT/data" 70         # two sources x ~29 GiB uncompressed
  echo "== gdal =="
  gdalinfo --version
  python -c "import terravault, sys; print('terravault', terravault.__file__)"
  echo "== dry runs =="
  swisseo --dry-run
  copernicus --dry-run
}

swisseo () {
  mkdir -p "$LOGS"
  python -m terravault.cli swisseo-monthly \
    --year 2024 --months 7 \
    --roi "$ROI" \
    --resolution 10 \
    --output-root "$ROOT/data/swisseo-monthly" \
    --temporary-root "$SCRATCH/tmp" \
    --max-output-gib "$MAX_OUTPUT_GIB" \
    --max-temporary-gib "$MAX_TEMP_GIB" \
    --publication-wait-seconds 21600 \
    --log-file "$LOGS/swisseo-2024-07-10m.log" \
    "$@"
}

copernicus () {
  mkdir -p "$LOGS"
  # No temporary-storage estimate exists for this route and its per-scene
  # intermediates are ~4x larger at 10 m. The 20 m run already filled the T7
  # once, so watch $SCRATCH while this runs.
  python -m terravault.cli copernicus-monthly \
    --year 2024 --months 7 \
    --roi "$ROI" \
    --resolution 10 \
    --dataset-db "$DATASET_DB" \
    --output-root "$ROOT/data/copernicus-monthly" \
    --staging-root "$SCRATCH/copernicus-staging" \
    --max-cloud-cover 20 \
    --max-output-gib "$MAX_OUTPUT_GIB" \
    --log-file "$LOGS/copernicus-monthly-2024-07-10m.log" \
    "$@"
}

case "${1:-preflight}" in
  preflight)  preflight ;;
  swisseo)    shift; swisseo "$@" ;;
  copernicus) shift; copernicus "$@" ;;
  all)        swisseo; copernicus ;;
  *) echo "usage: $0 {preflight|swisseo|copernicus|all}" >&2; exit 2 ;;
esac
