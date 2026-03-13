# Switzerland Patch Retrieval Example

This folder contains a runnable TerraVault script for retrieving a small
Sentinel-2 patch over Switzerland (default: Zurich area).

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

## Output location

All output is written under:

```text
examples/switzerland_patch/data/
```

This includes:

- `terravault_state.db`
- `satellite_data/` downloaded scene metadata/assets

`data/` contents are gitignored in the project `.gitignore`.
