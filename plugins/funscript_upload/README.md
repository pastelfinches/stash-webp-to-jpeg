# Funscript Upload

Adds an **Upload funscript** icon button to the scene detail page in the Stash UI.

Pick a `.funscript` file from your local device — the plugin writes it next to the video with the correct basename, triggers a metadata scan so Stash marks the scene **interactive**, and optionally kicks off heatmap generation immediately.

No more manually copying files via SMB or filebrowser.

## Requirements

- **CommunityScriptsUILibrary** — provides the `csLib` JavaScript helper used by the button. Install it from the Stash community source before installing this plugin.
- **Python 3.7+** in the Stash container (standard in all official Stash images).
- `stashapi` Python package — installed automatically via PythonDepManager (if installed) or via `pip` on first run.

## Installation

1. In Stash, go to **Settings → Plugins → Available Plugins → Add Source**.
2. Add:

   ```
   Name: pastelfinches
   Source URL: https://pastelfinches.github.io/stash-plugins/main/index.yml
   Local path: pastelfinches
   ```

3. Install **CommunityScriptsUILibrary** from the community source (if not already installed).
4. Install **Funscript Upload** from the pastelfinches source.
5. Reload plugins when prompted.

## Usage

1. Navigate to any scene's detail page (`/scenes/<id>`).
2. Click the **file-arrow-up icon** in the scene toolbar (the upload icon sits alongside the other icon buttons, before the operations menu).
3. A dialog appears:
   - If the scene is already interactive (a funscript exists), the dialog warns you and asks to confirm replacement.
   - A **"Generate heatmap after upload"** checkbox (default: on) lets you opt into immediate heatmap generation. When checked, the plugin calls `metadataGenerate` for the scene (heatmap only — no covers, sprites, or previews are regenerated) right after the scan is triggered.
4. Click **Upload** (or **Replace** if the scene is already interactive) to proceed, or **Cancel** to abort.
5. The plugin validates, writes the file, triggers a metadata scan, and optionally triggers heatmap generation. A toast notification reports the outcome.

## "Generate heatmap after upload" toggle

The checkbox in the upload dialog controls whether `metadataGenerate` is called for the scene immediately after the file is written. The generate call is scoped to `interactiveHeatmapsSpeeds: true` only — it will not regenerate covers, sprites, previews, phashes, transcodes, or any other asset. If the generate call fails (e.g. Stash is busy), the task still succeeds and a warning is logged; you can run Generate manually from Settings → Tasks.

## How the file is named

The destination filename is derived from the **scene video's basename**, not the upload filename. For example, if the video is `/data/MyScene.mp4`, the funscript is written to `/data/MyScene.funscript` regardless of what you named the file on your computer.

## Settings

| Setting | Type | Default | Description |
|---|---|---|---|
| Max upload size in MB | NUMBER | 20 | Maximum decoded funscript size in megabytes. Raise if you have very large funscripts. |

## Security

- The destination path is always derived from the **scene's video path** (from GraphQL), never from the upload filename — path traversal via the filename is impossible.
- The file is written atomically (temp file + rename) so a failed upload cannot corrupt an existing script.
- Overwrite only happens when the user explicitly confirms in the dialog.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Button does not appear | CommunityScriptsUILibrary not installed | Install it from the community source and reload plugins |
| "scene has no associated files" error | Scene is not backed by a file (streaming-only?) | Scan the library so the file is registered |
| Scene does not flip to interactive after upload | Scan did not complete or Stash version too old | Trigger a manual rescan from Settings → Tasks |
| "invalid funscript" error | File is not a valid funscript JSON | Check the file; it must have an `actions` array with `at`/`pos` entries |
| Heatmap not generated after upload | Generate call failed (Stash busy) | Run Generate from Settings → Tasks manually |
