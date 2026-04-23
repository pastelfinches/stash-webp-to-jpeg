"""Unit tests for the hook dispatch logic in the WEBP→JPEG plugin.

Tests cover:
- Scene.Update.Post: skip when cover_image not in inputFields
- Scene.Update.Post: process when cover_image is in inputFields
- Scene.Create.Post: skip when input.cover_image is falsy
- Scene.Create.Post: process when input.cover_image is truthy
- Unknown trigger type: gracefully skip
- hookContext.id missing: exit non-zero
- dry_run respected in hook mode
- convert_failed / update_failed propagate as errors

All tests mock at the boundary — no network, no Stash instance.
"""

from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import webp_to_jpeg as w2j
from PIL import Image

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_webp_bytes(size: tuple[int, int] = (16, 16)) -> bytes:
    img = Image.new("RGB", size, (200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=80)
    return buf.getvalue()


def make_jpeg_bytes(size: tuple[int, int] = (16, 16)) -> bytes:
    img = Image.new("RGB", size, (200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _default_settings() -> dict[str, Any]:
    return {"dryRun": False, "jpegQuality": 92, "workers": 8}


def _make_stub_stash() -> MagicMock:
    stash = MagicMock()
    stash.update_scene.return_value = None
    return stash


def _run_hook_capturing_stdout(
    stash: MagicMock,
    conn: dict[str, Any],
    settings: dict[str, Any],
    args: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Run run_hook(), capture stdout JSON, and return (payload, exit_code)."""
    captured: list[str] = []

    def fake_print(s: str, **_kwargs: Any) -> None:
        captured.append(s)

    exit_code = 0
    with patch("builtins.print", side_effect=fake_print):
        try:
            w2j.run_hook(stash, conn, settings, args)
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 1

    payload: dict[str, Any] = {}
    if captured:
        payload = json.loads(captured[-1])
    return payload, exit_code


# ---------------------------------------------------------------------------
# Scene.Update.Post — filtering on inputFields
# ---------------------------------------------------------------------------


class TestUpdatePostFiltering:
    """cover_image must appear in inputFields to trigger work."""

    def _args(self, input_fields: list[str], scene_id: int = 42) -> dict[str, Any]:
        return {
            "mode": "hook",
            "hookContext": {
                "type": "Scene.Update.Post",
                "id": scene_id,
                "input": {"id": str(scene_id)},
                "inputFields": input_fields,
            },
        }

    def test_skips_when_cover_image_absent_from_input_fields(self):
        stash = _make_stub_stash()
        args = self._args(["title", "tag_ids"])
        payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code == 0
        assert "skipped" in payload.get("output", "")
        stash.update_scene.assert_not_called()

    def test_processes_when_cover_image_in_input_fields(self):
        stash = _make_stub_stash()
        args = self._args(["cover_image", "title"])
        webp = make_webp_bytes()

        with patch.object(w2j, "fetch_cover", return_value=webp):
            payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code == 0
        assert "converted" in payload.get("output", "")
        stash.update_scene.assert_called_once()

    def test_skips_empty_input_fields_list(self):
        stash = _make_stub_stash()
        args = self._args([])
        payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code == 0
        assert "skipped" in payload.get("output", "")
        stash.update_scene.assert_not_called()


# ---------------------------------------------------------------------------
# Scene.Create.Post — filtering on input.cover_image
# ---------------------------------------------------------------------------


class TestCreatePostFiltering:
    """For Create hooks, fall back to checking input.cover_image is truthy."""

    def _args(self, cover_image: Any, scene_id: int = 7) -> dict[str, Any]:
        return {
            "mode": "hook",
            "hookContext": {
                "type": "Scene.Create.Post",
                "id": scene_id,
                "input": {"id": str(scene_id), "cover_image": cover_image},
                "inputFields": [],  # may be empty for Create
            },
        }

    @pytest.mark.parametrize("cover_value", [None, "", 0, False])
    def test_skips_when_cover_image_falsy(self, cover_value: Any):
        stash = _make_stub_stash()
        args = self._args(cover_value)
        payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code == 0
        assert "skipped" in payload.get("output", "")
        stash.update_scene.assert_not_called()

    def test_processes_when_cover_image_truthy(self):
        stash = _make_stub_stash()
        # The actual value doesn't matter — we fetch via HTTP anyway.
        args = self._args("https://example.com/cover.webp")
        webp = make_webp_bytes()

        with patch.object(w2j, "fetch_cover", return_value=webp):
            payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code == 0
        assert "converted" in payload.get("output", "")
        stash.update_scene.assert_called_once()


# ---------------------------------------------------------------------------
# Non-WEBP cover — no-op
# ---------------------------------------------------------------------------


class TestNonWebpCover:
    def test_not_webp_yields_no_op_output(self):
        stash = _make_stub_stash()
        args = {
            "mode": "hook",
            "hookContext": {
                "type": "Scene.Update.Post",
                "id": 10,
                "input": {"id": "10"},
                "inputFields": ["cover_image"],
            },
        }
        jpeg = make_jpeg_bytes()

        with patch.object(w2j, "fetch_cover", return_value=jpeg):
            payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code == 0
        assert "not WEBP" in payload.get("output", "") or "nothing to do" in payload.get(
            "output", ""
        )
        stash.update_scene.assert_not_called()


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_logs_would_convert_without_uploading(self):
        stash = _make_stub_stash()
        settings = {**_default_settings(), "dryRun": True}
        args = {
            "mode": "hook",
            "hookContext": {
                "type": "Scene.Update.Post",
                "id": 99,
                "input": {"id": "99"},
                "inputFields": ["cover_image"],
            },
        }
        webp = make_webp_bytes()

        with patch.object(w2j, "fetch_cover", return_value=webp):
            payload, code = _run_hook_capturing_stdout(stash, {}, settings, args)

        assert code == 0
        assert "dry run" in payload.get("output", "").lower() or "would convert" in payload.get(
            "output", ""
        ).lower()
        stash.update_scene.assert_not_called()


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestHookErrorCases:
    def test_unknown_trigger_type_skips_gracefully(self):
        stash = _make_stub_stash()
        args = {
            "mode": "hook",
            "hookContext": {
                "type": "Studio.Update.Post",
                "id": 1,
                "input": {},
                "inputFields": [],
            },
        }
        payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code == 0
        assert "skipped" in payload.get("output", "")
        stash.update_scene.assert_not_called()

    def test_missing_scene_id_exits_nonzero(self):
        stash = _make_stub_stash()
        args = {
            "mode": "hook",
            "hookContext": {
                "type": "Scene.Update.Post",
                # 'id' intentionally omitted
                "input": {},
                "inputFields": ["cover_image"],
            },
        }
        payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code != 0
        assert payload.get("error")

    def test_fetch_failed_exits_nonzero(self):
        stash = _make_stub_stash()
        args = {
            "mode": "hook",
            "hookContext": {
                "type": "Scene.Update.Post",
                "id": 5,
                "input": {"id": "5"},
                "inputFields": ["cover_image"],
            },
        }

        # fetch_cover returns None → fetch_failed status
        with patch.object(w2j, "fetch_cover", return_value=None):
            payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code != 0
        assert payload.get("error")

    def test_update_failed_exits_nonzero(self):
        stash = _make_stub_stash()
        stash.update_scene.side_effect = RuntimeError("DB error")
        args = {
            "mode": "hook",
            "hookContext": {
                "type": "Scene.Update.Post",
                "id": 6,
                "input": {"id": "6"},
                "inputFields": ["cover_image"],
            },
        }
        webp = make_webp_bytes()

        with patch.object(w2j, "fetch_cover", return_value=webp):
            payload, code = _run_hook_capturing_stdout(stash, {}, _default_settings(), args)

        assert code != 0
        assert payload.get("error")
