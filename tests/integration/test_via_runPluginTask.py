"""Full-stack integration test: plugin installed IN Stash, run via GraphQL.

This is the test that would have caught the `ensure_import("Pillow...")`
→ `from PIL import ...` mismatch: PythonDepManager believes the dependency
is satisfied, but the real import still fails. Running via subprocess on
the host can't catch that because the host has Pillow pre-installed in
the test venv.

Flow:
    1. Download PythonDepManager from CommunityScripts
    2. `docker cp` both PDM and webp_to_jpeg into the Stash container
    3. reloadPlugins()
    4. Seed a WEBP cover on the scanned scene
    5. runPluginTask("webp_to_jpeg", "Convert WEBP Covers to JPEG")
    6. Wait for the job to finish
    7. Assert the job did not fail AND the cover is now JPEG
"""

from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path

import pytest
from PIL import Image

PLUGIN_ID = "webp_to_jpeg"
TASK_NAME = "Convert WEBP Covers to JPEG"
ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / PLUGIN_ID


@pytest.fixture(scope="module")
def plugins_installed(sandbox, client):
    """Install PythonDepManager + our plugin into the running Stash."""
    with tempfile.TemporaryDirectory() as tmp:
        pdm_host = Path(tmp) / "PythonDepManager"
        sandbox.download_pythondepmanager(pdm_host)

        # Stash plugins dir may not exist yet — create it.
        sandbox.exec("mkdir", "-p", "/root/.stash/plugins")
        sandbox.copy_into(pdm_host, "/root/.stash/plugins/PythonDepManager")
        sandbox.copy_into(PLUGIN_DIR, f"/root/.stash/plugins/{PLUGIN_ID}")

    client.reload_plugins()
    return True


def _make_webp(color=(60, 180, 220), size=(128, 128)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90)
    return buf.getvalue()


def test_plugin_runs_inside_stash_and_converts_webp(
    client, sandbox, seeded_scene, plugins_installed
):
    scene_id = seeded_scene["id"]
    client.set_cover_raw(scene_id, "image/webp", _make_webp())

    before = client.fetch_cover(scene_id)
    assert before[:4] == b"RIFF" and before[8:12] == b"WEBP", (
        "seed cover should be WEBP; got: " + before[:16].hex()
    )

    job_id = client.run_plugin_task(PLUGIN_ID, TASK_NAME)
    job = client.wait_for_plugin_task(job_id, timeout=300)

    assert job["status"] == "FINISHED", (
        f"plugin task did not finish cleanly: {job}\n"
        f"sandbox logs:\n{sandbox.logs(tail=300)}"
    )
    assert not job.get("error"), (
        f"plugin task reported error: {job.get('error')}\n"
        f"sandbox logs:\n{sandbox.logs(tail=300)}"
    )

    after = client.fetch_cover(scene_id)
    assert after[:3] == b"\xff\xd8\xff", (
        "cover should be JPEG after the plugin runs inside Stash; "
        f"got magic bytes: {after[:16].hex()}\n"
        f"sandbox logs:\n{sandbox.logs(tail=300)}"
    )
