"""Integration test for webp_to_jpeg invoked directly (host Python).

Verifies conversion *logic* against a real Stash: seed a WEBP cover,
invoke the plugin script as a subprocess (same way Stash does), assert
the cover becomes JPEG.

This test does NOT exercise PythonDepManager — host Python is assumed
to have Pillow + stashapi installed. The `test_via_runPluginTask`
module covers the full plugin-install path inside Stash.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SCRIPT = ROOT / "plugins" / "webp_to_jpeg" / "webp_to_jpeg.py"


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
    # Cover is JPEG now; plugin should find 0 WEBP on the next run.
    result = _run_plugin(sandbox.url)
    summary = result["output"]
    assert summary["webp_covers_found"] == 0
    assert summary["converted"] == 0
    assert summary["errors"] == 0
