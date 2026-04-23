"""VR Preview Flattener — Stash plugin task.

For each scene tagged as VR or AR, regenerate the animated preview (mp4 +
webp) by cropping one stereo eye and (optionally) unwrapping the input
projection with ffmpeg's v360 filter, so the hover-preview is watchable in
a normal browser.

Strategy is driven by StashDB tags on the scene:
  - Virtual Reality / Augmented Reality   — enters the processing set
  - Top-Bottom                            — stereo layout is OU (default SBS)
  - Fisheye                               — input is fisheye (default equirect)
  - 180° / 190° / 200° / 360°             — input FOV (default 180°)

Manual task only — Stash's own "Generate" task will overwrite the flattened
preview from the source the next time it runs. Re-run this task afterwards.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn


def _emit_fatal(message: str) -> NoReturn:
    sys.stderr.write(f"\x01e\x02{message}\n")
    sys.stderr.flush()
    with contextlib.suppress(Exception):
        print(json.dumps({"output": None, "error": message}), flush=True)
    sys.exit(1)


def _pip_install(python_cmd: str, packages: list[str]) -> None:
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
        except Exception:  # noqa: BLE001 — fall through to pip
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
        "or `pip install -r requirements.txt` in the plugin directory."
    )


PLUGIN_ID = "vr_preview_flatten"
MARKER_SUFFIX = ".vr_flat"  # sidecar file next to a processed preview

DEFAULTS: dict[str, Any] = {
    "dryRun": False,
    "reprocess": False,
    "limit": 0,
    "workers": 2,
    "virtualRealityTag": "Virtual Reality",
    "augmentedRealityTag": "Augmented Reality",
    "fisheyeTag": "Fisheye",
    "topBottomTag": "Top-Bottom",
    "fov180Tag": "180°",
    "fov190Tag": "190°",
    "fov200Tag": "200°",
    "fov360Tag": "360°",
    "defaultFov": 180,
    "defaultProjection": "equirect",  # equirect | fisheye | flat
    "outputHFov": 90,
    "outputVFov": 90,
    "ffmpegBin": "ffmpeg",
    "ffprobeBin": "ffprobe",
    "crf": 23,
}


def load_settings(stash: StashInterface) -> dict[str, Any]:
    """Load plugin settings, coercing types and substituting defaults for 0/blank."""
    try:
        config = stash.get_configuration()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not load configuration: {e}. Using defaults.")
        return dict(DEFAULTS)
    user = ((config or {}).get("plugins") or {}).get(PLUGIN_ID) or {}
    merged: dict[str, Any] = {**DEFAULTS, **user}

    merged["dryRun"] = bool(merged.get("dryRun", False))
    merged["reprocess"] = bool(merged.get("reprocess", False))

    # limit is "0 means no limit". Everything else is a positive scene count.
    try:
        lim = int(merged.get("limit") or 0)
    except (TypeError, ValueError):
        lim = 0
    merged["limit"] = max(0, lim)

    # NUMBER settings: treat 0/missing/out-of-range as "use default".
    for key, lo, hi in (
        ("workers", 1, 32),
        ("defaultFov", 90, 360),
        ("outputHFov", 30, 180),
        ("outputVFov", 30, 180),
        ("crf", 1, 51),
    ):
        try:
            v = int(merged.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v < lo or v > hi:
            v = int(DEFAULTS[key])
        merged[key] = v

    # STRING settings: blank → default.
    for key in (
        "virtualRealityTag",
        "fisheyeTag",
        "topBottomTag",
        "fov180Tag",
        "fov190Tag",
        "fov200Tag",
        "fov360Tag",
        "ffmpegBin",
        "ffprobeBin",
        "defaultProjection",
    ):
        v = str(merged.get(key) or "").strip()
        merged[key] = v or str(DEFAULTS[key])

    # augmentedRealityTag blank is legitimate: "don't process AR scenes".
    merged["augmentedRealityTag"] = str(merged.get("augmentedRealityTag") or "").strip()

    proj = merged["defaultProjection"].lower()
    if proj not in {"equirect", "fisheye", "flat"}:
        log.warning(f"defaultProjection '{proj}' invalid; using 'equirect'.")
        proj = "equirect"
    merged["defaultProjection"] = proj

    return merged


def resolve_tag_id(stash: StashInterface, name: str) -> str | None:
    """Look up a single tag ID by exact name; returns None if no such tag."""
    if not name:
        return None
    try:
        tag = stash.find_tag(name)
    except Exception as e:  # noqa: BLE001
        log.warning(f"find_tag({name!r}) failed: {e}")
        return None
    if not tag:
        return None
    return str(tag.get("id"))


def get_generated_path(stash: StashInterface) -> str:
    """Resolve Stash's generated/ directory (preview files live under here)."""
    cfg = stash.get_configuration() or {}
    gp = ((cfg.get("general") or {}).get("generatedPath")) or ""
    if not gp:
        raise RuntimeError("Stash configuration.general.generatedPath is empty")
    return gp


