"""Minimal .env loader used by CLI and example scripts."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env", override: bool = False) -> Path | None:
    """Load simple KEY=VALUE entries from *path* into ``os.environ``."""

    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = (Path.cwd() / env_path).resolve()
    if not env_path.exists():
        return None

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value

    return env_path
