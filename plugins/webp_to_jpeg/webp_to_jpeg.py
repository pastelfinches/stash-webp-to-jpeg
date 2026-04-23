"""WEBP to JPEG cover converter — Stash plugin task.

Scans every scene, downloads its cover image via Stash's HTTP endpoint,
detects WEBP via magic bytes, re-encodes to JPEG with Pillow, and uploads
the new bytes via the sceneUpdate mutation (cover_image data URL).
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import urllib.error
import urllib.request
from typing import Any, NoReturn


def _emit_fatal(message: str) -> NoReturn:
    """Emit a fatal error using Stash's log framing and PluginOutput schema.

    Stash's task UI reports plugins as successful when the process exits
    with code 1 but produces no stdout JSON — emit a proper PluginOutput
    with a non-null error so the failure is visible.
    """
    sys.stderr.write(f"\x01e\x02{message}\n")
    sys.stderr.flush()
    with contextlib.suppress(Exception):
        print(json.dumps({"output": None, "error": message}), flush=True)
    sys.exit(1)


def _pip_install(
    python_cmd: str, packages: list[str]
) -> None:
    """Install `packages` into `python_cmd`'s environment.

    Some distributions (hotio/stash, Docker images that build venvs with
    `--without-pip`) ship a Python that has no `pip` module. Bootstrap
    via `ensurepip` before attempting the install.
    """
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


def _install_deps_via_pip() -> None:
    _pip_install(sys.executable, ["Pillow>=10.0.0", "stashapi>=0.1.5"])


def _deps_already_importable() -> bool:
    try:
        import PIL  # noqa: F401
        import stashapi  # noqa: F401

        return True
    except ImportError:
        return False


def _ensure_deps() -> None:
    """Make Pillow + stashapi importable, installing them if needed.

    Order of preference:
    1. Already importable → nothing to do.
    2. PythonDepManager (isolated, preferred when available).
    3. Direct `pip install` — for the official Alpine stashapp/stash
       image, which doesn't ship git (PDM requires git unconditionally).
    """
    if _deps_already_importable():
        return
    try:
        from PythonDepManager import ensure_import  # type: ignore
    except ImportError:
        ensure_import = None
    if ensure_import is not None:
        try:
            # Pillow's pip name differs from its import name (PIL), so
            # declare both. Without this, PDM "installs" the package but
            # the subsequent `from PIL import Image` still fails.
            ensure_import("PIL:Pillow>=10.0.0", "stashapi>=0.1.5")
            if _deps_already_importable():
                return
        except Exception:  # noqa: BLE001 — fall through to pip
            pass
    _install_deps_via_pip()


try:
    _ensure_deps()
except Exception as e:  # noqa: BLE001
    _emit_fatal(f"Failed to install dependencies: {e}")

try:
    import stashapi.log as log
    from PIL import Image
    from stashapi.stashapp import StashInterface
except ImportError as e:
    _emit_fatal(
        f"Missing dependency: {e.name}. Install the PythonDepManager plugin "
        "from the stashapp/CommunityScripts source, or `pip install -r "
        "requirements.txt` in the plugin directory."
    )


PLUGIN_ID = "webp_to_jpeg"
WEBP_RIFF = b"RIFF"
WEBP_FMT = b"WEBP"


def is_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == WEBP_RIFF and data[8:12] == WEBP_FMT


def build_cover_url(conn: dict[str, Any], scene_id: str) -> str:
    scheme = conn.get("Scheme", "http")
    host = conn.get("Host", "localhost")
    port = conn.get("Port", 9999)
    return f"{scheme}://{host}:{port}/scene/{scene_id}/screenshot"


def auth_headers(conn: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    api_key = conn.get("ApiKey")
    if api_key:
        headers["ApiKey"] = api_key
    cookie = conn.get("SessionCookie") or {}
    if isinstance(cookie, dict):
        name = cookie.get("Name") or "session"
        value = cookie.get("Value")
        if value:
            headers["Cookie"] = f"{name}={value}"
    return headers


def fetch_cover(url: str, headers: dict[str, str], timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        log.warning(f"HTTP {e.code} fetching {url}")
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
    return None


def webp_bytes_to_jpeg_data_url(webp: bytes, quality: int) -> str:
    with Image.open(io.BytesIO(webp)) as img:
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def load_settings(stash: StashInterface) -> dict[str, Any]:
    defaults = {"dryRun": False, "jpegQuality": 92, "workers": 8}
    try:
        config = stash.get_configuration()
    except Exception as e:
        log.warning(f"Could not load configuration: {e}. Using defaults.")
        return defaults
    plugins = (config or {}).get("plugins") or {}
    user = plugins.get(PLUGIN_ID) or {}
    merged = {**defaults, **user}
    # Coerce types in case Stash serializes them as strings.
    merged["dryRun"] = bool(merged.get("dryRun", False))
    # Stash's NUMBER setting renders null as 0 in the UI even when the user
    # has never touched it — treat 0 / missing / out-of-range as "use default".
    try:
        q = int(merged.get("jpegQuality") or 0)
    except (TypeError, ValueError):
        q = 0
    if q < 1 or q > 100:
        q = 92
    merged["jpegQuality"] = q
    try:
        w = int(merged.get("workers") or 0)
    except (TypeError, ValueError):
        w = 0
    if w < 1 or w > 64:
        w = 8
    merged["workers"] = w
    return merged


def fetch_all_scene_ids(stash: StashInterface) -> list[str]:
    query = """
    query FindScenes($filter: FindFilterType) {
      findScenes(filter: $filter) {
        count
        scenes { id }
      }
    }
    """
    page = 1
    per_page = 500
    ids: list[str] = []
    while True:
        data = stash.call_GQL(
            query,
            {"filter": {"per_page": per_page, "page": page, "sort": "id", "direction": "ASC"}},
        )
        batch = data["findScenes"]["scenes"]
        ids.extend(s["id"] for s in batch)
        if len(batch) < per_page:
            break
        page += 1
    return ids


def _process_scene(
    sid: str,
    conn: dict[str, Any],
    headers: dict[str, str],
    stash: StashInterface,
    quality: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Process a single scene. Safe to call from worker threads.

    Returns {"scene_id", "status": fetch_failed|not_webp|would_convert|
             converted|convert_failed|update_failed, "bytes": int|None}.
    PIL and requests both release the GIL during network / C image work,
    so threading gives real parallelism on multi-core boxes.
    """
    url = build_cover_url(conn, sid)
    data = fetch_cover(url, headers)
    if not data:
        return {"scene_id": sid, "status": "fetch_failed", "bytes": None}
    if not is_webp(data):
        return {"scene_id": sid, "status": "not_webp", "bytes": len(data)}
    if dry_run:
        return {"scene_id": sid, "status": "would_convert", "bytes": len(data)}
    try:
        data_url = webp_bytes_to_jpeg_data_url(data, quality)
    except Exception as e:
        log.warning(f"Scene {sid}: conversion failed: {e}")
        return {"scene_id": sid, "status": "convert_failed", "bytes": len(data)}
    try:
        stash.update_scene({"id": sid, "cover_image": data_url})
    except Exception as e:
        log.warning(f"Scene {sid}: update_scene failed: {e}")
        return {"scene_id": sid, "status": "update_failed", "bytes": len(data)}
    return {"scene_id": sid, "status": "converted", "bytes": len(data)}