def find_scenes_for_tags(
    stash: StashInterface, tag_ids: list[str]
) -> list[dict[str, Any]]:
    """Return all scenes tagged with any of the given tag IDs.

    Uses INCLUDES (OR across the tag list). Paginates until exhausted. The
    fields pulled here are the minimum needed to (a) find the generated
    preview file on disk and (b) decide the ffmpeg strategy from tags.
    """
    if not tag_ids:
        return []
    query = """
    query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {
      findScenes(filter: $filter, scene_filter: $scene_filter) {
        count
        scenes {
          id
          title
          oshash
          checksum
          files { fingerprints { type value } }
          tags { id name }
        }
      }
    }
    """
    scene_filter = {"tags": {"value": tag_ids, "modifier": "INCLUDES"}}
    page = 1
    per_page = 200
    out: list[dict[str, Any]] = []
    while True:
        data = stash.call_GQL(
            query,
            {
                "filter": {
                    "per_page": per_page,
                    "page": page,
                    "sort": "id",
                    "direction": "ASC",
                },
                "scene_filter": scene_filter,
            },
        )
        batch = data["findScenes"]["scenes"]
        out.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return out


def scene_checksums(scene: dict[str, Any]) -> list[str]:
    """Every fingerprint value on the scene, plus top-level oshash/checksum.

    Stash's preview filenames are `<checksum>.mp4` / `.webp` where `checksum`
    is whichever hash was chosen when the preview was generated. The field
    varies across setups (oshash vs MD5 vs phash-based), so we try all of
    them and stat the filesystem to find the match.
    """
    values: list[str] = []
    seen: set[str] = set()
    for f in scene.get("files") or []:
        for fp in f.get("fingerprints") or []:
            v = fp.get("value")
            if v and v not in seen:
                seen.add(v)
                values.append(v)
    for key in ("oshash", "checksum"):
        v = scene.get(key)
        if v and v not in seen:
            seen.add(v)
            values.append(v)
    return values


def find_preview_files(
    scene: dict[str, Any], generated_path: str
) -> tuple[Path | None, Path | None]:
    """Locate the on-disk animated preview (mp4) and webp preview for a scene.

    Returns (mp4_path_or_none, webp_path_or_none).
    """
    gp = Path(generated_path)
    mp4_path: Path | None = None
    webp_path: Path | None = None
    for c in scene_checksums(scene):
        if mp4_path is None:
            cand = gp / f"{c}.mp4"
            if cand.exists():
                mp4_path = cand
        if webp_path is None:
            cand = gp / f"{c}.webp"
            if cand.exists():
                webp_path = cand
        if mp4_path and webp_path:
            break
    return mp4_path, webp_path


# ---------- ffmpeg filter graph -------------------------------------------------


def _fov_from_tags(tags: set[str], settings: dict[str, Any]) -> int:
    for tag_key, fov in (
        ("fov360Tag", 360),
        ("fov200Tag", 200),
        ("fov190Tag", 190),
        ("fov180Tag", 180),
    ):
        tag_name = settings[tag_key]
        if tag_name and tag_name in tags:
            return fov
    return int(settings["defaultFov"])


def _projection_from_tags(tags: set[str], settings: dict[str, Any]) -> str:
    if settings["fisheyeTag"] and settings["fisheyeTag"] in tags:
        return "fisheye"
    return settings["defaultProjection"]


def _layout_from_tags(tags: set[str], settings: dict[str, Any]) -> str:
    if settings["topBottomTag"] and settings["topBottomTag"] in tags:
        return "ou"
    return "sbs"


