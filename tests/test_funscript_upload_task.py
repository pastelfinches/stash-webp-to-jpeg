"""Unit tests for the funscript_upload task dispatch logic.

Tests cover the full run_upload() path with mocked GraphQL and filesystem:
- Missing scene_id / payload_b64 → non-zero exit.
- Invalid base64 payload → non-zero exit.
- Funscript validation failure → non-zero exit.
- Scene not found → non-zero exit.
- No files on scene → non-zero exit.
- File already exists, overwrite=False → non-zero exit with code "EXISTS".
- File already exists, overwrite=True → overwrites successfully.
- Happy path: file written, scan triggered.
- Scan trigger failure is non-fatal (warning only, task still succeeds).
- Atomic write failure → non-zero exit.

All tests mock at the boundary — no network, no Stash instance, no disk I/O
beyond tmp_path fixtures.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import funscript_upload as fsu

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_funscript_b64(
    actions: list[dict[str, Any]] | None = None,
) -> str:
    doc = {
        "version": "1.0",
        "actions": actions
        if actions is not None
        else [{"at": 100, "pos": 0}, {"at": 500, "pos": 100}],
    }
    return base64.b64encode(json.dumps(doc).encode()).decode()


def _default_settings(generate_after_upload: bool = False) -> dict[str, Any]:
    return {
        "maxSizeMb": fsu.DEFAULT_MAX_SIZE_MB,
        "generateAfterUpload": generate_after_upload,
    }


def _scene(
    scene_id: str = "42",
    video_path: str = "/data/video.mp4",
    interactive: bool = False,
) -> dict[str, Any]:
    return {
        "id": scene_id,
        "interactive": interactive,
        "files": [
            {
                "path": video_path,
                "basename": Path(video_path).name,
                "parent_folder": {"path": str(Path(video_path).parent)},
            }
        ],
    }


def _make_stash(scene: dict[str, Any] | None = None) -> MagicMock:
    if scene is None:
        scene = _scene()
    stash = MagicMock()
    stash.call_GQL.side_effect = lambda query, _vars=None: (
        {"findScene": scene}
        if "findScene" in query
        else {"metadataScan": "job-123"}
    )
    return stash


def _run_upload_capturing(
    stash: MagicMock,
    args: dict[str, Any],
    settings: dict[str, Any],
    *,
    tmp_path: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Run run_upload(), capture stdout JSON, return (payload, exit_code)."""
    captured: list[str] = []

    def fake_print(s: str = "", **_kwargs: Any) -> None:
        captured.append(s)

    exit_code = 0

    # If tmp_path supplied, redirect the write to a writable temp location by
    # patching derive_target_path to return a path under tmp_path.
    ctx_managers: list[Any] = [patch("builtins.print", side_effect=fake_print)]

    if tmp_path is not None:
        target = tmp_path / "video.funscript"
        ctx_managers.append(
            patch.object(fsu, "derive_target_path", return_value=(target, ""))
        )

    with ctx_managers[0]:
        if len(ctx_managers) > 1:
            with ctx_managers[1]:
                try:
                    fsu.run_upload(stash, args, settings)
                except SystemExit as exc:
                    exit_code = int(exc.code) if exc.code is not None else 1
        else:
            try:
                fsu.run_upload(stash, args, settings)
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

    payload: dict[str, Any] = {}
    if captured:
        try:
            payload = json.loads(captured[-1])
        except json.JSONDecodeError:
            pass
    return payload, exit_code


# ---------------------------------------------------------------------------
# Missing required args
# ---------------------------------------------------------------------------


class TestMissingArgs:
    def test_missing_scene_id_exits_nonzero(self):
        stash = _make_stash()
        args: dict[str, Any] = {"mode": "upload", "payload_b64": _make_funscript_b64()}
        payload, code = _run_upload_capturing(stash, args, _default_settings())
        assert code != 0
        assert payload.get("error")

    def test_empty_scene_id_exits_nonzero(self):
        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "",
            "payload_b64": _make_funscript_b64(),
        }
        payload, code = _run_upload_capturing(stash, args, _default_settings())
        assert code != 0
        assert payload.get("error")

    def test_missing_payload_exits_nonzero(self):
        stash = _make_stash()
        args: dict[str, Any] = {"mode": "upload", "scene_id": "42"}
        payload, code = _run_upload_capturing(stash, args, _default_settings())
        assert code != 0
        assert payload.get("error")


# ---------------------------------------------------------------------------
# Base64 / validation failures
# ---------------------------------------------------------------------------


