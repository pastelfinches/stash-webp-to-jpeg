"""Unit tests for the funscript_upload plugin's pure functions.

Tests cover:
- validate_funscript: good funscript, bad JSON, missing actions, wrong type,
  empty actions, missing at/pos keys, oversized payload.
- derive_target_path: normal case, no files, no path, path traversal check.
- atomic_write: data lands at target; temp file cleaned up on success.
- load_settings: defaults, coercion, clamping.

No network, no Stash instance.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import funscript_upload as fsu
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_funscript(
    actions: list[Any] | None = None,
    extra_keys: dict[str, Any] | None = None,
) -> bytes:
    doc: dict[str, Any] = {
        "version": "1.0",
        "inverted": False,
        "range": 100,
        "actions": actions
        if actions is not None
        else [{"at": 100, "pos": 0}, {"at": 500, "pos": 100}],
    }
    if extra_keys:
        doc.update(extra_keys)
    return json.dumps(doc).encode()


_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


# ---------------------------------------------------------------------------
# validate_funscript
# ---------------------------------------------------------------------------


class TestValidateFunscript:
    def test_valid_funscript_passes(self):
        ok, reason = fsu.validate_funscript(_make_funscript(), _MAX_BYTES)
        assert ok is True
        assert reason == ""

    def test_not_json_fails(self):
        ok, reason = fsu.validate_funscript(b"this is not json", _MAX_BYTES)
        assert ok is False
        assert "not valid JSON" in reason

    def test_json_array_at_root_fails(self):
        ok, reason = fsu.validate_funscript(b"[]", _MAX_BYTES)
        assert ok is False
        assert "JSON object" in reason

    def test_missing_actions_key_fails(self):
        doc = json.dumps({"version": "1.0"}).encode()
        ok, reason = fsu.validate_funscript(doc, _MAX_BYTES)
        assert ok is False
        assert "missing" in reason.lower() and "actions" in reason

    def test_actions_not_array_fails(self):
        doc = json.dumps({"actions": "not-a-list"}).encode()
        ok, reason = fsu.validate_funscript(doc, _MAX_BYTES)
        assert ok is False
        assert "array" in reason

    def test_empty_actions_fails(self):
        ok, reason = fsu.validate_funscript(_make_funscript(actions=[]), _MAX_BYTES)
        assert ok is False
        assert "empty" in reason

    def test_actions_entry_not_dict_fails(self):
        ok, reason = fsu.validate_funscript(
            _make_funscript(actions=[42, 100]), _MAX_BYTES
        )
        assert ok is False
        assert "objects" in reason

    def test_missing_at_key_fails(self):
        ok, reason = fsu.validate_funscript(
            _make_funscript(actions=[{"pos": 50}]), _MAX_BYTES
        )
        assert ok is False
        assert "at" in reason

    def test_missing_pos_key_fails(self):
        ok, reason = fsu.validate_funscript(
            _make_funscript(actions=[{"at": 100}]), _MAX_BYTES
        )
        assert ok is False
        assert "pos" in reason

    def test_oversized_payload_fails(self):
        # Cap is 1 byte — any real content will exceed it.
        ok, reason = fsu.validate_funscript(_make_funscript(), 1)
        assert ok is False
        assert "too large" in reason

    def test_exactly_at_size_cap_passes(self):
        payload = _make_funscript()
        ok, _reason = fsu.validate_funscript(payload, len(payload))
        assert ok is True

    def test_one_byte_over_size_cap_fails(self):
        payload = _make_funscript()
        ok, reason = fsu.validate_funscript(payload, len(payload) - 1)
        assert ok is False
        assert "too large" in reason

    @pytest.mark.parametrize(
        "at_val,pos_val",
        [
            (0, 0),
            (999999, 100),
            (100, 50),
        ],
    )
    def test_various_valid_at_pos_values(self, at_val: int, pos_val: int):
        ok, _ = fsu.validate_funscript(
            _make_funscript(actions=[{"at": at_val, "pos": pos_val}]), _MAX_BYTES
        )
        assert ok is True


# ---------------------------------------------------------------------------
# derive_target_path
# ---------------------------------------------------------------------------


def _scene_with_file(video_path: str) -> dict[str, Any]:
    return {
        "id": "1",
        "interactive": False,
        "files": [
            {
                "path": video_path,
                "basename": Path(video_path).name,
                "parent_folder": {"path": str(Path(video_path).parent)},
            }
        ],
    }


class TestDeriveTargetPath:
    def test_normal_mp4_yields_funscript_sibling(self):
        scene = _scene_with_file("/data/MyScene.mp4")
        target, err = fsu.derive_target_path(scene)
        assert err == ""
        assert target == Path("/data/MyScene.funscript")

    def test_mkv_extension_replaced(self):
        scene = _scene_with_file("/data/sub/dir/clip.mkv")
        target, err = fsu.derive_target_path(scene)
        assert err == ""
        assert target == Path("/data/sub/dir/clip.funscript")

    def test_no_extension_video(self):
        scene = _scene_with_file("/data/myvideo")
        target, err = fsu.derive_target_path(scene)
        assert err == ""
        assert target == Path("/data/myvideo.funscript")

    def test_no_files_returns_error(self):
        scene = {"id": "1", "interactive": False, "files": []}
        target, err = fsu.derive_target_path(scene)
        assert target is None
        assert "no associated files" in err

    def test_null_files_returns_error(self):
        scene = {"id": "1", "interactive": False, "files": None}
        target, err = fsu.derive_target_path(scene)
        assert target is None
        assert err != ""

    def test_empty_path_returns_error(self):
        scene = {
            "id": "1",
            "interactive": False,
            "files": [{"path": "", "basename": "", "parent_folder": {"path": ""}}],
        }
        target, err = fsu.derive_target_path(scene)
        assert target is None
        assert err != ""

    def test_deep_nested_path(self):
        scene = _scene_with_file("/mnt/nas/adult/studio/performer/Scene Title.mp4")
        target, err = fsu.derive_target_path(scene)
        assert err == ""
        assert target == Path(
            "/mnt/nas/adult/studio/performer/Scene Title.funscript"
        )

    def test_spaces_in_path(self):
        scene = _scene_with_file("/data/my folder/my video file.mp4")
        target, err = fsu.derive_target_path(scene)
        assert err == ""
        assert target == Path("/data/my folder/my video file.funscript")

    def test_target_stays_in_same_directory(self):
        """Regression: with_suffix() must not escape the parent directory."""
        scene = _scene_with_file("/data/video.mp4")
        target, err = fsu.derive_target_path(scene)
        assert err == ""
        assert target is not None
        assert target.parent == Path("/data")


# ---------------------------------------------------------------------------
# atomic_write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_writes_correct_content(self, tmp_path: Path):
        target = tmp_path / "output.funscript"
        data = b'{"actions":[{"at":0,"pos":0}]}'
        fsu.atomic_write(target, data)
        assert target.read_bytes() == data

    def test_temp_file_is_cleaned_up_on_success(self, tmp_path: Path):
        target = tmp_path / "output.funscript"
        fsu.atomic_write(target, b"test")
        tmp = target.with_suffix(".funscript.tmp")
        assert not tmp.exists()

    def test_overwrites_existing_file(self, tmp_path: Path):
        target = tmp_path / "output.funscript"
        target.write_bytes(b"old content")
        fsu.atomic_write(target, b"new content")
        assert target.read_bytes() == b"new content"

    def test_raises_on_unwritable_directory(self, tmp_path: Path):
        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        os.chmod(ro_dir, 0o444)
        target = ro_dir / "out.funscript"
        try:
            with pytest.raises(Exception):
                fsu.atomic_write(target, b"data")
        finally:
            os.chmod(ro_dir, 0o755)

    def test_temp_file_cleaned_on_rename_success(self, tmp_path: Path):
        target = tmp_path / "out.funscript"
        fsu.atomic_write(target, b"hello")
        tmp = target.with_suffix(".funscript.tmp")
        # No temp file should remain.
        assert not tmp.exists()


# ---------------------------------------------------------------------------
# load_settings
# ---------------------------------------------------------------------------


class TestLoadSettings:
    def _make_stash(self, plugin_config: dict[str, Any] | None = None) -> MagicMock:
        stash = MagicMock()
        plugins = {}
        if plugin_config is not None:
            plugins["funscript_upload"] = plugin_config
        stash.get_configuration.return_value = {"plugins": plugins}
        return stash

    def test_default_max_size_when_no_config(self):
        stash = self._make_stash()
        settings = fsu.load_settings(stash)
        assert settings["maxSizeMb"] == fsu.DEFAULT_MAX_SIZE_MB

    def test_user_value_respected(self):
        stash = self._make_stash({"maxSizeMb": 50})
        settings = fsu.load_settings(stash)
        assert settings["maxSizeMb"] == 50

    def test_zero_falls_back_to_default(self):
        stash = self._make_stash({"maxSizeMb": 0})
        settings = fsu.load_settings(stash)
        assert settings["maxSizeMb"] == fsu.DEFAULT_MAX_SIZE_MB

    def test_negative_falls_back_to_default(self):
        stash = self._make_stash({"maxSizeMb": -5})
        settings = fsu.load_settings(stash)
        assert settings["maxSizeMb"] == fsu.DEFAULT_MAX_SIZE_MB

    def test_above_ceil_falls_back_to_default(self):
        stash = self._make_stash({"maxSizeMb": 9999})
        settings = fsu.load_settings(stash)
        assert settings["maxSizeMb"] == fsu.DEFAULT_MAX_SIZE_MB

    def test_string_coercion(self):
        # Stash sends NUMBER settings as strings in some versions.
        stash = self._make_stash({"maxSizeMb": "30"})
        settings = fsu.load_settings(stash)
        assert settings["maxSizeMb"] == 30

    def test_invalid_string_falls_back_to_default(self):
        stash = self._make_stash({"maxSizeMb": "not-a-number"})
        settings = fsu.load_settings(stash)
        assert settings["maxSizeMb"] == fsu.DEFAULT_MAX_SIZE_MB

    def test_get_configuration_exception_returns_defaults(self):
        stash = MagicMock()
        stash.get_configuration.side_effect = RuntimeError("DB down")
        settings = fsu.load_settings(stash)
        assert settings["maxSizeMb"] == fsu.DEFAULT_MAX_SIZE_MB
