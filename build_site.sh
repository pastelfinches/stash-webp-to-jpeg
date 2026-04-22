#!/bin/bash
# Builds a Stash plugin source repository from ./plugins/<id>/<id>.yml
#
# Output layout (in $outdir, default _site):
#   index.yml        — source manifest consumed by Stash
#   <plugin_id>.zip  — per-plugin archive referenced from index.yml
#
# Adapted from feederbox826/plugins (AGPLv3)
#   https://github.com/feederbox826/plugins/blob/main/build_site.sh
# Original inspiration: stashapp/CommunityScripts build scripts.

set -euo pipefail

outdir="${1:-_site}"
rm -rf "$outdir"
mkdir -p "$outdir"

buildPlugin() {
    local f="$1"
    local dir plugin_id
    dir="$(dirname "$f")"
    plugin_id="$(basename "$f" .yml)"

    echo "Processing $plugin_id"

    local commit updated zipfile
    commit="$(git log -n 1 --pretty=format:%h -- "$dir"/* || echo unknown)"
    updated="$(TZ=UTC0 git log -n 1 --date='format-local:%F %T' --pretty=format:%ad -- "$dir"/* || date -u +'%F %T')"
    zipfile="$(realpath "$outdir/$plugin_id.zip")"

    pushd "$dir" > /dev/null
    zip -r "$zipfile" . > /dev/null
    popd > /dev/null

    local name description yml_version version dep
    name="$(awk -F': *' '/^name:/ {sub(/\r$/,""); sub(/^"(.*)"$/, "\\1", $2); print $2; exit}' "$f")"
    description="$(awk -F': *' '/^description:/ {sub(/\r$/,""); sub(/^"(.*)"$/, "\\1", $2); print $2; exit}' "$f")"
    yml_version="$(awk -F': *' '/^version:/ {sub(/\r$/,""); sub(/^"(.*)"$/, "\\1", $2); print $2; exit}' "$f")"
    version="${yml_version}-${commit}"
    dep="$(grep '^# requires:' "$f" | head -n 1 | cut -c 12- | tr -d '\r' || true)"

    {
        echo "- id: $plugin_id"
        echo "  name: $name"
        echo "  metadata:"
        echo "    description: $description"
        echo "  version: $version"
        echo "  date: $updated"
        echo "  path: $plugin_id.zip"
        echo "  sha256: $(sha256sum "$zipfile" | cut -d' ' -f1)"
        if [[ -n "$dep" ]]; then
            echo "  requires:"
            for d in ${dep//,/ }; do
                echo "    - $d"
            done
        fi
        echo
    } >> "$outdir/index.yml"
}

find ./plugins -mindepth 2 -maxdepth 2 -name '*.yml' | sort | while read -r file; do
    buildPlugin "$file"
done

echo "Built source at $outdir/index.yml"
