"""Static checks on each plugin's manifest and entry script.

These are regression guards for bugs that are easy to introduce but
painful to diagnose in a real Stash instance:

- ensure_import() calls must use <import_name>:<pip_name> when the two
  differ (Pillow / PIL, beautifulsoup4 / bs4, etc.)
- manifest's `# requires:` comment must list any plugin referenced by
  ensure_import (so Stash installs them alongside via the source URL)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLUGINS_ROOT = Path(__file__).resolve().parents[1] / "plugins"

# packages whose pip name differs from their python import name
IMPORT_NAME_OVERRIDES = {
    "Pillow": "PIL",
    "beautifulsoup4": "bs4",
    "opencv-python": "cv2",
    "pyyaml": "yaml",
    "scikit-learn": "sklearn",
}


def _plugin_scripts():
    for plugin_dir in PLUGINS_ROOT.iterdir():
        if not plugin_dir.is_dir():
            continue
        for py in plugin_dir.glob("*.py"):
            yield py


@pytest.mark.parametrize("script", list(_plugin_scripts()), ids=lambda p: p.name)
def test_ensure_import_uses_correct_package_prefix(script: Path):
    source = script.read_text()
    calls = re.findall(r"ensure_import\s*\(([^)]*)\)", source, flags=re.DOTALL)
    for args in calls:
        # extract string literals
        for lit in re.findall(r"""["']([^"']+)["']""", args):
            # strip version specifiers
            head = re.split(r"[<>=!@]", lit, maxsplit=1)[0]
            if ":" in head:
                continue  # already uses import_name:pip_name
            pip_name = head.strip()
            expected_import = IMPORT_NAME_OVERRIDES.get(pip_name)
            assert expected_import is None, (
                f"{script.name}: ensure_import({lit!r}) — pip name {pip_name!r} "
                f"has a different python import name ({expected_import!r}). "
                f"Use {expected_import}:{pip_name} instead, or PDM will "
                f"install the package but fail to detect it as importable."
            )


@pytest.mark.parametrize("script", list(_plugin_scripts()), ids=lambda p: p.name)
def test_requires_comment_mentions_pythondepmanager_when_ensure_import_used(
    script: Path,
):
    source = script.read_text()
    if "ensure_import" not in source:
        pytest.skip(f"{script.name} does not use ensure_import")
    manifest = script.with_suffix(".yml")
    assert manifest.exists(), f"missing manifest for {script.name}"
    yml_text = manifest.read_text()
    assert re.search(
        r"^\s*#\s*requires:\s*.*PythonDepManager",
        yml_text,
        flags=re.MULTILINE,
    ), (
        f"{manifest.name}: plugin calls ensure_import() but does not declare "
        f"`# requires: PythonDepManager` so Stash won't install PDM "
        f"alongside it when installing via source URL."
    )
