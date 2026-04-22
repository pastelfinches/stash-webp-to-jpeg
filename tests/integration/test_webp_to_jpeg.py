"""End-to-end test for the WEBP→JPEG plugin against an ephemeral Stash.

Flow:
    1. Launch stashapp/stash via docker-compose (hermetic state)
    2. Point Stash's library at tests/sandbox/media/ and trigger a scan
    3. Pick up the scanned scene and seed it with a WEBP cover via sceneUpdate
    4. Invoke the plugin script directly (same way Stash would), feeding the
       PluginInput JSON on stdin
    5. Re-fetch the cover and assert it is now JPEG (magic bytes) and still
       visually matches the original WEBP (dominant color preserved)

The test is skipped when docker isn't available.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from tests.integration.sandbox import StashSandbox, StashSandboxError
from tests.integration.stash_client import StashClient

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SCRIPT = ROOT / "plugins" / "webp_to_jpeg" / "webp_to_jpeg.py"


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is required for integration tests"
)


@pytest.fixture(scope="module")
def sandbox():
    sb = StashSandbox()
    try:
        sb.start(timeout=180)
    except StashSandboxError as e:
        pytest.skip(f"sandbox unavailable: {e}")
    yield sb
    sb.stop()


@pytest.fixture(scope="module")
def client(sandbox):
    return StashClient(sandbox.url)


@pytest.fixture(scope="module")
def seeded_scene(client, sandbox):
    client.setup()
    client.set_library_path("/data")
    job = client.metadata_scan()
    client.wait_for_job(job, timeout=120)

    scenes = client.find_scenes()
    if not scenes:
        pytest.fail(
            "no scenes scanned; sandbox logs:\n" + sandbox.logs(tail=200)
        )
    return scenes[0]


def _make_webp(color=(210, 80, 40), size=(128, 128)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90)
    return buf.getvalue()


def _run_plugin(sandbox_url: str) -> dict:
    plugin_input = {
        "server_connection": {
            "Scheme": "http",
            "Host": sandbox_url.split("://", 1)[1].split(":")[0],
            "Port": int(sandbox_url.rsplit(":", 1)[1]),
        },
        "args": {"mode": "convert"},
    }
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_SCRIPT)],
        input=json.dumps(plugin_input),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert proc.returncode == 0, (
        f"plugin exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    stdout_lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert stdout_lines, f"plugin produced no stdout JSON. stderr:\n{proc.stderr}"
    return json.loads(stdout_lines[-1])


def test_converts_webp_cover_to_jpeg(client, sandbox, seeded_scene):
    scene_id = seeded_scene["id"]
    webp_bytes = _make_webp()
    client.set_cover_raw(scene_id, "image/webp", webp_bytes)

    before = client.fetch_cover(scene_id)
    assert before[:4] == b"RIFF" and before[8:12] == b"WEBP", "seed cover should be WEBP"

    result = _run_plugin(sandbox.url)
    assert result.get("error") is None, result
    summary = result["output"]
    assert summary["webp_covers_found"] >= 1
    assert summary["converted"] >= 1
    assert summary["errors"] == 0
    assert summary["dry_run"] is False

    after = client.fetch_cover(scene_id)
    assert after[:3] == b"\xff\xd8\xff", "post-conversion cover should be JPEG"

    # Dominant color should still be roughly red (cover content preserved).
    img = Image.open(io.BytesIO(after)).convert("RGB")
    r, g, b = img.resize((1, 1)).getpixel((0, 0))
    assert r > g and r > b, f"expected red-dominant cover, got rgb=({r},{g},{b})"


def test_second_run_is_noop(client, sandbox, seeded_scene):
    # After the first test, the cover is already JPEG — plugin should find 0 WEBP.
    result = _run_plugin(sandbox.url)
    summary = result["output"]
    assert summary["webp_covers_found"] == 0
    assert summary["converted"] == 0
    assert summary["errors"] == 0
