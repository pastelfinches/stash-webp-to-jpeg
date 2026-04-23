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
import shutil
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
# v2 marker: source-based flatten (v0.3+). Old ".vr_flat" markers from the
# preview-based v0.1/v0.2 releases are intentionally ignored so that an
# upgrading library re-flattens from source on the next run.
MARKER_SUFFIX = ".vr_flat_v2"

DEFAULTS: dict[str, Any] = {
    "dryRun": False,
    "reprocess": False,
    "limit": 0,
    # Source-based flattening runs ffmpeg against the raw 4K/8K source.
    # A single decoder can hold >1 GB of frame data; two concurrent workers
    # will OOM a default-sized Stash container (~4 GB memory limit). Keep
    # the default at 1 and let users raise it if they have headroom.
    "workers": 1,
    "ffmpegThreads": 4,
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
    "outputHFov": 120,
    "outputVFov": 90,
    "outputWidth": 960,
    "outputHeight": 720,
    # Preview pattern: N short segments spread across the source, stitched.
    # Zero = inherit from Stash's own configuration.general.preview* at runtime.
    "segments": 0,
    "segmentDuration": 0.0,
    "ffmpegBin": "ffmpeg",
    "ffprobeBin": "ffprobe",
    "crf": 18,
    "preset": "medium",
}

# Fallback preview-shape defaults when Stash config is unavailable.
# Matches Stash's own out-of-the-box defaults (12 × 0.75s = 9s).
_FALLBACK_SEGMENTS = 12
_FALLBACK_SEGMENT_DURATION = 0.75


