"""Shared pytest fixtures.

Puts each plugin's source directory onto sys.path so tests can import
`webp_to_jpeg` (etc.) directly without packaging.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = ROOT / "plugins"

for plugin_dir in PLUGINS_ROOT.iterdir():
    if plugin_dir.is_dir():
        sys.path.insert(0, str(plugin_dir))
