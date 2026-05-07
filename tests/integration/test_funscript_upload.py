"""Integration test for funscript_upload.

Tests the full plugin path via runPluginTask inside a real Stash container:
1. Copy the plugin into the running Stash sandbox.
2. Install stashapi into the container (via pip) so the plugin can import it.
3. Submit a valid base64-encoded funscript for the seeded scene.
4. Wait for the task job to complete.
5. Assert the funscript file landed at the expected path on the container FS.
6. Trigger a metadata scan; poll until the scene shows interactive=true.

A second sub-test verifies that submitting again without overwrite=true is
rejected with a meaningful error.

Prerequisites
-------------
- Docker must be available (integration fixture skips if not).
- The session sandbox must have run its one-time setup (conftest.py handles
  this via the session-scoped `sandbox`, `client`, and `seeded_scene`
  fixtures).
"""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ID = "funscript_upload"
TASK_NAME = "Upload Funscript"
ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / PLUGIN_ID


# ---------------------------------------------------------------------------
# Minimal valid funscript
# ---------------------------------------------------------------------------


def _make_funscript_bytes() -> bytes:
    doc = {
        "version": "1.0",
        "inverted": False,
        "range": 100,
        "actions": [
            {"at": 0, "pos": 0},
            {"at": 250, "pos": 100},
            {"at": 500, "pos": 0},
            {"at": 750, "pos": 100},
            {"at": 1000, "pos": 0},
        ],
    }
    return json.dumps(doc).encode()


def _make_funscript_b64() -> str:
    return base64.b64encode(_make_funscript_bytes()).decode()


# ---------------------------------------------------------------------------
# GraphQL helpers (not on StashClient yet — add inline for isolation)
# ---------------------------------------------------------------------------


def _find_scene_interactive(client: Any, scene_id: str) -> bool:
    data = client.gql(
        """
        query FindScene($id: ID!) { findScene(id: $id) { interactive } }
        """,
        {"id": scene_id},
    )
    return bool((data.get("findScene") or {}).get("interactive"))


def _find_scene_files(client: Any, scene_id: str) -> list[dict[str, Any]]:
    data = client.gql(
        """
        query FindScene($id: ID!) {
          findScene(id: $id) {
            files { path basename parent_folder { path } }
          }
        }
        """,
        {"id": scene_id},
    )
    return (data.get("findScene") or {}).get("files") or []


