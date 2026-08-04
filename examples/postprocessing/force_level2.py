#!/usr/bin/env python3
"""Run TerraVault's native FORCE L1C-to-Level-2 command."""

from __future__ import annotations

import sys

from terravault.cli import main


if __name__ == "__main__":
    main(["force-level2", *sys.argv[1:]])
