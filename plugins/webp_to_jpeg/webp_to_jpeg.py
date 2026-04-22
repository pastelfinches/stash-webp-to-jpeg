"""WEBP to JPEG cover converter — Stash plugin task.

Scans every scene, downloads its cover image via Stash's HTTP endpoint,
detects WEBP via magic bytes, re-encodes to JPEG with Pillow, and uploads
the new bytes via the sceneUpdate mutation (cover_image data URL).
"""

from __future__ import annotations

import base64
import io
import json
import sys
import urllib.error
import urllib.request
from typing import Any

try:
    import stashapi.log as log
    from stashapi.stashapp import StashInterface
except ImportError:
    sys.stderr.write(
        "Missing dependency: stashapi. Install with: pip install stashapi\n"
    )
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    sys.stderr.write(
        "Missing dependency: Pillow. Install with: pip install Pillow\n"
    )
    sys.exit(1)


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
    defaults = {"dryRun": False, "jpegQuality": 92}
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
    try:
        merged["jpegQuality"] = int(merged.get("jpegQuality", 92))
    except (TypeError, ValueError):
        merged["jpegQuality"] = 92
    merged["jpegQuality"] = max(1, min(100, merged["jpegQuality"]))
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


def run_conversion(stash: StashInterface, conn: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    dry_run = settings["dryRun"]
    quality = settings["jpegQuality"]

    if dry_run:
        log.info("Dry run mode — no changes will be written.")

    log.info("Enumerating scenes...")
    scene_ids = fetch_all_scene_ids(stash)
    total = len(scene_ids)
    log.info(f"Found {total} scenes to scan for WEBP covers.")

    headers = auth_headers(conn)
    webp_found = 0
    converted = 0
    skipped = 0
    errors = 0

    for i, sid in enumerate(scene_ids):
        if total:
            log.progress(i / total)
        url = build_cover_url(conn, sid)
        data = fetch_cover(url, headers)
        if not data:
            errors += 1
            continue
        if not is_webp(data):
            skipped += 1
            continue

        webp_found += 1
        log.debug(f"Scene {sid}: WEBP cover detected ({len(data)} bytes)")

        if dry_run:
            continue

        try:
            data_url = webp_bytes_to_jpeg_data_url(data, quality)
        except Exception as e:
            log.warning(f"Scene {sid}: conversion failed: {e}")
            errors += 1
            continue

        try:
            stash.update_scene({"id": sid, "cover_image": data_url})
            converted += 1
            log.info(f"Scene {sid}: converted WEBP -> JPEG")
        except Exception as e:
            log.warning(f"Scene {sid}: update_scene failed: {e}")
            errors += 1

    log.progress(1.0)

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