def run_conversion(
    stash: StashInterface, conn: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    dry_run = settings["dryRun"]
    quality = settings["jpegQuality"]
    workers = settings["workers"]

    if dry_run:
        log.info("Dry run mode — no changes will be written.")

    log.info("Enumerating scenes...")
    scene_ids = fetch_all_scene_ids(stash)
    total = len(scene_ids)
    log.info(f"Found {total} scenes to scan for WEBP covers ({workers} workers).")

    headers = auth_headers(conn)
    tallies = {
        "fetch_failed": 0,
        "not_webp": 0,
        "would_convert": 0,
        "converted": 0,
        "convert_failed": 0,
        "update_failed": 0,
    }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _process_scene, sid, conn, headers, stash, quality, dry_run
            )
            for sid in scene_ids
        ]
        completed = 0
        for fut in as_completed(futures):
            result = fut.result()
            tallies[result["status"]] = tallies.get(result["status"], 0) + 1
            completed += 1
            if total:
                log.progress(completed / total)
            if result["status"] == "converted":
                log.info(
                    f"Scene {result['scene_id']}: converted WEBP -> JPEG "
                    f"({result['bytes']} bytes)"
                )
            elif result["status"] == "would_convert":
                log.debug(
                    f"Scene {result['scene_id']}: would convert "
                    f"({result['bytes']} bytes)"
                )

    log.progress(1.0)

    webp_found = (
        tallies["would_convert"]
        + tallies["converted"]
        + tallies["convert_failed"]
        + tallies["update_failed"]
    )
    converted = tallies["converted"]
    skipped = tallies["not_webp"]
    errors = (
        tallies["fetch_failed"]
        + tallies["convert_failed"]
        + tallies["update_failed"]
    )

    summary = {
        "total_scenes": total,
        "webp_covers_found": webp_found,
        "converted": converted,
        "non_webp_skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
    }
    log.info(f"Done: {summary}")
    return summary


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
        print(json.dumps({"output": None, "error": "invalid json"}))
        sys.exit(1)

    conn = plugin_input.get("server_connection") or {}
    args = plugin_input.get("args") or {}
    mode = args.get("mode", "convert")

    stash = StashInterface(conn)
    settings = load_settings(stash)

    if mode == "convert":
        try:
            summary = run_conversion(stash, conn, settings)
            print(json.dumps({"output": summary}))
        except Exception as e:
            log.error(f"Task failed: {e}")
            print(json.dumps({"output": None, "error": str(e)}))
            sys.exit(1)
    else:
        log.error(f"Unknown mode: {mode}")
        print(json.dumps({"output": None, "error": f"unknown mode: {mode}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
