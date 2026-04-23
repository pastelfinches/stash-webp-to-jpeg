# VR Preview Flattener

Regenerates scene preview videos for VR/AR scenes so they're watchable in a normal browser — crops one stereo eye and, when appropriate, unwraps the input projection (equirectangular / fisheye / 360°) with ffmpeg's `v360` filter.

## Why

Stash's built-in preview generator runs ffmpeg over the raw video file. For VR/AR scenes that file is either:

- side-by-side stereo (both eyes squished into one frame), or
- equirectangular / fisheye projection (wide warped ball-shaped view),

…so the hover-preview looks like two squashed fish-eyes. Meanwhile, pass-through clients like HereSphere or a VR headset render the raw file fine — it's only the library view in a normal browser that's unreadable.

This plugin re-runs ffmpeg over Stash's *already-generated* preview file (not the source), applying a two-stage filter graph:

1. **Crop one eye** — `crop=iw/2:ih:0:0` for side-by-side, `crop=iw:ih/2:0:0` for over/under.
2. **Reproject** — `v360=hequirect:flat:…` for 180°/190°/200° equirect, `v360=equirect:flat:…` for 360°, `v360=fisheye:flat:…` for fisheye. Skipped when `defaultProjection=flat`.

The result is overwritten in place at `<generated>/<hash>.mp4` and `<generated>/<hash>.webp`.

## What this plugin touches (and what it doesn't)

**Touches only derived preview files** at `$generated/<hash>.mp4` and `$generated/<hash>.webp`. These are the animated hover-previews Stash generates from your source video.

**Never touches:**
- Source video files — never opened, not even for reading.
- The scrubber sprite (`_sprite.jpg` / `_thumbs.vtt`).
- The scene screenshot / cover (`<hash>.jpg`).
- Stash's database, ratings, tags, or any GraphQL state.

A bad run at worst produces bad preview files. **Fix = Stash's "Generate → Preview" task**, which rebuilds previews from the source. No plugin-level backup is kept; previews are cheap to regenerate and backing them up would waste disk on every user.

## Safety rollout

1. **Dry Run first.** Turn on `dryRun`. Run the task. Read the "Filter usage breakdown" at the end — one line per unique filter string, with a scene count. Mis-tagged scenes show up as outliers (e.g. 3 scenes getting the fisheye filter when your library has no fisheye content → go fix those tags before a live run).
2. **Turn off `dryRun`, set `limit: 5`.** Run for real. Spot-check the five flattened hover-previews in Stash's grid view. Confirm HereSphere / headset playback of those same scenes still works — if that breaks too, you've either found a bug or a mis-configuration in your Stash's source-file routing (the plugin cannot affect it, so investigate before continuing).
3. **Raise `limit` to 50.** The `vr_flat` marker makes re-runs incremental; already-processed scenes are skipped.
4. **`limit: 0` for the full library.**

If a run produces wrong-looking previews, don't panic — run Stash's "Generate → Preview" task (over the affected scenes, or the whole library) to rebuild them from source. Then adjust plugin settings and re-run this task.

## Install

Via plugin source:

1. **Settings → Plugins → Available Plugins → Add Source**
2. Source URL: `https://pastelfinches.github.io/stash-plugins/main/index.yml`
3. Install **VR Preview Flattener**. `PythonDepManager` is pulled in for Python deps (just `stashapi` this time).
4. ffmpeg + ffprobe must be on `PATH` (or point the `ffmpegBin` / `ffprobeBin` settings at their full paths). The official `stashapp/stash` Docker image ships both.

Manual install: clone `plugins/vr_preview_flatten/` into Stash's `plugins/` folder and reload plugins.

## Usage

This is a **manual-only task** — there is no hook. Stash's own "Generate" task regenerates previews from the source, which will overwrite the flattened result, so running this on every scene update would just keep losing to Generate. Instead, run the task after any bulk preview generation.

1. (Optional) Enable **Dry Run** to see per-scene filter graphs without modifying anything.
2. **Settings → Tasks**, click **Flatten VR Previews**.
3. The task lists a filter-usage breakdown at the end (how many scenes got which filter graph) — useful for catching mistagged scenes.

## Tag-driven strategy

StashDB-aligned defaults. All tag names are configurable in plugin settings.

