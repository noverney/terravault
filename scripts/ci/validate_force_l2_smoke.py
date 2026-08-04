#!/usr/bin/env python3
"""Validate the durable metadata and raster outputs of a FORCE L2PS CI run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--item-id", required=True)
    return parser.parse_args()


def require_file(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size < 1:
        raise RuntimeError(f"Expected non-empty FORCE output is missing: {path}")
    return path


def manifest_paths(manifest: dict[str, Any], key: str) -> list[Path]:
    values = manifest.get(key)
    if not isinstance(values, list) or not values:
        raise RuntimeError(f"FORCE job manifest has no {key}")
    return [require_file(Path(value)) for value in values]


def main() -> int:
    args = parse_args()
    output_root = args.output_root.expanduser().resolve()
    manifest_path = (
        output_root / "_terravault" / "force-l2" / "jobs" / f"{args.item_id}.json"
    )
    require_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("status") != "complete":
        raise RuntimeError(f"FORCE job is not complete: {manifest.get('status')}")
    if manifest.get("queue_status") != "DONE":
        raise RuntimeError(f"FORCE queue did not finish: {manifest.get('queue_status')}")
    if manifest.get("runtime") != "docker":
        raise RuntimeError(f"Expected Docker runtime, got {manifest.get('runtime')}")

    boa_paths = manifest_paths(manifest, "boa_paths")
    qai_paths = manifest_paths(manifest, "qai_paths")
    overview_paths = manifest_paths(manifest, "overview_paths")
    if len(boa_paths) != len(qai_paths) or len(boa_paths) != len(overview_paths):
        raise RuntimeError(
            "FORCE output counts differ: "
            f"BOA={len(boa_paths)} QAI={len(qai_paths)} OVV={len(overview_paths)}"
        )

    boa_mosaic = require_file(Path(str(manifest.get("boa_mosaic_path") or "")))
    qai_mosaic = require_file(Path(str(manifest.get("qai_mosaic_path") or "")))
    require_file(Path(str(manifest.get("parameter_path") or "")))
    require_file(Path(str(manifest.get("progress_path") or "")))

    output_bytes = sum(path.stat().st_size for path in (*boa_paths, *qai_paths, *overview_paths))
    summary = {
        "boa_mosaic": str(boa_mosaic),
        "boa_tiles": len(boa_paths),
        "output_bytes": output_bytes,
        "overview_tiles": len(overview_paths),
        "qai_mosaic": str(qai_mosaic),
        "qai_tiles": len(qai_paths),
        "queue_status": manifest["queue_status"],
        "runtime": manifest["runtime"],
        "status": manifest["status"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

