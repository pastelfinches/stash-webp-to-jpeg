# WEBP to JPEG Cover Converter

Finds scene cover images stored as WEBP and re-encodes them as JPEG in-place, preserving the original scraped artwork.

## Why

Some VR/gallery clients — most notably [HereSphere](https://heresphere.xyz/) — don't render WEBP. When scrapers or studio packs deliver covers as WEBP, Stash stores them as-is and thumbnails vanish in those clients. The usual "Generate → Cover images" task replaces the cover with a frame grab from the video, which discards the original artwork.

This plugin takes the middle path: it keeps the exact same image bytes but re-encodes them from WEBP to JPEG, so you keep the studio cover and every client can display it.

## Install

Preferred — via plugin source:

1. In Stash, **Settings → Plugins → Available Plugins → Add Source**
2. Source URL: `https://pastelfinches.github.io/stash-plugins/main/index.yml`
3. Install **WEBP to JPEG Cover Converter** from the available plugins list. The [`PythonDepManager`](https://discourse.stashapp.cc/t/pythondepmanager/1801) plugin is pulled in as a dependency.
4. (Optional) Reload plugins when prompted.

Python dependencies (Pillow, stashapi) install themselves on first run:

- **Preferred path:** PythonDepManager. Requires `git` to be available in the environment. The official `stashapp/stash` Docker image (Alpine) doesn't ship git — `docker exec <container> apk add git` once, and PDM works fine thereafter.
- **Fallback path:** if PDM isn't installed or isn't usable (e.g. no git), the plugin calls `pip install` directly. Works out-of-the-box on the default Docker image.

Manual install: clone the `plugins/webp_to_jpeg/` directory into Stash's `plugins/` folder and reload plugins.

## Usage

1. (Optional) Enable **Dry Run** in plugin settings to see how many covers are WEBP without making any changes.
2. **Settings → Tasks**, click **Convert WEBP Covers to JPEG**.
3. Final summary (scanned / found / converted / errors) logged at INFO level.

## Settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `dryRun` | boolean | `false` | Report WEBP covers without modifying anything. |
| `jpegQuality` | number | `92` | JPEG encoding quality, 1–100. |
| `workers` | number | `8` | Scenes processed in parallel. Raise on fast hardware, lower if it pegs Stash's CPU. |

## How it works

1. Enumerate every scene via paginated GraphQL.
2. For each scene, fetch `/scene/{id}/screenshot` using the session/API key Stash injects into plugins.
3. Check the first 12 bytes for the WEBP magic (`RIFF....WEBP`).
4. If WEBP, decode with Pillow, re-encode as JPEG at the configured quality.
5. Upload via the `sceneUpdate` GraphQL mutation using a `data:image/jpeg;base64,...` data URL.

## Caveats

- Conversion is one-way and lossy.
- Scenes with no cover are skipped silently.
- JPEG / PNG / any non-WEBP cover is left untouched.
- URL structure of `paths.screenshot` is unchanged — clients keep working.