| Tag | Effect |
|---|---|
| **Virtual Reality** | Scene enters the processing set. |
| **Augmented Reality** | Same — clear the setting blank to ignore AR scenes. |
| **Top-Bottom** | Stereo layout is over/under instead of side-by-side. |
| **Fisheye** | Input is treated as fisheye projection instead of equirectangular. |
| **180°** | Input FOV is 180° (this is also the default when no FOV tag is present). |
| **190°** / **200°** | Input FOV is 190° / 200° (MKX200, VRCA220). |
| **360°** | Full equirectangular — uses `v360=equirect` instead of `hequirect`. |

Without any modifier tags, a VR/AR-only scene is assumed to be **SBS + equirectangular + 180°** — the most common encoding.

## Settings

| Setting | Type | Default | Notes |
|---|---|---|---|
| `dryRun` | boolean | `false` | Print per-scene plan only. |
| `reprocess` | boolean | `false` | Re-run on previews already marked as flattened. |
| `limit` | number | `0` | Cap scenes per run (0 = no cap). Use a small number for initial test passes. |
| `workers` | number | `2` | Parallel ffmpeg jobs. ffmpeg is CPU-heavy; don't set this higher than ~half your cores. |
| `virtualRealityTag` | string | `Virtual Reality` | |
| `augmentedRealityTag` | string | `Augmented Reality` | Blank = ignore AR scenes. |
| `fisheyeTag` | string | `Fisheye` | |
| `topBottomTag` | string | `Top-Bottom` | |
| `fov180Tag` / `fov190Tag` / `fov200Tag` / `fov360Tag` | string | `180°` / `190°` / `200°` / `360°` | |
| `defaultFov` | number | `180` | Used when no FOV tag is present. |
| `defaultProjection` | string | `equirect` | `equirect` / `fisheye` / `flat`. `flat` skips reprojection (crops one eye only). |
| `outputHFov` / `outputVFov` | number | `90` / `90` | Horizontal/vertical FOV of the flattened output in degrees. |
| `ffmpegBin` / `ffprobeBin` | string | `ffmpeg` / `ffprobe` | Override if not on PATH. |
| `crf` | number | `23` | x264 CRF for the re-encoded mp4 preview. Lower = bigger + sharper. |

## Idempotency

After flattening a preview, the plugin writes a zero-byte sidecar next to it — e.g. `abc123.mp4.vr_flat` alongside `abc123.mp4`. On re-run, any preview with a sidecar is skipped unless **Reprocess** is enabled.

Stash's Generate task overwrites the preview but leaves the sidecar behind; that gives you a false-positive "already flattened" skip. **After running Generate, either turn on Reprocess for one flatten pass, or delete `*.vr_flat` from the generated directory first.**

## Caveats

- **Regenerate overwrites you.** This is accepted — re-run the task afterward.
- **Scrubber sprite (the thumbnail strip) and screenshot (single-frame cover) are not touched.** The scrubber will still display SBS thumbnails. If you want those fixed too, that's a follow-up.
- **Lossy re-encode.** The output mp4 is re-encoded with libx264 at the configured CRF. Previews are already aggressively compressed, so quality loss from a second pass is usually invisible at preview resolutions — but it's not zero.
- **Tag hygiene matters.** A scene marked "Virtual Reality" but actually encoded flat 2D will get cropped in half. Use Dry Run first on a new library to spot outliers via the filter-usage breakdown.
- **Fisheye + higher-FOV combos:** the plugin passes `ih_fov=iv_fov=<tag>` to `v360`. Lenses with a non-square FOV (e.g. 200° horizontal / 180° vertical) aren't represented in StashDB tags and will be slightly off — usable, but not perfect.
- **Only the `.mp4` and `.webp` animated previews are processed.** If you've disabled preview generation and only have sprites, there's nothing for this plugin to do.

## How it works

1. Resolve the VR / AR tag names → tag IDs via `findTag`.
2. `findScenes` with `tags INCLUDES [vr_id, ar_id]`, paginated at 200/page.
3. `configuration.general.generatedPath` → locate the preview directory.
4. Per scene: collect every fingerprint value (oshash, MD5, phash), stat `<generated>/<hash>.mp4` and `<generated>/<hash>.webp` until one hits.
5. Build the filter graph from the scene's tags; run ffmpeg to `<preview>.tmp.mp4` / `<preview>.tmp.webp`, then atomic-rename over the original and write a `.vr_flat` sidecar.