# ---------------------------------------------------------------------------
# Plugin install fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def plugin_installed(sandbox, client):
    """Copy funscript_upload into the Stash container and pip-install stashapi."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plugin_host = tmp_path / PLUGIN_ID
        shutil.copytree(PLUGIN_DIR, plugin_host)

        sandbox.exec("mkdir", "-p", "/root/.stash/plugins")
        sandbox.copy_into(plugin_host, f"/root/.stash/plugins/{PLUGIN_ID}")

    # Install stashapi inside the container so the plugin script can import it.
    sandbox.exec(
        "python3",
        "-m",
        "pip",
        "install",
        "--quiet",
        "--break-system-packages",
        "stashapi>=0.1.5",
    )
    client.reload_plugins()
    return True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("plugin_installed")
def test_upload_funscript_creates_file_and_scene_becomes_interactive(
    client, sandbox, seeded_scene
):
    scene_id = seeded_scene["id"]

    # Determine expected target path from the scene's video file.
    files = _find_scene_files(client, scene_id)
    assert files, "seeded scene must have at least one file"
    video_path = Path(files[0]["path"])
    expected_target = video_path.with_suffix(".funscript")

    # The scene must not already be interactive before we start.
    # If it is (from a previous test run against a persistent volume), remove
    # the funscript file before continuing.
    if _find_scene_interactive(client, scene_id):
        sandbox.exec("rm", "-f", str(expected_target), check=False)

    # Run the plugin task.
    job_id = client.run_plugin_task(
        PLUGIN_ID,
        TASK_NAME,
        {
            "mode": "upload",
            "scene_id": str(scene_id),
            "payload_b64": _make_funscript_b64(),
            "overwrite": False,
        },
    )
    job = client.wait_for_plugin_task(job_id, timeout=120)
    assert job["status"] == "FINISHED", (
        f"upload task status={job['status']} error={job.get('error')}\n"
        f"sandbox logs:\n{sandbox.logs(tail=200)}"
    )
    assert not job.get("error"), (
        f"upload task reported error: {job.get('error')}\n"
        f"sandbox logs:\n{sandbox.logs(tail=200)}"
    )

    # Assert the file was written to the container filesystem.
    result = sandbox.exec(
        "test", "-f", str(expected_target), check=False
    )
    assert result.returncode == 0, (
        f"funscript file not found at {expected_target} in container\n"
        f"sandbox logs:\n{sandbox.logs(tail=200)}"
    )

    # Assert the content is valid JSON with the expected actions.
    cat_result = sandbox.exec("cat", str(expected_target))
    doc = json.loads(cat_result.stdout)
    assert "actions" in doc
    assert len(doc["actions"]) >= 1

    # Poll until the scene flips to interactive=true (scan must complete).
    scan_job_id = client.metadata_scan()
    client.wait_for_job(scan_job_id, timeout=60)

    deadline = time.monotonic() + 30.0
    interactive = False
    while time.monotonic() < deadline:
        interactive = _find_scene_interactive(client, scene_id)
        if interactive:
            break
        time.sleep(1.0)

    assert interactive, (
        "scene did not become interactive after scan;\n"
        f"sandbox logs:\n{sandbox.logs(tail=200)}"
    )


@pytest.mark.usefixtures("plugin_installed")
def test_upload_without_overwrite_flag_rejected_when_file_exists(
    client, sandbox, seeded_scene
):
    """Second upload without overwrite=true must fail with EXISTS code."""
    scene_id = seeded_scene["id"]

    # The previous test left the funscript in place — so this should be
    # rejected.  If somehow it was cleaned up, write a stub file first.
    files = _find_scene_files(client, scene_id)
    if not files:
        pytest.skip("scene has no files — cannot determine target path")
    video_path = Path(files[0]["path"])
    expected_target = video_path.with_suffix(".funscript")

    exists_result = sandbox.exec("test", "-f", str(expected_target), check=False)
    if exists_result.returncode != 0:
        # Write a stub so we can test the rejection.
        sandbox.exec(
            "sh",
            "-c",
            f'echo \'{{"actions":[{{"at":0,"pos":0}}]}}\' > {expected_target}',
        )

    job_id = client.run_plugin_task(
        PLUGIN_ID,
        TASK_NAME,
        {
            "mode": "upload",
            "scene_id": str(scene_id),
            "payload_b64": _make_funscript_b64(),
            "overwrite": False,
        },
    )
    job = client.wait_for_plugin_task(job_id, timeout=120)

    # The job should complete (FINISHED or FAILED) — the plugin exits non-zero,
    # which different Stash versions surface differently:
    #   - older: FAILED with an "error" field
    #   - newer: FINISHED with error: null (exit code not propagated to job status)
    assert job["status"] in {"FINISHED", "FAILED"}, f"unexpected status: {job}"

    error_text = job.get("error") or ""
    if error_text:
        # Older Stash: job error field is populated — validate the code.
        assert "EXISTS" in error_text or "already exists" in error_text.lower(), (
            f"unexpected error text: {error_text}"
        )
    else:
        # Newer Stash surfaces the rejection as FINISHED with error=null.
        # The authoritative check is that the file on disk was NOT overwritten.
        # The stub has 1 action; the upload payload has 5 — if the content is
        # still 1 action the plugin correctly declined to replace it.
        cat_result = sandbox.exec("cat", str(expected_target))
        on_disk = json.loads(cat_result.stdout)
        assert len(on_disk.get("actions", [])) != len(
            json.loads(_make_funscript_bytes()).get("actions", [])
        ), (
            "funscript was silently overwritten when overwrite=false — plugin "
            "did not reject the upload"
        )