def load_settings(stash: StashInterface) -> dict[str, Any]:
    """Load plugin settings, coercing types and substituting defaults for 0/blank."""
    try:
        config = stash.get_configuration()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not load configuration: {e}. Using defaults.")
        return dict(DEFAULTS)
    cfg = config or {}
    user = (cfg.get("plugins") or {}).get(PLUGIN_ID) or {}
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
        ("ffmpegThreads", 0, 32),
        ("defaultFov", 90, 360),
        ("outputHFov", 30, 180),
        ("outputVFov", 30, 180),
        ("outputWidth", 160, 3840),
        ("outputHeight", 90, 2160),
        ("crf", 1, 51),
    ):
        try:
            v = int(merged.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v < lo or v > hi:
            v = int(DEFAULTS[key])
        merged[key] = v

    # Segments / segmentDuration — inherit Stash's own preview config when
    # unset (0 sentinel), so our output shape tracks whatever the user has
    # chosen globally for Stash's preview generator.
    stash_general = cfg.get("general") or {}
    try:
        seg_n = int(merged.get("segments") or 0)
    except (TypeError, ValueError):
        seg_n = 0
    if seg_n <= 0:
        seg_n = int(stash_general.get("previewSegments") or _FALLBACK_SEGMENTS)
    merged["segments"] = max(1, min(seg_n, 64))

    try:
        seg_d = float(merged.get("segmentDuration") or 0.0)
    except (TypeError, ValueError):
        seg_d = 0.0
    if seg_d <= 0:
        seg_d = float(
            stash_general.get("previewSegmentDuration") or _FALLBACK_SEGMENT_DURATION
        )
    merged["segmentDuration"] = max(0.25, min(seg_d, 10.0))

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
        "preset",
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
    # Modern Stash (>=0.20) dropped top-level Scene.oshash/Scene.checksum —
    # fingerprints live only under files[].fingerprints now. Querying the
    # removed fields raises GRAPHQL_VALIDATION_FAILED rather than being
    # silently tolerated, which breaks the whole query.
    query = """
    query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {
      findScenes(filter: $filter, scene_filter: $scene_filter) {
        count
        scenes {
          id
          title
          files {
            path
            duration
            fingerprints { type value }
          }
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
        if not data or not data.get("findScenes"):
            # stashapi returns None when the server reports GraphQL errors,
            # which would otherwise crash below with a confusing NoneType
            # subscript error. Surface the real failure to the task log.
            raise RuntimeError(
                "findScenes returned no data — check earlier log lines for "
                "GRAPHQL_VALIDATION_FAILED messages."
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

    The top-level `oshash`/`checksum` lookup is a defensive fallback: modern
    Stash returns only `files[].fingerprints`, but if a caller shapes input
    dicts with those keys directly (e.g. tests, older Stash responses), use
    them too.
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

    Stash stores these as `<generated>/screenshots/<checksum>.{mp4,webp}`.
    Older or patched layouts may place them directly under `<generated>/`, so
    we try both and return the first hit for each format.

    Returns (mp4_path_or_none, webp_path_or_none).
    """
    gp = Path(generated_path)
    search_dirs = [gp / "screenshots", gp]
    mp4_path: Path | None = None
    webp_path: Path | None = None
    for c in scene_checksums(scene):
        for base in search_dirs:
            if mp4_path is None:
                cand = base / f"{c}.mp4"
                if cand.exists():
                    mp4_path = cand
            if webp_path is None:
                cand = base / f"{c}.webp"
                if cand.exists():
                    webp_path = cand
            if mp4_path and webp_path:
                break
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


def pick_source_file(scene: dict[str, Any]) -> tuple[str, float] | None:
    """Return (path, duration_seconds) of the first usable source file.

    "Usable" = has a non-blank `path` and a positive `duration`. Returns
    None when the scene has no suitable file (e.g. detached scene, missing
    duration metadata).
    """
    for f in scene.get("files") or []:
        path = f.get("path")
        try:
            dur = float(f.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        if path and dur > 0:
            return str(path), dur
    return None


def segment_offsets(
    duration: float, segments: int, segment_duration: float
) -> list[float]:
    """Evenly-spaced segment start offsets across the source duration.

    Uses the same spacing rule as Stash's own preview generator: each
    segment is centred inside one of N equal-width windows, so offsets are
    `duration * (i + 0.5) / N` minus half the segment duration so the
    segment stays inside its window.

    Falls back to packing the segments at t=0 for very short videos where
    the computed spacing would overlap.
    """
    if segments <= 0 or duration <= 0:
        return [0.0]
    if duration <= segments * segment_duration:
        # Not enough runway for spaced-out segments — pack them serially.
        return [i * segment_duration for i in range(segments)]
    out: list[float] = []
    for i in range(segments):
        centre = duration * (i + 0.5) / segments
        start = max(0.0, centre - segment_duration / 2)
        out.append(round(start, 3))
    return out


def _extract_segment(
    src_path: str,
    offset: float,
    duration: float,
    vf: str,
    settings: dict[str, Any],
    out_path: Path,
) -> tuple[bool, str]:
    """Decode a single segment from the source, apply filter, re-encode.

    `-threads N` caps ffmpeg's internal decoder+filter+encoder parallelism.
    Default 4 — large enough to hide H.264 decoder latency on a modern
    8-core box, small enough to keep a single 8K HEVC source decode below
    ~1 GB RSS. Plugin-level workers>1 multiplies this, so a user who
    wants high throughput should raise workers first, threads second.
    """
    threads = int(settings.get("ffmpegThreads") or 0)
    thread_args = ["-threads", str(threads)] if threads > 0 else []
    cmd = [
        settings["ffmpegBin"],
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *thread_args,
        "-ss",
        f"{offset:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        src_path,
        "-vf",
        vf,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        settings["preset"],
        "-crf",
        str(settings["crf"]),
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        *thread_args,
        str(out_path),
    ]
    rc, err = _run_ffmpeg(cmd)
    if rc != 0:
        with contextlib.suppress(FileNotFoundError):
            out_path.unlink()
        return False, err or f"ffmpeg exit {rc}"
    return True, ""


def _concat_segments(
    segment_paths: list[Path], out_path: Path, settings: dict[str, Any]
) -> tuple[bool, str]:
    """Stitch pre-encoded segments into a single mp4 via stream copy.

    The concat demuxer needs a listing file. Segments were all encoded with
    identical codec params (same preset/crf/pix_fmt), so stream copy works
    without re-encoding and is effectively free.
    """
    work = out_path.parent
    list_file = work / f"{out_path.name}.concat-list.txt"
    with list_file.open("w") as f:
        for p in segment_paths:
            # ffmpeg concat demuxer needs single-quoted absolute paths.
            f.write(f"file '{p}'\n")
    try:
        cmd = [
            settings["ffmpegBin"],
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        rc, err = _run_ffmpeg(cmd)
        if rc != 0:
            with contextlib.suppress(FileNotFoundError):
                out_path.unlink()
            return False, err or f"ffmpeg exit {rc}"
        return True, ""
    finally:
        with contextlib.suppress(FileNotFoundError):
            list_file.unlink()


def _flatten_mp4_to_webp(
    mp4_path: Path, webp_path: Path, settings: dict[str, Any]
) -> tuple[bool, str]:
    """Transcode an already-flattened mp4 into the animated-webp preview.

    We deliberately feed the freshly-flattened mp4 (not the source) here —
    crop/v360 has already been applied to the mp4, and this second pass is
    a cheap format conversion.
    """
    tmp = webp_path.with_suffix(webp_path.suffix + ".tmp.webp")
    cmd = [
        settings["ffmpegBin"],
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(mp4_path),
        "-an",
        "-c:v",
        "libwebp_anim",
        "-loop",
        "0",
        "-compression_level",
        "6",
        "-quality",
        "70",
        str(tmp),
    ]
    rc, err = _run_ffmpeg(cmd)
    if rc != 0:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        return False, err or f"ffmpeg exit {rc}"
    os.replace(tmp, webp_path)
    return True, ""


def flatten_from_source(
    src_path: str,
    duration: float,
    vf_body: str,
    out_mp4: Path,
    out_webp: Path | None,
    settings: dict[str, Any],
) -> tuple[bool, str, bool]:
    """Produce a multi-segment flattened preview for one scene.

    Returns (mp4_ok, message, webp_ok). A failed webp doesn't void the mp4
    — callers report that as "partial" the same way the old preview-based
    code did, so a bad webp encoder doesn't block an otherwise successful
    run.
    """
    segments = int(settings["segments"])
    seg_dur = float(settings["segmentDuration"])
    out_w = int(settings["outputWidth"])
    out_h = int(settings["outputHeight"])

    # Per-segment filter graph: crop + v360 + scale to preview dims.
    vf = f"{vf_body},scale={out_w}:{out_h}"

    offsets = segment_offsets(duration, segments, seg_dur)

    work = out_mp4.parent / f".{out_mp4.stem}.vrflat-work"
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    try:
        seg_paths: list[Path] = []
        for i, off in enumerate(offsets):
            seg_path = work / f"seg_{i:02d}.mp4"
            ok, err = _extract_segment(src_path, off, seg_dur, vf, settings, seg_path)
            if not ok:
                return False, f"segment {i} @ {off:.1f}s: {err}", False
            seg_paths.append(seg_path)

        tmp_mp4 = out_mp4.with_suffix(out_mp4.suffix + ".tmp.mp4")
        ok, err = _concat_segments(seg_paths, tmp_mp4, settings)
        if not ok:
            return False, f"concat: {err}", False
        os.replace(tmp_mp4, out_mp4)

        webp_ok = True
        if out_webp is not None:
            w_ok, w_err = _flatten_mp4_to_webp(out_mp4, out_webp, settings)
            if not w_ok:
                webp_ok = False
                log.warning(f"webp from {out_mp4.name} failed: {w_err}")
        return True, "", webp_ok
    finally:
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(work)


# ---------- per-scene orchestration --------------------------------------------


def _process_scene(
    scene: dict[str, Any],
    settings: dict[str, Any],
    generated_path: str,
) -> dict[str, Any]:
    """Process one scene. Safe for thread-pool execution.

    Flow: locate the source video path + duration, figure out where Stash
    writes this scene's preview on disk (via hashes → `screenshots/<hash>.*`),
    then re-render the preview from source in N short segments stitched
    together. The source video is only opened for reading; the preview
    files are overwritten in place via atomic rename.
    """
    sid = str(scene.get("id"))
    title = scene.get("title") or f"scene {sid}"
    tag_names = {t.get("name") for t in (scene.get("tags") or []) if t.get("name")}
    vf_body = build_filter_graph(tag_names, settings)

    result: dict[str, Any] = {
        "scene_id": sid,
        "title": title,
        "filter": vf_body,
        "mp4": None,
        "webp": None,
        "status": "no_source",
        "errors": [],
    }

    source = pick_source_file(scene)
    if source is None:
        # No source video path / duration — can't re-render from source.
        return result
    src_path, duration = source

    mp4, webp = find_preview_files(scene, generated_path)
    if mp4 is None:
        # No existing preview → we don't know the right hash to write under.
        # Require Stash's Generate → Preview to have run first so the path
        # is deterministic.
        result["status"] = "no_preview"
        return result

    # Skip already-processed scenes unless the user explicitly requested a
    # reprocess. Marker lives next to the mp4.
    if is_marked(mp4) and not settings["reprocess"]:
        result["status"] = "skipped_marker"
        result["mp4"] = {"status": "skipped_marker"}
        return result

    if settings["dryRun"]:
        result["status"] = "would_process"
        result["source"] = {"path": src_path, "duration": duration}
        result["mp4"] = {"path": str(mp4)}
        if webp:
            result["webp"] = {"path": str(webp)}
        return result

    mp4_ok, err, webp_ok = flatten_from_source(
        src_path, duration, vf_body, mp4, webp, settings
    )

    if mp4_ok:
        write_marker(mp4)
        if webp and webp_ok:
            result["status"] = "flattened"
            result["mp4"] = {"status": "flattened"}
            result["webp"] = {"status": "flattened"}
        elif webp and not webp_ok:
            # mp4 succeeded, webp failed — still useful.
            result["status"] = "partial"
            result["mp4"] = {"status": "flattened"}
            result["webp"] = {"status": "failed"}
            result["errors"].append("webp transcode failed (mp4 is fine)")
        else:
            result["status"] = "flattened"
            result["mp4"] = {"status": "flattened"}
    else:
        result["status"] = "failed"
        result["mp4"] = {"status": "failed"}
        result["errors"].append(err)

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
