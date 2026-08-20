# Switzerland Patch Retrieval Example

This folder contains runnable TerraVault scripts for retrieving small patches
and country-wide Sentinel-2 test products over Switzerland.

Default downloaded asset is `thumbnail` (public HTTPS, easy smoke test).

## Run

From the repository root:

```bash
conda activate terra
python examples/switzerland_patch/retrieve_patch.py
```

Metadata-only test:

```bash
python examples/switzerland_patch/retrieve_patch.py --no-download
```

Fresh run with an isolated state DB (useful for repeated testing):

```bash
python examples/switzerland_patch/retrieve_patch.py \
  --lookback-hours 720 \
  --state-db examples/switzerland_patch/data/terravault_state_fresh.db
```

Try Sentinel-2 10m red+NIR bands:

```bash
python examples/switzerland_patch/retrieve_patch.py \
  --asset-keys B04_10m B08_10m \
  --lookback-hours 720
```

Note: Copernicus STAC commonly returns these band assets as `s3://eodata/...`
HREFs. Those require an S3-capable access path/credentials; plain `requests`
downloads will fail on those links.

## Account-backed download auth

There is no separate CDSE API-key field for this path. Use your CDSE account
to obtain a bearer token, or let TerraVault request one:

```bash
export TERRAVAULT_CDSE_USERNAME="your-cdse-user"
export TERRAVAULT_CDSE_PASSWORD="your-cdse-password"
python examples/switzerland_patch/retrieve_patch.py --asset-keys thumbnail
```

## Raw raster patch fetch

For a real pixel subset, use the Sentinel Hub Process API example. This
requires a Sentinel Hub OAuth client ID/secret from the CDSE dashboard,
not your account password:

```bash
cat > .env <<'EOF'
TERRAVAULT_CDSE_SH_CLIENT_ID="..."
TERRAVAULT_CDSE_SH_CLIENT_SECRET="..."
EOF

python examples/switzerland_patch/fetch_raw_patch.py \
  --time-from 2024-06-01T00:00:00Z \
  --time-to 2024-06-30T23:59:59Z \
  --bands B04 B08
```

## Latest Basel patch

To automatically discover the newest Sentinel-2 scene over Basel and fetch
the patch at Sentinel-2's highest native 10 m resolution:

```bash
cp .env.example .env
python examples/switzerland_patch/fetch_latest_basel_patch.py
```

This writes `examples/switzerland_patch/data/basel_latest_patch.tif` and
`examples/switzerland_patch/data/basel_latest_patch.png` plus a
false-color preview `examples/switzerland_patch/data/basel_latest_patch_false_color.png`.
It also writes a JSON sidecar listing the raw band order.

To split a multiband Basel TIFF into individual band files:

```bash
python examples/switzerland_patch/extract_bands.py
```

To also generate grayscale validation PNGs for each extracted band:

```bash
python examples/switzerland_patch/extract_bands.py --validate-pngs
```

## Switzerland-wide overview

The credential-free overview helper discovers clear L2A granules for a fixed
test day and stitches their public quicklooks:

```bash
python -m pip install -e ".[overview]"
python examples/switzerland_patch/build_switzerland_overview.py \
  --date 2026-07-04 \
  --width 2400
```

This is a visual JPEG, not an analysis-ready raster.

To create a current rolling overview, select the newest public quicklook for
each MGRS tile in the preceding 14 days:

```bash
python examples/switzerland_patch/build_switzerland_overview.py \
  --latest \
  --max-cloud-cover 100 \
  --width 4000
```

If a metadata-only rolling run already created `dataset.duckdb`, render the
exact completed scenes stored there without repeating the STAC query:

```bash
python examples/switzerland_patch/build_switzerland_overview.py \
  --dataset-db artifacts/switzerland_cloud20/dataset.duckdb \
  --database-selection all \
  --max-cloud-cover 20 \
  --width 2400 \
  --output artifacts/switzerland_cloud20/switzerland_overview.jpg
```

The same command accepts a `force_images.duckdb` produced by
`terravault force-pipeline`. `--database-selection all` paints every stored
scene; the default `latest-per-tile` keeps one representative per MGRS tile.
The overview and manifest paths, item IDs and cloud threshold are registered
back into either database.

For a georeferenced, feature-selected result, use `terravault extract` against
downloaded native pieces.

## Switzerland-wide all-layer snapshot

Inspect the generated Process API requests without credentials:

```bash
python examples/switzerland_patch/fetch_switzerland_snapshot.py --dry-run
```

With valid Sentinel Hub OAuth credentials in `.env`, remove `--dry-run` to
create three type-correct GeoTIFF groups (spectral, quality and angles) and a
true-colour PNG:

```bash
python examples/switzerland_patch/fetch_switzerland_snapshot.py
```

The fixed default date, 2026-07-04, was verified against live CDSE STAC as a
clear same-day multi-swath test. See
[`../../docs/SWITZERLAND_SENTINEL2_PIPELINE.md`](../../docs/SWITZERLAND_SENTINEL2_PIPELINE.md)
for the rolling production design.

## Output location

All output is written under:

```text
examples/switzerland_patch/data/
```

This includes:

- `terravault_state.db`
- `satellite_data/` downloaded scene metadata/assets

`data/` contents are gitignored in the project `.gitignore`.