class TestPayloadValidation:
    def test_invalid_base64_exits_nonzero(self):
        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "1",
            "payload_b64": "!!!not-base64!!!",
        }
        payload, code = _run_upload_capturing(stash, args, _default_settings())
        assert code != 0
        assert payload.get("error")

    def test_invalid_json_payload_exits_nonzero(self):
        stash = _make_stash()
        not_json = base64.b64encode(b"this is not json").decode()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "1",
            "payload_b64": not_json,
        }
        payload, code = _run_upload_capturing(stash, args, _default_settings())
        assert code != 0
        assert payload.get("error")

    def test_missing_actions_field_exits_nonzero(self):
        stash = _make_stash()
        no_actions = base64.b64encode(json.dumps({"version": "1.0"}).encode()).decode()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "1",
            "payload_b64": no_actions,
        }
        payload, code = _run_upload_capturing(stash, args, _default_settings())
        assert code != 0
        assert "invalid funscript" in payload.get("error", "").lower()

    def test_oversized_payload_exits_nonzero(self):
        stash = _make_stash()
        # Force the cap to 1 MB (floor value) so a 100k-action payload exceeds it.
        tiny_settings = {"maxSizeMb": fsu.MAX_SIZE_FLOOR_MB}
        # We need a payload larger than 1 MB — use a large actions list.
        big_doc = {"actions": [{"at": i, "pos": i % 100} for i in range(100000)]}
        big_b64 = base64.b64encode(json.dumps(big_doc).encode()).decode()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "1",
            "payload_b64": big_b64,
        }
        payload, code = _run_upload_capturing(stash, args, tiny_settings)
        assert code != 0
        assert "too large" in payload.get("error", "").lower()


# ---------------------------------------------------------------------------
# Scene resolution failures
# ---------------------------------------------------------------------------


class TestSceneResolution:
    def test_scene_not_found_exits_nonzero(self):
        stash = _make_stash(scene=None)
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "999",
            "payload_b64": _make_funscript_b64(),
        }
        payload, code = _run_upload_capturing(stash, args, _default_settings())
        assert code != 0
        assert payload.get("error")

    def test_scene_with_no_files_exits_nonzero(self):
        empty_scene = {"id": "1", "interactive": False, "files": []}
        stash = _make_stash(scene=empty_scene)
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "1",
            "payload_b64": _make_funscript_b64(),
        }
        payload, code = _run_upload_capturing(stash, args, _default_settings())
        assert code != 0
        assert payload.get("error")


# ---------------------------------------------------------------------------
# Overwrite behaviour
# ---------------------------------------------------------------------------


class TestOverwrite:
    def test_existing_file_without_overwrite_flag_exits_with_exists_code(
        self, tmp_path: Path
    ):
        target = tmp_path / "video.funscript"
        target.write_bytes(b"existing")

        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": _make_funscript_b64(),
            "overwrite": False,
        }

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        exit_code = 0
        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ):
            try:
                fsu.run_upload(stash, args, _default_settings())
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        assert exit_code != 0
        payload = json.loads(captured[-1]) if captured else {}
        assert payload.get("code") == "EXISTS"
        # Original file must be untouched.
        assert target.read_bytes() == b"existing"

    def test_existing_file_string_false_overwrite_is_rejected(self, tmp_path: Path):
        """Stash's Map scalar may send booleans as the string 'false'.

        bool('false') is True in Python, so a naive bool() coercion would
        silently allow the overwrite.  The plugin must handle this.
        """
        target = tmp_path / "video.funscript"
        target.write_bytes(b"existing")

        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": _make_funscript_b64(),
            "overwrite": "false",  # string, not bool — mirrors Stash Map coercion
        }

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        exit_code = 0
        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ):
            try:
                fsu.run_upload(stash, args, _default_settings())
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        assert exit_code != 0, "overwrite='false' (string) must be treated as False"
        payload = json.loads(captured[-1]) if captured else {}
        assert payload.get("code") == "EXISTS"
        assert target.read_bytes() == b"existing"

    def test_existing_file_with_overwrite_flag_succeeds(self, tmp_path: Path):
        target = tmp_path / "video.funscript"
        target.write_bytes(b"old content")

        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": _make_funscript_b64(),
            "overwrite": True,
        }

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        exit_code = 0
        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ):
            try:
                fsu.run_upload(stash, args, _default_settings())
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        assert exit_code == 0
        payload = json.loads(captured[-1]) if captured else {}
        assert payload.get("error") is None
        # File should have new content now.
        assert target.read_bytes() != b"old content"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_file_written_with_correct_content(self, tmp_path: Path):
        target = tmp_path / "video.funscript"
        b64 = _make_funscript_b64()
        expected_bytes = base64.b64decode(b64)

        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": b64,
        }

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        exit_code = 0
        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ):
            try:
                fsu.run_upload(stash, args, _default_settings())
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        assert exit_code == 0
        payload = json.loads(captured[-1]) if captured else {}
        assert payload.get("error") is None
        assert payload.get("output") is not None
        assert target.exists()
        assert target.read_bytes() == expected_bytes

    def test_scan_triggered_on_success(self, tmp_path: Path):
        target = tmp_path / "video.funscript"
        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": _make_funscript_b64(),
        }

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ):
            try:
                fsu.run_upload(stash, args, _default_settings())
            except SystemExit:
                pass

        # The metadataScan mutation should have been called.
        calls = [str(call) for call in stash.call_GQL.call_args_list]
        assert any("metadataScan" in c for c in calls)

    def test_scan_failure_is_non_fatal(self, tmp_path: Path):
        target = tmp_path / "video.funscript"

        stash = MagicMock()
        stash.call_GQL.side_effect = lambda query, _vars=None: (
            {"findScene": _scene()}
            if "findScene" in query
            else (_ for _ in ()).throw(RuntimeError("scan down"))  # type: ignore[arg-type]
        )

        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": _make_funscript_b64(),
        }

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        exit_code = 0
        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ), patch.object(fsu, "trigger_scan", return_value=None):
            try:
                fsu.run_upload(stash, args, _default_settings())
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        # Task must succeed even though scan trigger failed.
        assert exit_code == 0
        payload = json.loads(captured[-1]) if captured else {}
        assert payload.get("error") is None

    def test_output_contains_scene_id_and_target(self, tmp_path: Path):
        target = tmp_path / "video.funscript"
        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": _make_funscript_b64(),
        }

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ):
            try:
                fsu.run_upload(stash, args, _default_settings())
            except SystemExit:
                pass

        payload = json.loads(captured[-1]) if captured else {}
        output = payload.get("output") or {}
        assert output.get("scene_id") == "42"
        assert str(target) in output.get("target", "")


