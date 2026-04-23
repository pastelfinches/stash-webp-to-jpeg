"""Unit tests for the VR preview flattener's pure logic.

The filter-graph builder is the load-bearing piece of this plugin — it
translates StashDB tag combinations into ffmpeg `-vf` strings. A bug here
means silently applying the wrong crop/projection to every VR preview in
the library, so every combination worth caring about has its own test.

These tests exercise:
- build_filter_graph() across projection × layout × FOV
- _fov_from_tags / _projection_from_tags / _layout_from_tags
- load_settings() type coercion and safe defaults
- scene_checksums() de-duplication
- marker + preview-discovery helpers
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import vr_preview_flatten as vpf
from stashapi.stashapp import StashInterface

# ---------- filter graph: projection × layout × FOV ----------------------------


def _settings(**overrides: Any) -> dict[str, Any]:
    """Build a settings dict with sensible defaults, overridden per-test."""
    base = dict(vpf.DEFAULTS)
    base.update(overrides)
    return base


class TestLayoutFromTags:
    def test_sbs_when_no_tb_tag(self):
        assert vpf._layout_from_tags(set(), _settings()) == "sbs"

    def test_ou_when_tb_tag_present(self):
        assert vpf._layout_from_tags({"Top-Bottom"}, _settings()) == "ou"

    def test_custom_tb_tag_name_respected(self):
        s = _settings(topBottomTag="Over/Under")
        assert vpf._layout_from_tags({"Over/Under"}, s) == "ou"

    def test_blank_tb_tag_never_matches(self):
        s = _settings(topBottomTag="")
        assert vpf._layout_from_tags({"Top-Bottom"}, s) == "sbs"


class TestProjectionFromTags:
    def test_equirect_is_default(self):
        assert vpf._projection_from_tags(set(), _settings()) == "equirect"

    def test_fisheye_tag_overrides_default(self):
        assert (
            vpf._projection_from_tags({"Fisheye"}, _settings()) == "fisheye"
        )

    def test_default_can_be_flat(self):
        s = _settings(defaultProjection="flat")
        assert vpf._projection_from_tags(set(), s) == "flat"

    def test_fisheye_tag_wins_over_flat_default(self):
        s = _settings(defaultProjection="flat")
        assert vpf._projection_from_tags({"Fisheye"}, s) == "fisheye"


class TestFovFromTags:
    def test_180_is_default(self):
        assert vpf._fov_from_tags(set(), _settings()) == 180

    @pytest.mark.parametrize(
        "tag,expected",
        [
            ("180°", 180),
            ("190°", 190),
            ("200°", 200),
            ("360°", 360),
        ],
    )
    def test_exact_tag_match(self, tag: str, expected: int):
        assert vpf._fov_from_tags({tag}, _settings()) == expected

    def test_highest_wins_if_multiple_present(self):
        # Real libraries sometimes double-tag; we deliberately prefer the
        # larger FOV because mis-unwrapping a 200° video as 180° drops
        # visible content, while the inverse just adds black edges.
        tags = {"180°", "200°"}
        assert vpf._fov_from_tags(tags, _settings()) == 200

    def test_custom_tag_name_respected(self):
        s = _settings(fov200Tag="FOV 200")
        assert vpf._fov_from_tags({"FOV 200"}, s) == 200


class TestBuildFilterGraph:
    def test_sbs_equirect_180_is_default(self):
        # VR/AR only, no modifier tags → most common VR encoding.
        # Default output FOV is 120x90 (wide-ish, avoids severe flat-
        # projection edge distortion past ~130° horizontal).
        vf = vpf.build_filter_graph(set(), _settings())
        assert vf == (
            "crop=iw/2:ih:0:0,"
            "v360=hequirect:flat:ih_fov=180:iv_fov=180:h_fov=120:v_fov=90"
        )

    def test_ou_layout_changes_crop(self):
        vf = vpf.build_filter_graph({"Top-Bottom"}, _settings())
        assert vf.startswith("crop=iw:ih/2:0:0,")

    def test_fisheye_uses_fisheye_mode(self):
        vf = vpf.build_filter_graph({"Fisheye"}, _settings())
        assert "v360=fisheye:flat:" in vf
        assert "hequirect" not in vf
        assert "ih_fov=180" in vf and "iv_fov=180" in vf

    def test_fisheye_200_combines_tags(self):
        # MKX200-style: fisheye lens + 200° FOV
        vf = vpf.build_filter_graph({"Fisheye", "200°"}, _settings())
        assert "v360=fisheye:flat:ih_fov=200:iv_fov=200" in vf

    def test_360_uses_full_equirect(self):
        vf = vpf.build_filter_graph({"360°"}, _settings())
        assert "v360=equirect:flat:" in vf
        # The half-equirect form takes ih_fov/iv_fov; the full one only needs
        # the output h_fov/v_fov. Guard against accidentally concatenating.
        assert "hequirect" not in vf
        assert "ih_fov=" not in vf

    def test_flat_projection_skips_v360(self):
        s = _settings(defaultProjection="flat")
        vf = vpf.build_filter_graph(set(), s)
        assert vf == "crop=iw/2:ih:0:0"
        assert "v360" not in vf

    def test_output_fov_threaded_through(self):
        s = _settings(outputHFov=120, outputVFov=80)
        vf = vpf.build_filter_graph(set(), s)
        assert "h_fov=120" in vf and "v_fov=80" in vf

    def test_all_filter_variants_are_valid_ffmpeg_syntax(self):
        # Smoke check: every permutation produces a comma-separated
        # list of key=value filters, no dangling commas or empty filters.
        for fisheye in (False, True):
            for ou in (False, True):
                for fov_tag in ("180°", "190°", "200°", "360°"):
                    tags: set[str] = {fov_tag}
                    if fisheye:
                        tags.add("Fisheye")
                    if ou:
                        tags.add("Top-Bottom")
                    vf = vpf.build_filter_graph(tags, _settings())
                    assert vf, f"empty filter for {tags}"
                    assert not vf.endswith(","), vf
                    assert ",," not in vf, vf
                    for filt in vf.split(","):
                        assert "=" in filt, f"malformed filter {filt!r} in {vf!r}"


# ---------- settings loader ----------------------------------------------------


class FakeStash:
    """Minimal StashInterface stand-in for load_settings()."""

    def __init__(self, plugin_config: dict[str, Any] | None = None) -> None:
        self._cfg = {"plugins": {vpf.PLUGIN_ID: plugin_config or {}}}

    def get_configuration(self) -> dict[str, Any]:
        return self._cfg


def fake_stash(plugin_config: dict[str, Any] | None = None) -> StashInterface:
    """Cast a FakeStash to StashInterface so pyright accepts the duck-type.

    load_settings() only calls .get_configuration(); we don't want to widen
    production types to Any just for tests.
    """
    return cast(StashInterface, FakeStash(plugin_config))


class FakeStashWithGeneral:
    """FakeStash variant that also exposes the `general` section.

    Used for tests that exercise Stash-config inheritance (e.g. segments /
    segmentDuration falling back to previewSegments / previewSegmentDuration).
    """

    def __init__(
        self,
        plugin_config: dict[str, Any] | None = None,
        general: dict[str, Any] | None = None,
    ) -> None:
        self._cfg = {
            "plugins": {vpf.PLUGIN_ID: plugin_config or {}},
            "general": general or {},
        }

    def get_configuration(self) -> dict[str, Any]:
        return self._cfg


def fake_stash_with_general(
    plugin_config: dict[str, Any] | None = None,
    general: dict[str, Any] | None = None,
) -> StashInterface:
    return cast(StashInterface, FakeStashWithGeneral(plugin_config, general))


class TestLoadSettings:
    def test_empty_config_yields_defaults(self):
        # `segments` / `segmentDuration` have a 0 sentinel in DEFAULTS that
        # load_settings resolves against Stash's own `preview*` config (or
        # a fallback constant when Stash config is blank). Skip those keys
        # in this assertion; they get their own dedicated tests below.
        s = vpf.load_settings(fake_stash())
        inherited = {"segments", "segmentDuration"}
        for key, expected in vpf.DEFAULTS.items():
            if key in inherited:
                continue
            assert s[key] == expected, f"default drift for {key}"

    def test_segments_inherit_fallback_when_stash_unset(self):
        s = vpf.load_settings(fake_stash())
        assert s["segments"] == vpf._FALLBACK_SEGMENTS
        assert s["segmentDuration"] == vpf._FALLBACK_SEGMENT_DURATION

    def test_segments_explicit_plugin_override_wins(self):
        s = vpf.load_settings(fake_stash({"segments": 8, "segmentDuration": 1.0}))
        assert s["segments"] == 8
        assert s["segmentDuration"] == 1.0

    def test_segments_inherit_from_stash_preview_config(self):
        # Stash's preview generator has 20 × 0.5s configured — plugin should
        # track that when segments/segmentDuration are left at 0.
        s = vpf.load_settings(
            fake_stash_with_general(
                general={"previewSegments": 20, "previewSegmentDuration": 0.5}
            )
        )
        assert s["segments"] == 20
        assert s["segmentDuration"] == 0.5


class TestSegmentOffsets:
    def test_evenly_spaced_for_long_video(self):
        # 3600s video, 12 segments of 0.75s → offsets centred in 300s windows
        offs = vpf.segment_offsets(3600.0, 12, 0.75)
        assert len(offs) == 12
        # First offset: window 0..300, centre 150, start = 150 - 0.375 = 149.625
        assert offs[0] == pytest.approx(149.625, abs=0.01)
        # Last offset: window 3300..3600, centre 3450, start = 3450 - 0.375
        assert offs[-1] == pytest.approx(3449.625, abs=0.01)
        # Monotonically increasing
        assert all(offs[i] < offs[i + 1] for i in range(len(offs) - 1))

    def test_packs_segments_for_very_short_video(self):
        # 5s video, 12 × 0.75s = 9s requested — doesn't fit spaced.
        # Falls back to sequential packing so segments don't overlap.
        offs = vpf.segment_offsets(5.0, 12, 0.75)
        assert offs == [i * 0.75 for i in range(12)]

    def test_handles_zero_duration(self):
        assert vpf.segment_offsets(0.0, 12, 0.75) == [0.0]

    def test_zero_number_uses_default(self):
        # Stash NUMBER type renders unset fields as 0 in the UI
        s = vpf.load_settings(
            fake_stash({"workers": 0, "crf": 0, "defaultFov": 0})
        )
        assert s["workers"] == vpf.DEFAULTS["workers"]
        assert s["crf"] == vpf.DEFAULTS["crf"]
        assert s["defaultFov"] == vpf.DEFAULTS["defaultFov"]

    def test_out_of_range_number_uses_default(self):
        s = vpf.load_settings(fake_stash({"workers": 9999, "crf": 500}))
        assert s["workers"] == vpf.DEFAULTS["workers"]
        assert s["crf"] == vpf.DEFAULTS["crf"]

    def test_blank_string_uses_default(self):
        s = vpf.load_settings(fake_stash({"virtualRealityTag": "   "}))
        assert s["virtualRealityTag"] == "Virtual Reality"

    def test_blank_ar_tag_is_kept_blank(self):
        # Specifically: users can clear AR to opt out of AR processing.
        s = vpf.load_settings(fake_stash({"augmentedRealityTag": ""}))
        assert s["augmentedRealityTag"] == ""

    def test_limit_zero_means_no_limit(self):
        s = vpf.load_settings(fake_stash({"limit": 0}))
        assert s["limit"] == 0

    def test_limit_positive_preserved(self):
        s = vpf.load_settings(fake_stash({"limit": 25}))
        assert s["limit"] == 25

    def test_bogus_projection_falls_back_to_equirect(self):
        s = vpf.load_settings(
            fake_stash({"defaultProjection": "panoramic-holo-field"})
        )
        assert s["defaultProjection"] == "equirect"


# ---------- scene_checksums ----------------------------------------------------


class TestSceneChecksums:
    def test_pulls_fingerprints(self):
        scene = {
            "files": [
                {
                    "fingerprints": [
                        {"type": "oshash", "value": "AAA"},
                        {"type": "md5", "value": "BBB"},
                    ]
                }
            ]
        }
        assert vpf.scene_checksums(scene) == ["AAA", "BBB"]

    def test_includes_top_level_oshash_and_checksum(self):
        scene: dict[str, Any] = {
            "files": [],
            "oshash": "HASH1",
            "checksum": "HASH2",
        }
        assert vpf.scene_checksums(scene) == ["HASH1", "HASH2"]

    def test_deduplicates(self):
        scene = {
            "files": [
                {"fingerprints": [{"type": "oshash", "value": "DUPE"}]}
            ],
            "oshash": "DUPE",
        }
        assert vpf.scene_checksums(scene) == ["DUPE"]

    def test_handles_missing_fields(self):
        assert vpf.scene_checksums({}) == []
        assert vpf.scene_checksums({"files": None}) == []


# ---------- backup + marker helpers -------------------------------------------


class TestMarkersAndDiscovery:
    def test_marker_roundtrip(self, tmp_path: Path):
        src = tmp_path / "xyz.mp4"
        src.touch()
        assert vpf.is_marked(src) is False
        vpf.write_marker(src)
        assert vpf.is_marked(src) is True
        vpf.clear_marker(src)
        assert vpf.is_marked(src) is False

    def test_clear_marker_is_idempotent(self, tmp_path: Path):
        src = tmp_path / "nothing.mp4"
        src.touch()
        # No marker exists — should not raise
        vpf.clear_marker(src)

    def test_find_preview_files_matches_any_hash(self, tmp_path: Path):
        scene = {
            "files": [
                {"fingerprints": [{"type": "oshash", "value": "HASH_A"}]}
            ],
            "checksum": "HASH_B",
        }
        # Only the MD5 file exists on disk
        (tmp_path / "HASH_B.mp4").write_bytes(b"x")
        (tmp_path / "HASH_B.webp").write_bytes(b"y")
        mp4, webp = vpf.find_preview_files(scene, str(tmp_path))
        assert mp4 == tmp_path / "HASH_B.mp4"
        assert webp == tmp_path / "HASH_B.webp"

    def test_find_preview_files_none_when_missing(self, tmp_path: Path):
        scene = {"oshash": "NOPE"}
        mp4, webp = vpf.find_preview_files(scene, str(tmp_path))
        assert mp4 is None and webp is None
