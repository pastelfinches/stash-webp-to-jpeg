"""Funscript Upload — Stash plugin task.

Receives a base64-encoded funscript from the Stash UI, validates it,
writes it atomically next to the scene video with the correct basename,
and triggers a metadata scan so Stash detects the interactive file and
flips the scene to interactive=true.

Security model
--------------
- The destination filename is derived ONLY from the scene's video
  basename (via GraphQL), never from the user-supplied upload filename.
- A path-traversal check confirms the computed destination sits inside
  the same directory as the video file.
- Overwrite is only allowed when the UI explicitly passes overwrite=true
  (set after the user has confirmed in the confirm-replace dialog).
- The file is written to a `.tmp` sibling first, then atomically renamed
  via os.replace(), so a partial upload cannot corrupt an existing script.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

# ---------------------------------------------------------------------------
# Fatal error helper — dual-protocol (JSON stdout + stderr)
# ---------------------------------------------------------------------------


def _emit_fatal(message: str) -> NoReturn:
    sys.stderr.write(f"\x01e\x02{message}\n")
    sys.stderr.flush()
    with contextlib.suppress(Exception):
        print(json.dumps({"output": None, "error": message}), flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Dependency bootstrap
# ---------------------------------------------------------------------------


def _pip_install(python_cmd: str, packages: list[str]) -> None:
    import subprocess

    try:
        subprocess.check_call(
            [python_cmd, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        subprocess.check_call(
            [python_cmd, "-m", "ensurepip", "--default-pip"],
            stdout=subprocess.DEVNULL,
        )
    subprocess.check_call(
        [
            python_cmd,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--break-system-packages",
            *packages,
        ]
    )


def _deps_already_importable() -> bool:
    try:
        import stashapi  # noqa: F401

        return True
    except ImportError:
        return False


def _ensure_deps() -> None:
    if _deps_already_importable():
        return
    try:
        from PythonDepManager import ensure_import  # type: ignore
    except ImportError:
        ensure_import = None
    if ensure_import is not None:
        try:
            ensure_import("stashapi>=0.1.5")
            if _deps_already_importable():
                return
        except Exception:  # noqa: BLE001
            pass
    _pip_install(sys.executable, ["stashapi>=0.1.5"])


try:
    _ensure_deps()
except Exception as e:  # noqa: BLE001
    _emit_fatal(f"Failed to install dependencies: {e}")

try:
    import stashapi.log as log
    from stashapi.stashapp import StashInterface
except ImportError as e:
    _emit_fatal(
        f"Missing dependency: {e.name}. Install the PythonDepManager plugin "
        "from the stashapp/CommunityScripts source, or `pip install -r "
        "requirements.txt` in the plugin directory."
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_ID = "funscript_upload"
DEFAULT_MAX_SIZE_MB = 20
MAX_SIZE_FLOOR_MB = 1
MAX_SIZE_CEIL_MB = 500


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def load_settings(stash: StashInterface) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "maxSizeMb": DEFAULT_MAX_SIZE_MB,
        "generateAfterUpload": False,
    }
    try:
        config = stash.get_configuration()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not load configuration: {e}. Using defaults.")
        return defaults
    plugins = (config or {}).get("plugins") or {}
    user = plugins.get(PLUGIN_ID) or {}
    merged = {**defaults, **user}
    try:
        mb = int(merged.get("maxSizeMb") or 0)
    except (TypeError, ValueError):
        mb = 0
    if mb < MAX_SIZE_FLOOR_MB or mb > MAX_SIZE_CEIL_MB:
        mb = DEFAULT_MAX_SIZE_MB
    merged["maxSizeMb"] = mb
    merged["generateAfterUpload"] = bool(merged.get("generateAfterUpload", False))
    return merged


# ---------------------------------------------------------------------------
# GraphQL helpers
# ---------------------------------------------------------------------------

_FIND_SCENE_QUERY = """
query FindScene($id: ID!) {
  findScene(id: $id) {
    id
    interactive
    files {
      path
      basename
      parent_folder {
        path
      }
    }
  }
}
"""

_METADATA_SCAN_MUTATION = """
mutation MetadataScan($input: ScanMetadataInput!) {
  metadataScan(input: $input)
}
"""

_METADATA_GENERATE_MUTATION = """
mutation MetadataGenerate($input: GenerateMetadataInput!) {
  metadataGenerate(input: $input)
}
"""


def find_scene(stash: StashInterface, scene_id: str) -> dict[str, Any] | None:
    data = stash.call_GQL(_FIND_SCENE_QUERY, {"id": scene_id})
    return (data or {}).get("findScene")


def trigger_scan(stash: StashInterface, directory: str) -> str | None:
    """Trigger a metadata scan of *directory* and return the job ID."""
    try:
        data = stash.call_GQL(
            _METADATA_SCAN_MUTATION,
            {"input": {"paths": [directory]}},
        )
        job_id = (data or {}).get("metadataScan")
        return str(job_id) if job_id is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning(f"Failed to trigger metadata scan: {e}")
        return None


def trigger_generate(stash: StashInterface, scene_id: str) -> str | None:
    """Trigger heatmap-only generation for *scene_id* and return the job ID.

    Sets only ``interactiveHeatmapsSpeeds: true``; all other generation
    options (covers, sprites, previews, phashes, transcodes, etc.) are
    explicitly set to false so nothing extra is regenerated.
    """
    input_vars: dict[str, Any] = {
        "sceneIDs": [scene_id],
        "covers": False,
        "sprites": False,
        "previews": False,
        "imagePreviews": False,
        "markers": False,
        "markerImagePreviews": False,
        "markerScreenshots": False,
        "transcodes": False,
        "forceTranscodes": False,
        "phashes": False,
        "interactiveHeatmapsSpeeds": True,
        "imagePhashes": False,
        "imageThumbnails": False,
        "clipPreviews": False,
    }
    try:
        data = stash.call_GQL(
            _METADATA_GENERATE_MUTATION,
            {"input": input_vars},
        )
        job_id = (data or {}).get("metadataGenerate")
        return str(job_id) if job_id is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning(f"Failed to trigger heatmap generation for scene {scene_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Funscript validation
# ---------------------------------------------------------------------------

# Maximum bytes we will accept before even attempting to parse.
# This is checked AFTER base64-decode, on the raw bytes.


def validate_funscript(payload: bytes, max_bytes: int) -> tuple[bool, str]:
    """Validate that *payload* looks like a well-formed funscript.

    Returns (ok, reason).  reason is non-empty only when ok is False.
    """
    if len(payload) > max_bytes:
        mb = len(payload) / (1024 * 1024)
        cap = max_bytes / (1024 * 1024)
        return False, f"funscript too large ({mb:.1f} MB > {cap:.0f} MB cap)"

    try:
        doc = json.loads(payload)
    except json.JSONDecodeError as e:
        return False, f"not valid JSON: {e}"

    if not isinstance(doc, dict):
        return False, "funscript must be a JSON object at the top level"

    actions = doc.get("actions")
    if actions is None:
        return False, "funscript is missing the 'actions' field"
    if not isinstance(actions, list):
        return False, "'actions' must be a JSON array"
    if len(actions) == 0:
        return False, "'actions' array is empty — not a valid funscript"

    # Spot-check: first entry should have 'at' (milliseconds) and 'pos' (0-100).
    first = actions[0]
    if not isinstance(first, dict):
        return False, "'actions' entries must be JSON objects"
    if "at" not in first or "pos" not in first:
        return False, "first action entry is missing 'at' or 'pos' key"

    return True, ""


# ---------------------------------------------------------------------------
# Path derivation + traversal check
# ---------------------------------------------------------------------------


def derive_target_path(scene: dict[str, Any]) -> tuple[Path | None, str]:
    """Compute the destination .funscript path from the scene's primary file.

    Returns (path, error_reason).  path is None on error.
    """
    files: list[dict[str, Any]] = scene.get("files") or []
    if not files:
        return None, "scene has no associated files"

    primary = files[0]
    video_path_str: str = primary.get("path") or ""
    if not video_path_str:
        return None, "scene primary file has no path"

    video_path = Path(video_path_str)
    # Derive target: same directory, same stem, .funscript extension.
    target = video_path.with_suffix(".funscript")

    # Traversal guard: target must be in the same directory as the video.
    # This is inherently satisfied because we use video_path.with_suffix(),
    # but we verify explicitly in case the video path itself is malformed.
    if target.parent != video_path.parent:
        return None, (
            f"computed target path {target} escapes the video directory "
            f"{video_path.parent} — refusing to write"
        )

    return target, ""


# ---------------------------------------------------------------------------
# Atomic file write
# ---------------------------------------------------------------------------


def atomic_write(target: Path, data: bytes) -> None:
    """Write *data* to *target* atomically via a temp sibling + rename."""
    tmp = target.with_suffix(".funscript.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except Exception:
        with contextlib.suppress(Exception):
            tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Upload task
# ---------------------------------------------------------------------------


def run_upload(
    stash: StashInterface,
    args: dict[str, Any],
    settings: dict[str, Any],
) -> None:
    """Execute the upload task.

    Expected args keys
    ------------------
    scene_id    : str  — Stash scene ID
    payload_b64 : str  — base64-encoded funscript bytes
    overwrite   : bool — caller confirmed replacing an existing script

    Whether to run a heatmap-only generate after the scan is controlled by
    the plugin setting ``generateAfterUpload`` (Settings → Plugins →
    Funscript Upload), not by a per-upload argument.
    """
    scene_id: str = str(args.get("scene_id") or "").strip()
    if not scene_id:
        log.error("upload task: scene_id is required")
        print(json.dumps({"output": None, "error": "scene_id is required"}))
        sys.exit(1)

    payload_b64: str = str(args.get("payload_b64") or "").strip()
    if not payload_b64:
        log.error("upload task: payload_b64 is required")
        print(json.dumps({"output": None, "error": "payload_b64 is required"}))
        sys.exit(1)

    # Stash's Map scalar may serialise booleans as JSON booleans *or* as the
    # string "true"/"false" depending on version.  Coerce defensively so that
    # the string "false" is not misread as True.
    _overwrite_raw = args.get("overwrite", False)
    overwrite: bool = (
        _overwrite_raw
        if isinstance(_overwrite_raw, bool)
        else str(_overwrite_raw).strip().lower() not in {"false", "0", "no", ""}
    )
    generate_after: bool = bool(settings.get("generateAfterUpload", False))

    # 1. Decode the base64 payload.
    try:
        payload = base64.b64decode(payload_b64)
    except Exception as e:
        err = f"could not base64-decode payload: {e}"
        log.error(err)
        print(json.dumps({"output": None, "error": err}))
        sys.exit(1)

    log.info(f"Scene {scene_id}: received {len(payload)} bytes after base64-decode.")

    # 2. Validate the funscript content.
    max_bytes = settings["maxSizeMb"] * 1024 * 1024
    ok, reason = validate_funscript(payload, max_bytes)
    if not ok:
        err = f"invalid funscript: {reason}"
        log.error(err)
        print(json.dumps({"output": None, "error": err}))
        sys.exit(1)

    log.info(f"Scene {scene_id}: funscript validation passed.")
    log.progress(0.25)

    # 3. Resolve the scene's primary file path via GraphQL.
    scene = find_scene(stash, scene_id)
    if scene is None:
        err = f"scene {scene_id} not found"
        log.error(err)
        print(json.dumps({"output": None, "error": err}))
        sys.exit(1)

    already_interactive: bool = bool(scene.get("interactive"))

    target, path_err = derive_target_path(scene)
    if target is None:
        log.error(path_err)
        print(json.dumps({"output": None, "error": path_err}))
        sys.exit(1)

    log.info(f"Scene {scene_id}: target path is {target}")
    log.progress(0.4)

    # 4. Check for an existing file.
    if target.exists() and not overwrite:
        err = (
            f"a funscript already exists at {target} "
            "— pass overwrite=true to replace it"
        )
        log.error(err)
        # Use a specific error code the UI can distinguish from a general failure.
        print(json.dumps({"output": None, "error": err, "code": "EXISTS"}))
        sys.exit(1)

    # 5. Atomic write.
    try:
        atomic_write(target, payload)
    except Exception as e:
        err = f"failed to write funscript to {target}: {e}"
        log.error(err)
        print(json.dumps({"output": None, "error": err}))
        sys.exit(1)

    log.info(f"Scene {scene_id}: wrote funscript to {target}")
    log.progress(0.7)

    # 6. Trigger a metadata scan so Stash detects the new file.
    directory = str(target.parent)
    job_id = trigger_scan(stash, directory)
    if job_id:
        log.info(
            f"Scene {scene_id}: metadata scan triggered (job {job_id}) on {directory}"
        )
    else:
        log.warning(
            f"Scene {scene_id}: could not trigger metadata scan — "
            "rescan the directory manually to enable interactive mode"
        )

    log.progress(0.85)

    # 7. Optionally trigger heatmap-only generate for the scene.
    generate_job_id: str | None = None
    if generate_after:
        generate_job_id = trigger_generate(stash, scene_id)
        if generate_job_id:
            log.info(
                f"Scene {scene_id}: heatmap generation triggered (job {generate_job_id})"
            )
        else:
            log.warning(
                f"Scene {scene_id}: could not trigger heatmap generation — "
                "run Generate from Settings → Tasks to generate the heatmap manually"
            )

    log.progress(1.0)

    result = {
        "scene_id": scene_id,
        "target": str(target),
        "was_interactive": already_interactive,
        "overwritten": target.exists() and overwrite,
        "scan_job": job_id,
        "generate_job": generate_job_id,
    }
    log.info(f"Scene {scene_id}: upload complete — {result}")
    print(json.dumps({"output": result}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        log.error("No input received on stdin.")
        print(json.dumps({"output": None, "error": "no stdin input"}))
        sys.exit(1)

    try:
        plugin_input = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Invalid JSON on stdin: {e}")
        print(json.dumps({"output": None, "error": "invalid json on stdin"}))
        sys.exit(1)

    conn = plugin_input.get("server_connection") or {}
    args = plugin_input.get("args") or {}
    mode = args.get("mode", "upload")

    stash = StashInterface(conn)
    settings = load_settings(stash)

    if mode == "upload":
        try:
            run_upload(stash, args, settings)
        except SystemExit:
            raise
        except Exception as e:
            log.error(f"Upload task failed: {e}")
            print(json.dumps({"output": None, "error": str(e)}))
            sys.exit(1)
    else:
        log.error(f"Unknown mode: {mode}")
        print(json.dumps({"output": None, "error": f"unknown mode: {mode}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