def build_filter_graph(tags: set[str], settings: dict[str, Any]) -> str:
    """Compose the -vf filter graph for this scene's tags.

    Stage 1 (always): crop to one stereo eye.
        SBS → left half; OU → top half.

    Stage 2 (conditional): reproject from the input projection into a flat,
        rectilinear view. Skipped when projection='flat' (the scene is
        already rectilinear stereo — just cropping is enough).

    For a 360° scene we use v360=equirect (the full sphere); for 180°/190°/
    200° we use v360=hequirect (half-equirectangular), with iv_fov/ih_fov
    set to the scene's actual FOV. Fisheye uses v360=fisheye.
    """
    layout = _layout_from_tags(tags, settings)
    projection = _projection_from_tags(tags, settings)
    fov = _fov_from_tags(tags, settings)
    out_h = int(settings["outputHFov"])
    out_v = int(settings["outputVFov"])

    crop = "crop=iw/2:ih:0:0" if layout == "sbs" else "crop=iw:ih/2:0:0"

    if projection == "flat":
        return crop

    if projection == "fisheye":
        reproject = (
            f"v360=fisheye:flat:ih_fov={fov}:iv_fov={fov}"
            f":h_fov={out_h}:v_fov={out_v}"
        )
    elif fov >= 360:
        reproject = f"v360=equirect:flat:h_fov={out_h}:v_fov={out_v}"
    else:
        reproject = (
            f"v360=hequirect:flat:ih_fov={fov}:iv_fov={fov}"
            f":h_fov={out_h}:v_fov={out_v}"
        )

    return f"{crop},{reproject}"


# ---------- ffmpeg execution ---------------------------------------------------


def is_marked(preview_path: Path) -> bool:
    return (preview_path.parent / f"{preview_path.name}{MARKER_SUFFIX}").exists()


def write_marker(preview_path: Path) -> None:
    (preview_path.parent / f"{preview_path.name}{MARKER_SUFFIX}").touch()


def clear_marker(preview_path: Path) -> None:
    marker = preview_path.parent / f"{preview_path.name}{MARKER_SUFFIX}"
    with contextlib.suppress(FileNotFoundError):
        marker.unlink()


