"""Target test for the pip-install fallback, including ensurepip bootstrap.

Regression coverage for the hotio/stash case where Stash's Python venv
ships without pip. The plugin is expected to detect this and call
`python -m ensurepip` before running `pip install`.

Builds a throwaway venv with `python -m venv --without-pip`, points
`_pip_install()` at it, and asserts a real package ends up importable
in that venv afterward.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import webp_to_jpeg as w2j


def _python_with_ensurepip() -> str | None:
    """Find a Python interpreter that has ensurepip available.

    Some minimal test environments (uv venvs built from packaged Pythons,
    Debian images without `python3-venv`) strip ensurepip. Skip the test
    cleanly if none of the obvious candidates has it.
    """
    candidates = [
        sys.executable,
        "/usr/bin/python3",
        "/usr/local/bin/python3",
        "/home/linuxbrew/.linuxbrew/bin/python3",
    ]
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        probe = subprocess.run(
            [path, "-c", "import ensurepip, venv"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return path
    return None


@pytest.fixture
def pipless_venv(tmp_path):
    base_python = _python_with_ensurepip()
    if base_python is None:
        pytest.skip("no python with ensurepip available to build a test venv")

    venv_dir = tmp_path / "venv"
    subprocess.check_call(
        [base_python, "-m", "venv", "--without-pip", str(venv_dir)]
    )
    python = venv_dir / "bin" / "python"
    if not python.exists():
        python = venv_dir / "Scripts" / "python.exe"
    assert python.exists(), f"venv python not found at {python}"
    probe = subprocess.run(
        [str(python), "-c", "import pip"], capture_output=True
    )
    assert probe.returncode != 0, "precondition failed: pip is already in venv"
    return str(python)


def test_pip_install_bootstraps_ensurepip_then_installs(pipless_venv):
    # `six` is tiny, pure-python, and has no deps — safe to install in a
    # test environment without network latency or native-build flakiness.
    w2j._pip_install(pipless_venv, ["six"])

    verify = subprocess.run(
        [pipless_venv, "-c", "import pip, six; print(pip.__version__, six.__version__)"],
        capture_output=True,
        text=True,
    )
    assert verify.returncode == 0, (
        f"pip or six not importable after _pip_install: {verify.stderr}"
    )
    # Basic sanity: both versions printed on a single line.
    pip_v, six_v = verify.stdout.strip().split()
    assert pip_v and six_v
