"""Unit tests for the WEBP→JPEG plugin's pure functions.

These run fast, without a Stash instance, and exercise:
- magic-byte detection of WEBP vs non-WEBP
- round-trip WEBP→JPEG conversion produces a valid data URL
- data URL is proper base64 of a JPEG file
"""

from __future__ import annotations

import base64
import io
import struct

import pytest
import webp_to_jpeg as w2j
from PIL import Image


def make_webp_bytes(size=(16, 16), color=(200, 100, 50)) -> bytes:
    """Return valid WEBP bytes of a solid-color image."""
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=80)
    return buf.getvalue()


def make_jpeg_bytes(size=(16, 16), color=(200, 100, 50)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def make_png_bytes(size=(16, 16), color=(200, 100, 50)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestIsWebp:
    def test_detects_valid_webp(self):
        assert w2j.is_webp(make_webp_bytes()) is True

    def test_rejects_jpeg(self):
        assert w2j.is_webp(make_jpeg_bytes()) is False

    def test_rejects_png(self):
        assert w2j.is_webp(make_png_bytes()) is False

    def test_rejects_empty(self):
        assert w2j.is_webp(b"") is False

    def test_rejects_short_payload(self):
        # RIFF present but too short to contain the WEBP marker
        assert w2j.is_webp(b"RIFF\x00\x00\x00\x00") is False

    def test_rejects_riff_non_webp(self):
        # Valid RIFF container of a different type (e.g. WAVE)
        payload = b"RIFF" + struct.pack("<I", 0) + b"WAVE"
        assert w2j.is_webp(payload) is False

    def test_accepts_minimal_webp_header(self):
        # Smallest possible structure that passes the magic check,
        # even without a full VP8 payload.
        payload = b"RIFF\x00\x00\x00\x00WEBP"
        assert w2j.is_webp(payload) is True


class TestDataUrlConversion:
    def test_returns_data_url_with_jpeg_mime(self):
        url = w2j.webp_bytes_to_jpeg_data_url(make_webp_bytes(), quality=80)
        assert url.startswith("data:image/jpeg;base64,")

    def test_body_is_valid_base64_jpeg(self):
        url = w2j.webp_bytes_to_jpeg_data_url(make_webp_bytes(), quality=80)
        _, _, body = url.partition(",")
        raw = base64.b64decode(body)
        # JPEG SOI marker
        assert raw.startswith(b"\xff\xd8\xff")
        # Pillow can reopen it
        img = Image.open(io.BytesIO(raw))
        img.verify()

    def test_quality_setting_affects_size(self):
        webp = make_webp_bytes(size=(256, 256), color=(120, 140, 160))
        high = w2j.webp_bytes_to_jpeg_data_url(webp, quality=95)
        low = w2j.webp_bytes_to_jpeg_data_url(webp, quality=20)
        # Higher quality should produce at least as many bytes.
        assert len(high) >= len(low)

    def test_rejects_non_webp_input(self):
        with pytest.raises(Exception):
            w2j.webp_bytes_to_jpeg_data_url(b"not an image", quality=80)


class TestAuthHeaders:
    def test_includes_api_key(self):
        headers = w2j.auth_headers({"ApiKey": "xyz"})
        assert headers["ApiKey"] == "xyz"

    def test_includes_session_cookie(self):
        headers = w2j.auth_headers(
            {"SessionCookie": {"Name": "session", "Value": "abc123"}}
        )
        assert headers["Cookie"] == "session=abc123"

    def test_defaults_cookie_name(self):
        headers = w2j.auth_headers({"SessionCookie": {"Value": "abc"}})
        assert headers["Cookie"] == "session=abc"

    def test_empty_conn_yields_empty_headers(self):
        assert w2j.auth_headers({}) == {}

    def test_ignores_empty_cookie_value(self):
        headers = w2j.auth_headers({"SessionCookie": {"Value": ""}})
        assert "Cookie" not in headers


class TestBuildCoverUrl:
    def test_default_scheme_and_host(self):
        url = w2j.build_cover_url({}, "42")
        assert url == "http://localhost:9999/scene/42/screenshot"

    def test_honors_conn_fields(self):
        conn = {"Scheme": "https", "Host": "stash.example.com", "Port": 443}
        url = w2j.build_cover_url(conn, "7")
        assert url == "https://stash.example.com:443/scene/7/screenshot"
