#!/usr/bin/env bash
# Regenerate the July 2024 Switzerland monthly composites on a 10 m grid.
#
# Strategy: everything heavy happens on the local disk, and the finished
# products are copied to the upscale share afterwards. Two reasons --
# random-seek GeoTIFF writes over SMB proved unreliable during the 20 m run,
# and the share should only ever see completed files.
#
# The existing 20 m products are not touched: output filenames carry the
# resolution, so these land beside them as *_10m_*.tif.
#
#     conda activate terra
#     cd ~/github/terravault
#     bash scripts/run_july_2024_10m.sh preflight
#     bash scripts/run_july_2024_10m.sh swisseo     # ~4-5 h
#     bash scripts/run_july_2024_10m.sh clean       # frees the swissEO scratch
#     bash scripts/run_july_2024_10m.sh copernicus  # ~10 h
#     bash scripts/run_july_2024_10m.sh publish     # copy to the share
set -euo pipefail

SHARE=/Volumes/upscale/working_package_4/terravault
ROI="$SHARE/data/roi/switzerland-boundary.geojson"
LOGS="$SHARE/logs"

# Local working area. Override with WORK=/Volumes/T7/terravault-10m if you
# would rather use the external SSD.
WORK="${WORK:-$HOME/terravault-10m}"
SCRATCH="$WORK/scratch"       # intermediates, deleted between stages
STAGE="$WORK/out"             # finished products, before publishing
DATASET_DB="${DATASET_DB:-/Volumes/T7/terravault/copernicus-dataset.duckdb}"

# The 20 m defaults are sized for a 7 GiB output and a 32 GiB scratch.
# At 10 m both scale 4x, so the stock ceilings reject the run outright.
MAX_OUTPUT_GIB=48
MAX_TEMP_GIB=192

avail_gib () { df -k "$1" | awk 'NR==2 {printf "%d", $4/1048576}'; }

need_free () {  # need_free <path> <gib>
  local path="$1" want="$2" have
  mkdir -p "$path"
  have=$(avail_gib "$path")
  printf '  %-42s %5s GiB free  (need %s)\n' "$path" "$have" "$want"
  [ "$have" -ge "$want" ] || { echo "  ^^ NOT ENOUGH SPACE"; return 1; }
}

preflight () {
  echo "== inputs =="
  for p in "$ROI" "$DATASET_DB"; do
    [ -e "$p" ] && echo "  ok      $p" || { echo "  MISSING $p"; exit 1; }
  done
  echo "== space =="
  # swissEO scratch peaks near 127 GiB; Copernicus intermediates are not
  # estimated by the pipeline at all, so 200 covers either stage alone.
  need_free "$WORK" 200
  need_free "$SHARE/data" 70
  echo "== environment =="
  gdalinfo --version
  python -c "import terravault; print('terravault', terravault.__file__)"
  echo "== dry runs =="
  swisseo --dry-run
  copernicus --dry-run
  echo
  echo "Run the stages one at a time; 'clean' between them keeps the peak down."
}

swisseo () {
  mkdir -p "$SCRATCH/swisseo" "$STAGE/swisseo-monthly" "$LOGS"
  python -m terravault.cli swisseo-monthly \
    --year 2024 --months 7 \
    --roi "$ROI" \
    --resolution 10 \
    --output-root "$STAGE/swisseo-monthly" \
    --temporary-root "$SCRATCH/swisseo" \
    --max-output-gib "$MAX_OUTPUT_GIB" \
    --max-temporary-gib "$MAX_TEMP_GIB" \
    --publication-wait-seconds 21600 \
    --log-file "$LOGS/swisseo-2024-07-10m.log" \
    "$@"
  report swisseo
}

copernicus () {
  mkdir -p "$SCRATCH/copernicus" "$STAGE/copernicus-monthly" "$LOGS"
  # This route has no temporary-storage estimate and its per-scene and
  # per-tile intermediates are 4x larger at 10 m. Watch $SCRATCH while it runs:
  #     watch -n 60 du -sh ~/terravault-10m/scratch
  python -m terravault.cli copernicus-monthly \
    --year 2024 --months 7 \
    --roi "$ROI" \
    --resolution 10 \
    --dataset-db "$DATASET_DB" \
    --output-root "$STAGE/copernicus-monthly" \
    --staging-root "$SCRATCH/copernicus" \
    --max-cloud-cover 20 \
    --max-output-gib "$MAX_OUTPUT_GIB" \
    --log-file "$LOGS/copernicus-monthly-2024-07-10m.log" \
    "$@"
  report copernicus
}

report () {  # record what the stage actually cost, since nothing else does
  echo "== $1 peak-ish usage =="
  du -sh "$SCRATCH/$1" 2>/dev/null || true
  find "$STAGE" -name "*_10m_*.tif" -exec ls -lh {} \; | awk '{print "  ", $5, $NF}'
}

clean () {
  echo "Removing intermediates under $SCRATCH (finished products in $STAGE are kept)."
  rm -rf "${SCRATCH:?}"/*
  df -h "$WORK" | tail -1
}

publish () {
  local n
  n=$(find "$STAGE" -name "*_10m_*" -type f | wc -l | tr -d ' ')
  [ "$n" -gt 0 ] || { echo "Nothing staged in $STAGE"; exit 1; }
  echo "Copying $n files to $SHARE/data ..."
  rsync -ah --progress --info=progress2 \
    "$STAGE/swisseo-monthly/"    "$SHARE/data/swisseo-monthly/"
  rsync -ah --progress --info=progress2 \
    "$STAGE/copernicus-monthly/" "$SHARE/data/copernicus-monthly/"
  echo "Published. Verify, then remove $STAGE by hand."
}

case "${1:-preflight}" in
  preflight)  preflight ;;
  swisseo)    shift; swisseo "$@" ;;
  copernicus) shift; copernicus "$@" ;;
  clean)      clean ;;
  publish)    publish ;;
  *) echo "usage: $0 {preflight|swisseo|clean|copernicus|publish}" >&2; exit 2 ;;
esac