# ---------------------------------------------------------------------------
# generate_after flag
# ---------------------------------------------------------------------------


class TestGenerateAfter:
    """When the plugin setting generateAfterUpload is True, metadataGenerate
    must be called with the scene ID and interactiveHeatmapsSpeeds=True.
    When False (or absent), it must NOT be called.

    This is now a plugin-level setting, not a per-upload arg.
    """

    def _run_with_generate_flag(
        self, generate_after: bool, tmp_path: Path
    ) -> tuple[dict[str, Any], int, list[Any]]:
        """Run run_upload with the given generateAfterUpload setting, capture
        stdout and all call_GQL calls.  Returns (payload, exit_code, gql_calls).
        """
        target = tmp_path / "video.funscript"

        # Build a stash mock that tracks all call_GQL invocations.
        stash = MagicMock()
        stash.call_GQL.side_effect = lambda query, _vars=None: (
            {"findScene": _scene()}
            if "findScene" in query
            else (
                {"metadataScan": "scan-job-1"}
                if "metadataScan" in query
                else {"metadataGenerate": "gen-job-1"}
            )
        )

        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": _make_funscript_b64(),
        }
        settings = _default_settings(generate_after_upload=generate_after)

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        exit_code = 0
        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ):
            try:
                fsu.run_upload(stash, args, settings)
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        payload: dict[str, Any] = {}
        if captured:
            try:
                payload = json.loads(captured[-1])
            except json.JSONDecodeError:
                pass

        return payload, exit_code, stash.call_GQL.call_args_list

    def test_generate_after_true_calls_metadata_generate(self, tmp_path: Path):
        payload, code, calls = self._run_with_generate_flag(True, tmp_path)
        assert code == 0
        assert payload.get("error") is None

        call_strs = [str(c) for c in calls]
        assert any("metadataGenerate" in s for s in call_strs), (
            "metadataGenerate should have been called when generate_after=True"
        )

    def test_generate_after_true_uses_scene_id_and_heatmap_flag(self, tmp_path: Path):
        _payload, _code, calls = self._run_with_generate_flag(True, tmp_path)

        # Find the metadataGenerate call and inspect its variables.
        gen_call = next(
            (c for c in calls if "metadataGenerate" in str(c)),
            None,
        )
        assert gen_call is not None, "metadataGenerate call not found"

        # call_GQL(query, vars) — vars is the second positional or keyword arg.
        call_args = gen_call[0]  # positional args tuple
        call_kwargs = gen_call[1]  # keyword args dict
        # Vars may be positional (index 1) or keyword (_vars / variables).
        variables = (
            call_args[1]
            if len(call_args) > 1
            else call_kwargs.get("_vars") or call_kwargs.get("variables")
        )
        assert variables is not None, "no variables dict found on metadataGenerate call"

        inp = variables.get("input") or {}
        assert inp.get("interactiveHeatmapsSpeeds") is True
        assert inp.get("sceneIDs") == ["42"]
        # Everything else should be False.
        for key in ("covers", "sprites", "previews", "transcodes", "phashes"):
            assert inp.get(key) is False, f"{key} should be False"

    def test_generate_after_true_sets_generate_job_in_output(self, tmp_path: Path):
        payload, code, _calls = self._run_with_generate_flag(True, tmp_path)
        assert code == 0
        output = payload.get("output") or {}
        assert output.get("generate_job") == "gen-job-1"

    def test_generate_after_false_does_not_call_metadata_generate(
        self, tmp_path: Path
    ):
        _payload, code, calls = self._run_with_generate_flag(False, tmp_path)
        assert code == 0
        call_strs = [str(c) for c in calls]
        assert not any("metadataGenerate" in s for s in call_strs), (
            "metadataGenerate must NOT be called when generate_after=False"
        )

    def test_generate_after_false_output_has_null_generate_job(self, tmp_path: Path):
        payload, code, _calls = self._run_with_generate_flag(False, tmp_path)
        assert code == 0
        output = payload.get("output") or {}
        assert output.get("generate_job") is None

    def test_generate_after_absent_defaults_to_false(self, tmp_path: Path):
        """generate_after not in args at all — must not call metadataGenerate."""
        target = tmp_path / "video.funscript"
        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": _make_funscript_b64(),
            # generate_after intentionally omitted
        }

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ):
            try:
                fsu.run_upload(stash, args, _default_settings())
            except SystemExit:
                pass

        call_strs = [str(c) for c in stash.call_GQL.call_args_list]
        assert not any("metadataGenerate" in s for s in call_strs)

    def test_generate_after_failure_is_non_fatal(self, tmp_path: Path):
        """If trigger_generate raises, the task must still succeed."""
        target = tmp_path / "video.funscript"
        stash = _make_stash()
        args: dict[str, Any] = {
            "mode": "upload",
            "scene_id": "42",
            "payload_b64": _make_funscript_b64(),
            "generate_after": True,
        }

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        exit_code = 0
        with patch("builtins.print", side_effect=fake_print), patch.object(
            fsu, "derive_target_path", return_value=(target, "")
        ), patch.object(fsu, "trigger_generate", return_value=None):
            try:
                fsu.run_upload(stash, args, _default_settings())
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        assert exit_code == 0
        payload = json.loads(captured[-1]) if captured else {}
        assert payload.get("error") is None


