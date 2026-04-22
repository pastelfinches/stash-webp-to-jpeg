# pastelfinches / stash-plugins

A source repository of [Stash](https://github.com/stashapp/stash) plugins, built on each push to `main` and published via GitHub Pages.

## Plugins

| Plugin | Description |
|---|---|
| [webp_to_jpeg](plugins/webp_to_jpeg) | Convert scene covers stored as WEBP to JPEG in-place, preserving the original artwork. Fixes missing thumbnails in HereSphere and other WEBP-averse clients. |

## Install via Stash source URL

1. In Stash, go to **Settings → Plugins → Available Plugins → Add Source**.
2. Add:

   ```
   Name: pastelfinches
   Source URL: https://pastelfinches.github.io/stash-plugins/main/index.yml
   Local path: pastelfinches
   ```

3. Click the source, pick a plugin, install.
4. Reload plugins when prompted.

Dependencies listed in each plugin's `requirements.txt` must be installed into Stash's Python environment (`pip install -r <plugin>/requirements.txt`).

## Repository layout

```
plugins/<plugin_id>/
    <plugin_id>.yml       # Stash plugin manifest
    <plugin_id>.py        # plugin script
    requirements.txt      # pip dependencies
tests/
    test_*.py             # fast unit tests
    integration/          # Stash sandbox tests
    sandbox/               # docker-compose for ephemeral Stash
build_site.sh              # generates _site/index.yml + per-plugin zip
.github/workflows/         # CI (tests) + GH Pages deploy
```

The plugin id is the filename of the manifest (without `.yml`), and it must match the directory name.

## Developing

Run unit tests only (fast, no Docker):

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r tests/requirements-test.txt
pytest tests/test_*.py
```

Run integration tests against a temporary Stash sandbox (requires Docker and ffmpeg):

```sh
# One-time: generate the tiny test video
ffmpeg -y -f lavfi -i color=c=black:s=64x64:d=1:r=10 \
  -c:v libx264 -pix_fmt yuv420p -preset ultrafast \
  -movflags +faststart tests/sandbox/media/sample.mp4

pytest tests/integration
```

The integration harness spins up `stashapp/stash:latest` via docker-compose on `127.0.0.1:19999` (override with `STASH_HOST_PORT=...`), runs the setup wizard programmatically, scans a 1-second test video, seeds it with a WEBP cover, invokes the plugin, and asserts the cover is now JPEG. The container and volumes are torn down at the end of the module.

Build the source index locally:

```sh
./build_site.sh _site
cat _site/index.yml
```

## Credits

- Build script structure adapted from [feederbox826/plugins](https://github.com/feederbox826/plugins) (AGPLv3).
- Stash plugin API: [stashapp/stash](https://github.com/stashapp/stash), library: [stg-annon/stashapi](https://github.com/stg-annon/stashapi).

## License

MIT — see [LICENSE](LICENSE).
