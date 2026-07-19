#!/usr/bin/env python3
"""Run TerraVault's FORCE NDVI visualization command."""

from __future__ import annotations

import sys

from terravault.cli import main


if __name__ == "__main__":
    main(["force-visualize", *sys.argv[1:]])