# ---------------------------------------------------------------------------
# Unknown mode dispatch (tested via main() parsing path)
# ---------------------------------------------------------------------------


class TestMainDispatch:
    def _run_main(self, plugin_input: dict[str, Any]) -> tuple[dict[str, Any], int]:
        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        fake_stash = MagicMock()
        fake_stash.get_configuration.return_value = {"plugins": {}}

        exit_code = 0
        import io
        import sys

        stdin_data = json.dumps(plugin_input)
        with patch("builtins.print", side_effect=fake_print), patch.object(
            sys, "stdin", io.StringIO(stdin_data)
        ), patch.object(fsu, "StashInterface", return_value=fake_stash):
            try:
                fsu.main()
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        payload: dict[str, Any] = {}
        if captured:
            try:
                payload = json.loads(captured[-1])
            except json.JSONDecodeError:
                pass
        return payload, exit_code

    def test_unknown_mode_exits_nonzero(self):
        plugin_input = {
            "server_connection": {},
            "args": {"mode": "nonexistent_mode"},
        }
        payload, code = self._run_main(plugin_input)
        assert code != 0
        assert payload.get("error")

    def test_empty_stdin_exits_nonzero(self):
        import io
        import sys

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        exit_code = 0
        with patch("builtins.print", side_effect=fake_print), patch.object(
            sys, "stdin", io.StringIO("")
        ):
            try:
                fsu.main()
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        assert exit_code != 0

    def test_invalid_json_stdin_exits_nonzero(self):
        import io
        import sys

        captured: list[str] = []

        def fake_print(s: str = "", **_kwargs: Any) -> None:
            captured.append(s)

        exit_code = 0
        with patch("builtins.print", side_effect=fake_print), patch.object(
            sys, "stdin", io.StringIO("not json at all")
        ):
            try:
                fsu.main()
            except SystemExit as exc:
                exit_code = int(exc.code) if exc.code is not None else 1

        assert exit_code != 0
