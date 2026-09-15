#!/usr/bin/env bash
# Regenerate the July 2024 Switzerland monthly composites on a 10 m grid.
#
# All heavy work happens on the local disk; finished products are copied to
# the upscale share at the end. Two reasons: random-seek GeoTIFF writes over
# SMB proved unreliable during the 20 m run, and the share should only ever
# see completed files.
#
# The T7 is not used. The one thing that ever lived there and is still
# needed is the Copernicus DuckDB catalogue -- copy it once with the
# `fetch-db` stage, after which the drive can stay unplugged. (DuckDB must
# not live on the share: network-filesystem locking is unreliable there.)
#
# The existing 20 m products are untouched; output filenames carry the
# resolution, so these land beside them as *_10m_*.tif.
#
#     conda activate terra
#     cd ~/github/terravault
#     bash scripts/run_july_2024_10m.sh fetch-db     # once, T7 attached
#     bash scripts/run_july_2024_10m.sh preflight
#     bash scripts/run_july_2024_10m.sh swisseo      # ~4-5 h
#     bash scripts/run_july_2024_10m.sh clean
#     bash scripts/run_july_2024_10m.sh copernicus   # ~10 h
#     bash scripts/run_july_2024_10m.sh compare      # local, before publishing
#     bash scripts/run_july_2024_10m.sh publish
set -euo pipefail

SHARE=/Volumes/upscale/working_package_4/terravault
ROI_SHARE="$SHARE/data/roi/switzerland-boundary.geojson"
L2A="$SHARE/data/copernicus-l2a"          # 520 JP2 assets, ~23 GB, read-only here
WORK="${WORK:-$HOME/terravault-10m}"      # everything local lives under here
SCRATCH="$WORK/scratch"                   # intermediates, discarded between stages
STAGE="$WORK/out"                         # finished products, before publishing
COMPARE="$WORK/comparison"               # comparison outputs, before publishing
DATASET_DB="${DATASET_DB:-$WORK/copernicus-dataset.duckdb}"
# Logs stay local while a stage runs. A wedged SMB mount blocks writes
# uninterruptibly, so logging straight to the share put it back on the
# critical path for the whole run even though nothing else needed it.
LOGS="${LOGS:-$WORK/logs}"
# The ROI doubles as the gdalwarp cutline, which is read at the very end of
# a run -- long after the inputs are staged. A share that wedges by then
# would hang the run just before it finishes, so keep a local copy.
ROI="${ROI:-$WORK/roi/switzerland-boundary.geojson}"
# The month is overridable so the same route can be re-run for another
# period without editing the script: YEAR=2026 MONTH=7 bash ... swisseo
YEAR="${YEAR:-2024}"
MONTH="${MONTH:-7}"
MM=$(printf '%02d' "$MONTH")
# swissEO product version. v100 keeps every original directory and filename;
# any other version gets its own output, scratch and log names so it can never
# overwrite a v100 product:  SWISSEO_VERSION=v200 bash ... swisseo
SWISSEO_VERSION="${SWISSEO_VERSION:-v100}"
if [ "$SWISSEO_VERSION" = "v100" ]; then
  SWISSEO_TAG="swisseo"
else
  SWISSEO_TAG="swisseo-$SWISSEO_VERSION"
fi
T7_DATASET_DB="${T7_DATASET_DB:-/Volumes/T7/terravault/copernicus-dataset.duckdb}"

# The stock ceilings are sized for 20 m (7 GiB output, 32 GiB scratch).
# At 10 m both scale 4x, so the defaults reject the run outright.
# Overridable: the swissEO estimator is heavily conservative -- it predicted
# 127.5 GiB for July 2024 and the run used 39.3. Raise deliberately, after
# checking free space, rather than editing these defaults.
MAX_OUTPUT_GIB="${MAX_OUTPUT_GIB:-48}"
MAX_TEMP_GIB="${MAX_TEMP_GIB:-192}"

avail_gib () { df -k "$1" | awk 'NR==2 {printf "%d", $4/1048576}'; }

stage_roi () {
  [ -f "$ROI" ] && return 0
  mkdir -p "$(dirname "$ROI")"
  cp "$ROI_SHARE" "$ROI"
  echo "Staged ROI locally: $ROI"
}

need_free () {  # need_free <path> <gib>
  local path="$1" want="$2" have
  mkdir -p "$path"
  have=$(avail_gib "$path")
  printf '  %-44s %5s GiB free  (need %s)\n' "$path" "$have" "$want"
  [ "$have" -ge "$want" ] || { echo "  ^^ NOT ENOUGH SPACE"; return 1; }
}

fetch_db () {
  mkdir -p "$WORK"
  if [ -f "$DATASET_DB" ]; then
    echo "Catalogue already local: $DATASET_DB"; return 0
  fi
  [ -f "$T7_DATASET_DB" ] || {
    echo "Cannot find the catalogue at $T7_DATASET_DB"
    echo "Attach the T7 once, or set T7_DATASET_DB to wherever it lives."
    exit 1
  }
  echo "Copying the Copernicus catalogue to local disk ..."
  rsync -ah --progress "$T7_DATASET_DB" "$DATASET_DB"
  echo "Done. The T7 is no longer needed."
}