def _run_ffmpeg(cmd: list[str]) -> tuple[int, str]:
    """Run ffmpeg silently; return (returncode, last-chunk-of-stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    # ffmpeg prints everything useful on stderr; keep the tail for log messages.
    tail = (proc.stderr or "").strip().splitlines()[-5:]
    return proc.returncode, "\n".join(tail)


def flatten_mp4(
    src: Path, vf: str, settings: dict[str, Any]
) -> tuple[bool, str]:
    """Re-encode the animated mp4 preview with the filter graph applied."""
    tmp = src.with_suffix(src.suffix + ".tmp.mp4")
    cmd = [
        settings["ffmpegBin"],
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-an",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(settings["crf"]),
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    rc, err = _run_ffmpeg(cmd)
    if rc != 0:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        return False, err or f"ffmpeg exit {rc}"
    os.replace(tmp, src)
    return True, ""


def flatten_webp(
    src: Path, vf: str, settings: dict[str, Any]
) -> tuple[bool, str]:
    """Re-encode the animated webp preview with the filter graph applied.

    libwebp_anim is the animated-webp encoder. We keep the default frame
    rate and let the input's own timing ride through.
    """
    tmp = src.with_suffix(src.suffix + ".tmp.webp")
    cmd = [
        settings["ffmpegBin"],
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        vf,
        "-c:v",
        "libwebp_anim",
        "-loop",
        "0",
        "-compression_level",
        "6",
        "-quality",
        "60",
        str(tmp),
    ]
    rc, err = _run_ffmpeg(cmd)
    if rc != 0:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        return False, err or f"ffmpeg exit {rc}"
    os.replace(tmp, src)
    return True, ""


# ---------- per-scene orchestration --------------------------------------------


def _process_scene(
    scene: dict[str, Any],
    settings: dict[str, Any],
    generated_path: str,
) -> dict[str, Any]:
    """Process one scene. Safe for thread-pool execution."""
    sid = str(scene.get("id"))
    title = scene.get("title") or f"scene {sid}"
    tag_names = {t.get("name") for t in (scene.get("tags") or []) if t.get("name")}
    vf = build_filter_graph(tag_names, settings)

    mp4, webp = find_preview_files(scene, generated_path)

    result: dict[str, Any] = {
        "scene_id": sid,
        "title": title,
        "filter": vf,
        "mp4": None,
        "webp": None,
        "status": "no_preview",
        "errors": [],
    }

    if not mp4 and not webp:
        return result

    if settings["dryRun"]:
        result["status"] = "would_process"
        if mp4:
            result["mp4"] = {"path": str(mp4), "marked": is_marked(mp4)}
        if webp:
            result["webp"] = {"path": str(webp), "marked": is_marked(webp)}
        return result

    processed_any = False
    skipped_any = False

    if mp4:
        if is_marked(mp4) and not settings["reprocess"]:
            result["mp4"] = {"status": "skipped_marker"}
            skipped_any = True
        else:
            ok, err = flatten_mp4(mp4, vf, settings)
            if ok:
                write_marker(mp4)
                result["mp4"] = {"status": "flattened"}
                processed_any = True
            else:
                result["mp4"] = {"status": "failed"}
                result["errors"].append(f"mp4: {err}")

    if webp:
        if is_marked(webp) and not settings["reprocess"]:
            result["webp"] = {"status": "skipped_marker"}
            skipped_any = True
        else:
            ok, err = flatten_webp(webp, vf, settings)
            if ok:
                write_marker(webp)
                result["webp"] = {"status": "flattened"}
                processed_any = True
            else:
                result["webp"] = {"status": "failed"}
                result["errors"].append(f"webp: {err}")

    if result["errors"]:
        result["status"] = "partial" if processed_any else "failed"
    elif processed_any:
        result["status"] = "flattened"
    elif skipped_any:
        result["status"] = "skipped_marker"
    else:
        result["status"] = "no_preview"
    return result


def _ensure_ffmpeg(settings: dict[str, Any]) -> None:
    """Fail loudly at task start rather than per-scene if ffmpeg is missing."""
    try:
        subprocess.check_call(
            [settings["ffmpegBin"], "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise RuntimeError(
            f"ffmpeg binary {settings['ffmpegBin']!r} not runnable: {e}"
        ) from e


def run_flatten(
    stash: StashInterface, settings: dict[str, Any]
) -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if settings["dryRun"]:
        log.info("Dry run — no files will be modified.")
    else:
        _ensure_ffmpeg(settings)

    tag_ids: list[str] = []
    for key in ("virtualRealityTag", "augmentedRealityTag"):
        name = settings[key]
        if not name:
            continue
        tid = resolve_tag_id(stash, name)
        if tid:
            tag_ids.append(tid)
            log.info(f"Matched tag {name!r} → id {tid}")
        else:
            log.warning(f"Tag {name!r} not found in Stash — skipping")

    if not tag_ids:
        raise RuntimeError(
            "No trigger tags resolved; set virtualRealityTag / "
            "augmentedRealityTag in plugin settings to tag names that exist."
        )

    generated_path = get_generated_path(stash)
    log.info(f"Stash generated path: {generated_path}")

    scenes = find_scenes_for_tags(stash, tag_ids)
    total_matched = len(scenes)
    limit = int(settings.get("limit") or 0)
    if limit > 0 and total_matched > limit:
        scenes = scenes[:limit]
        log.info(
            f"Found {total_matched} VR/AR scene(s); capped at limit={limit}."
        )
    else:
        log.info(f"Found {total_matched} VR/AR scene(s) to evaluate.")
    total = len(scenes)

    tallies: dict[str, int] = {}
    sample_filters: dict[str, int] = {}  # filter string → count
    errors: list[str] = []

    workers = max(1, int(settings["workers"]))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_process_scene, sc, settings, generated_path)
            for sc in scenes
        ]
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            if total:
                log.progress(done / total)
            tallies[r["status"]] = tallies.get(r["status"], 0) + 1
            sample_filters[r["filter"]] = sample_filters.get(r["filter"], 0) + 1
            if r["status"] in {"flattened", "would_process", "partial"}:
                log.info(
                    f"Scene {r['scene_id']} [{r['title']}] {r['status']} "
                    f"vf='{r['filter']}'"
                )
            for e in r.get("errors") or []:
                errors.append(f"scene {r['scene_id']}: {e}")
                log.warning(f"Scene {r['scene_id']}: {e}")

    log.progress(1.0)

    log.info("Filter usage breakdown:")
    for vf, n in sorted(sample_filters.items(), key=lambda x: -x[1]):
        log.info(f"  {n:>4}× {vf}")

    summary = {
        "total_vr_ar_scenes_matched": total_matched,
        "processed_this_run": total,
        "limit_applied": limit,
        "status_counts": tallies,
        "errors": len(errors),
        "dry_run": settings["dryRun"],
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
    mode = args.get("mode", "flatten")

    stash = StashInterface(conn)
    settings = load_settings(stash)

    log.debug(f"settings: {json.dumps({k: v for k, v in settings.items()})}")
    log.debug(f"argv: {shlex.join(sys.argv)}")

    if mode != "flatten":
        log.error(f"Unknown mode: {mode}")
        print(json.dumps({"output": None, "error": f"unknown mode: {mode}"}))
        sys.exit(1)

    try:
        summary = run_flatten(stash, settings)
        print(json.dumps({"output": summary}))
    except Exception as e:  # noqa: BLE001
        log.error(f"Task failed: {e}")
        print(json.dumps({"output": None, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
