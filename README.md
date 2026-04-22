# WEBP to JPEG Cover Converter

A [Stash](https://github.com/stashapp/stash) plugin that finds scene cover images stored as WEBP and re-encodes them as JPEG in-place, preserving the original scraped artwork.

## Why

Some VR/gallery clients — most notably [HereSphere](https://heresphere.xyz/) — don't render WEBP. When scrapers or studio packs deliver covers as WEBP, Stash stores them as-is and thumbnails vanish in those clients. The usual "Generate → Cover images" task replaces the cover with a frame grab from the video, which discards the original artwork.

This plugin takes the middle path: it keeps the exact same image bytes but re-encodes them from WEBP to JPEG, so you keep the studio cover and every client can display it.

## Installation

1. Copy the plugin directory into your Stash `plugins/` folder:

   ```sh
   cd /path/to/stash/plugins
   git clone https://github.com/pastelfinches/stash-webp-to-jpeg.git
   ```

2. Install Python dependencies:

   ```sh
   pip install -r stash-webp-to-jpeg/requirements.txt
   ```

3. In Stash: **Settings → Plugins → Reload Plugins**.

## Usage

1. (Optional) **Settings → Plugins → WEBP to JPEG Cover Converter → Dry Run**: enable this first to see how many covers are WEBP without making any changes.
2. Go to **Settings → Tasks**, scroll to the plugin section, click **Convert WEBP Covers to JPEG**.
3. Watch the progress bar. The final summary (scanned/found/converted/errors) is logged at INFO level.

## Settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `dryRun` | boolean | `false` | Report WEBP covers without modifying anything. |
| `jpegQuality` | number | `92` | JPEG encoding quality, 1–100. |

## How It Works

1. Enumerate every scene via GraphQL (paginated).
2. For each scene, fetch `/scene/{id}/screenshot` over HTTP using the session/API key Stash injects into plugins.
3. Check the first 12 bytes for the WEBP magic (`RIFF....WEBP`).
4. If WEBP, decode with Pillow, re-encode as JPEG at the configured quality.
5. Upload via the `sceneUpdate` GraphQL mutation with `cover_image` as a `data:image/jpeg;base64,...` data URL.

The original WEBP is replaced; there is no backup. Covers are still stored in Stash's normal generated-images location.

## Caveats

- Conversion is one-way and lossy (WEBP → JPEG always is).
- Scenes with no cover image are skipped silently.
- If a cover is JPEG, PNG, or any non-WEBP format, it's left untouched.
- This does not alter `Paths.Screenshot` URL structure — clients keep working.

## License

MIT — see [LICENSE](LICENSE).
