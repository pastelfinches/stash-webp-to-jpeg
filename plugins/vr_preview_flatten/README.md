# VR Preview Flattener

Regenerates scene animated previews for VR/AR scenes so they're watchable in a normal browser — re-renders each preview **from the source video**, cropping one stereo eye and (when appropriate) unwrapping the input projection (equirectangular / fisheye / 360°) with ffmpeg's `v360` filter. Output is stitched from N short segments spread across the source, matching Stash's own preview pattern.

## Why

Stash's built-in preview generator runs ffmpeg over the raw video file. For VR/AR scenes that file is either:

- side-by-side stereo (both eyes squished into one frame), or
- equirectangular / fisheye projection (wide warped ball-shaped view),

…so the hover-preview looks like two squashed fish-eyes. Meanwhile, pass-through clients like HereSphere or a VR headset render the raw file fine — it's only the library view in a normal browser that's unreadable.

## How it works

For each scene tagged `Virtual Reality` or `Augmented Reality`, the plugin:

1. Pulls the source video path + duration from Stash's GraphQL.
2. Picks N segment offsets evenly spaced across the source (matching Stash's `previewSegments` / `previewSegmentDuration` config, default 12 × 0.75s = 9s total).
3. For each segment, runs `ffmpeg -ss OFFSET -t DURATION -i SOURCE -vf <crop,v360,scale>` → writes a short encoded mp4 to a scratch dir.
4. Concat-demuxes the segments into the final mp4 via `-c copy` (no re-encode) and overwrites `<generated>/screenshots/<hash>.mp4`.
5. Transcodes the resulting mp4 → animated webp for `<generated>/screenshots/<hash>.webp`.

The filter chain per segment:

- **Crop one eye** — `crop=iw/2:ih:0:0` for side-by-side, `crop=iw:ih/2:0:0` for over/under.
- **Reproject** — `v360=hequirect:flat:…` for 180°/190°/200° equirect, `v360=equirect:flat:…` for 360°, `v360=fisheye:flat:…` for fisheye. Skipped when `defaultProjection=flat`.
- **Scale** — `scale=WIDTH:HEIGHT` to preview dimensions (default 960×720).

Previous releases (v0.1 / v0.2) re-encoded Stash's already-downsampled preview instead of the source. That was visually mushy — a 640×320 preview has maybe 160 px of useful content per eye; no amount of CRF tuning recovers detail that isn't there. v0.3+ reads from source. **Upgrading from v0.2: `.vr_flat` markers from the old release are ignored; scenes will naturally re-flatten from source on the next run.**

## What this plugin touches (and what it doesn't)

**Touches:**
- `$generated/screenshots/<hash>.mp4` — overwritten in place (atomic rename).
- `$generated/screenshots/<hash>.webp` — overwritten in place.

**Reads (never writes):**
- The source video files listed in `scene.files[].path`. Opened read-only for short seek+extract passes (one 0.75-second window per segment).

**Never touches:**
- Source video files for writing — not modified, not moved, not re-muxed. Only read with `ffmpeg -ss ... -t ... -i SOURCE`.
- The scrubber sprite (`_sprite.jpg` / `_thumbs.vtt`).
- The scene screenshot / cover (`<hash>.jpg`).
- Stash's database, ratings, tags, or any GraphQL state.

A bad run at worst produces bad preview files. **Fix = Stash's "Generate → Preview" task**, which rebuilds previews from source. No plugin-level backup is kept; previews are cheap to regenerate.

## IO & memory pressure

**IO:** Source-based flattening reads ~1.5 MB per scene (12 × 0.75s windows from an 8K H.265 source). A 55-scene library pulls roughly 80 MB total — not a lot, but seek-heavy. On NVMe this is invisible; on NAS or spinning rust the seeks dominate wall time.

**Memory:** An 8K HEVC decoder + libx264 encoder with `-threads 4` peaks around **2.5–3 GB RSS** per running ffmpeg. Default `workers=1` stays under the 4 GB memory limit a container-deployed Stash typically has. Raising workers multiplies this roughly linearly — on a 4 GB Stash container, `workers: 2` will OOM-kill Stash mid-run. If your Stash is on a beefy bare-metal host with RAM to spare, 2–3 workers is fine; otherwise leave `workers: 1`.

**Wall time:** ~70–90 s per scene at `preset medium` with `workers: 1` on an 8K source. A 55-scene library takes ~75 min serially; running `workers: 2` roughly halves that on a host with sufficient RAM.

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
| `outputWidth` / `outputHeight` | number | `960` / `720` | Pixel dimensions of the flattened preview after scaling. |
| `segments` | number | `0` (= inherit) | Number of short clips stitched together. `0` inherits Stash's own `previewSegments` config (typically 12). |
| `segmentDuration` | number | `0` (= inherit) | Duration of each segment in seconds. `0` inherits Stash's `previewSegmentDuration` (typically 0.75s). |
| `crf` | number | `18` | x264 CRF for the re-encoded mp4. Lower = bigger + sharper. 18 is near-visually-lossless. |
| `preset` | string | `medium` | x264 preset. `medium` balances quality vs. speed for source-based flattening; `slower` gains a little compression efficiency for ~2× encode time. |
| `workers` | number | `1` | Scenes processed in parallel. Each worker decodes a 4K/8K source and can hold >1 GB of decoder state. **Default 1** to avoid OOM on default-sized Stash containers. Raise to 2–3 on hosts with 16+ GB free RAM. |
| `ffmpegThreads` | number | `4` | `-threads N` cap on ffmpeg's internal parallelism. Default 4 keeps 8K HEVC decode ≤~1 GB RSS. Set 0 to let ffmpeg auto-pick (can spike memory on high-core hosts). |

## Idempotency

After flattening a preview, the plugin writes a zero-byte sidecar next to it — e.g. `abc123.mp4.vr_flat_v2` alongside `abc123.mp4`. On re-run, any preview with a sidecar is skipped unless **Reprocess** is enabled.

Stash's Generate task overwrites the preview but leaves the sidecar behind; that gives you a false-positive "already flattened" skip. **After running Generate, either turn on Reprocess for one flatten pass, or delete `*.vr_flat_v2` from the generated directory first.**

(Upgrading from v0.2 or earlier: the old `.vr_flat` markers are intentionally ignored by v0.3+, so an upgrade triggers a fresh source-based re-flatten on the next run.)

## Caveats

- **Regenerate overwrites you.** Stash's "Generate → Preview" task rewrites the preview from source without this plugin's filter chain. Re-run the plugin afterward to re-flatten.
- **Scrubber sprite (the thumbnail strip) and screenshot (single-frame cover) are not touched.** The scrubber will still display SBS thumbnails. If you want those fixed too, that's a follow-up.
- **Tag hygiene matters.** A scene marked "Virtual Reality" but actually encoded flat 2D will get cropped in half. Use Dry Run first on a new library to spot outliers via the filter-usage breakdown.
- **Fisheye + higher-FOV combos:** the plugin passes `ih_fov=iv_fov=<tag>` to `v360`. Lenses with a non-square FOV (e.g. 200° horizontal / 180° vertical) aren't represented in StashDB tags and will be slightly off — usable, but not perfect.
- **Needs an existing preview file on disk** to know where to write. Run Stash's "Generate → Preview" over new scenes first; the plugin overwrites the preview at its existing hash-derived path.
- **Source path must be reachable from the Stash process.** The plugin uses `scene.files[].path` exactly as Stash reports it, which means that path must exist inside whatever filesystem Stash is running on (containerised Stash: `/data/...` style paths).

## How it works

1. Resolve the VR / AR tag names → tag IDs via `findTag`.
2. `findScenes` with `tags INCLUDES [vr_id, ar_id]`, paginated at 200/page. Query pulls `files { path duration fingerprints }` for each scene.
3. Per scene: pick first file with a path + positive duration; locate the existing preview at `<generated>/screenshots/<hash>.mp4`.
4. Build the per-scene filter graph from the scene's tags.
5. Extract N segments from the source via `ffmpeg -ss … -t … -i SOURCE -vf <filter,scale>`; each lands as a short mp4 in a scratch dir.
6. Concat-demux the segments into the final mp4 with `-c copy` (no re-encode).
7. Transcode the final mp4 → animated webp for the `.webp` preview.
8. Atomic-rename over the originals and write a `.vr_flat_v2` sidecar.