preflight () {
  stage_roi
  echo "== inputs =="
  for p in "$ROI" "$L2A" "$DATASET_DB"; do
    [ -e "$p" ] && echo "  ok      $p" || { echo "  MISSING $p"; exit 1; }
  done
  printf '  %-8s %s L2A assets on the share\n' "ok" \
    "$(find "$L2A/pieces" -name '*.jp2' | wc -l | tr -d ' ')"
  echo "== space =="
  # swissEO scratch peaks near 127 GiB; the Copernicus route publishes no
  # estimate at all, so 200 covers either stage on its own.
  need_free "$WORK" 200
  need_free "$SHARE/data" 70
  echo "== environment =="
  gdalinfo --version
  python -c "import terravault; print('terravault', terravault.__file__)"
  echo "== dry runs =="
  swisseo --dry-run
  copernicus --dry-run
  echo
  echo "Stages are meant to be run one at a time, with 'clean' between them."
}

swisseo () {
  stage_roi
  mkdir -p "$SCRATCH/$SWISSEO_TAG" "$STAGE/$SWISSEO_TAG-monthly" "$LOGS"
  python -m terravault.cli swisseo-monthly \
    --product-version "$SWISSEO_VERSION" \
    --year "$YEAR" --months "$MONTH" \
    --roi "$ROI" \
    --resolution 10 \
    --output-root "$STAGE/$SWISSEO_TAG-monthly" \
    --temporary-root "$SCRATCH/$SWISSEO_TAG" \
    --max-output-gib "$MAX_OUTPUT_GIB" \
    --max-temporary-gib "$MAX_TEMP_GIB" \
    --publication-wait-seconds 21600 \
    --log-file "$LOGS/$SWISSEO_TAG-$YEAR-$MM-10m.log" \
    "$@"
  report "$SWISSEO_TAG"
}

copernicus () {
  stage_roi
  mkdir -p "$SCRATCH/copernicus" "$STAGE/copernicus-monthly" "$LOGS"
  # No temporary-storage estimate exists for this route and its per-scene and
  # per-tile intermediates are 4x larger at 10 m. Watch it while it runs:
  #     watch -n 60 du -sh ~/terravault-10m/scratch
  python -m terravault.cli copernicus-monthly \
    --year "$YEAR" --months "$MONTH" \
    --roi "$ROI" \
    --resolution 10 \
    --dataset-db "$DATASET_DB" \
    --output-root "$STAGE/copernicus-monthly" \
    --staging-root "$SCRATCH/copernicus" \
    --max-cloud-cover 20 \
    --max-output-gib "$MAX_OUTPUT_GIB" \
    --log-file "$LOGS/copernicus-monthly-$YEAR-$MM-10m.log" \
    "$@"
  report copernicus
}

report () {  # record what a stage actually cost, since nothing else does
  echo "== $1: scratch used, products written =="
  du -sh "$SCRATCH/$1" 2>/dev/null || true
  find "$STAGE" -name "*_10m_*.tif" -exec ls -lh {} \; | awk '{print "  ", $5, $NF}'
}

clean () {
  echo "Removing intermediates under $SCRATCH (products in $STAGE are kept)."
  rm -rf "${SCRATCH:?}"/*
  df -h "$WORK" | tail -1
}

compare () {
  # Run this BEFORE publish. The comparison reads every product several times
  # over -- statistics, histograms, index maps, false colour -- so pointing it
  # at the share makes it far slower than the compositing it summarises.
  stage_roi
  mkdir -p "$COMPARE" "$LOGS"
  python -m terravault.cli compare-monthly \
    --year "$YEAR" --months "$MONTH" \
    --roi "$ROI" \
    --product-resolution 10 \
    --analysis-resolution 10 \
    --sources copernicus swisseo \
    --copernicus-root "$STAGE/copernicus-monthly" \
    --swisseo-root "$STAGE/swisseo-monthly" \
    --output-root "$COMPARE" \
    --log-file "$LOGS/compare-$YEAR-$MM-10m.log" \
    "$@"
}

publish () {
  local n
  n=$(find "$STAGE" -name "*_10m_*" -type f | wc -l | tr -d ' ')
  [ "$n" -gt 0 ] || { echo "Nothing staged in $STAGE"; exit 1; }
  echo "Copying $n files to $SHARE/data ..."
  # macOS ships openrsync (2.6.9-compatible), which rejects --info=progress2
  # and exits before copying anything. Probe for it rather than assume.
  progress=""
  if rsync --info=progress2 --version >/dev/null 2>&1; then
    progress="--info=progress2"
  elif rsync --progress --version >/dev/null 2>&1; then
    progress="--progress"
  fi
  rsync -ah $progress "$STAGE/swisseo-monthly/"    "$SHARE/data/swisseo-monthly/"
  rsync -ah $progress "$STAGE/copernicus-monthly/" "$SHARE/data/copernicus-monthly/"
  rsync -ah "$LOGS/" "$SHARE/logs/"
  if [ -d "$COMPARE" ]; then
    echo "Copying comparison outputs ..."
    rsync -ah $progress "$COMPARE/" "$SHARE/reports/switzerland-monthly-2024/comparison-10m/"
  else
    echo "NOTE: nothing in $COMPARE -- run the compare stage before publish."
  fi
  echo
  echo "Published. Compare the two resolutions with:"
  echo "  python -m terravault.cli compare-monthly --year 2024 --months 7 \\"
  echo "    --product-resolution 10 --roi $ROI ..."
  echo "Verify the copies, then remove $STAGE by hand."
}

case "${1:-preflight}" in
  fetch-db)   fetch_db ;;
  preflight)  preflight ;;
  swisseo)    shift; swisseo "$@" ;;
  copernicus) shift; copernicus "$@" ;;
  clean)      clean ;;
  compare)    shift; compare "$@" ;;
  publish)    publish ;;
  *) echo "usage: $0 {fetch-db|preflight|swisseo|clean|copernicus|compare|publish}" >&2; exit 2 ;;
esac
