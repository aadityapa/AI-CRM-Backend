"""Recordings play in the page (8 Oct 2026): same-origin signed streaming with
HTTP Range, and ONE file with camera + screen side by side.

Run:  cd backend && python -m pytest tests/test_recording_stream.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services import interview_recording as rec
from services import media_storage as ms
from services import recording_stream as rs

BACKEND = Path(__file__).resolve().parents[1]
WEBM = rec.WEBM_MAGIC + b"cluster" * 50


@pytest.fixture()
def local_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_STORAGE_BACKEND", "local")
    monkeypatch.setenv("AUTH_SECRET", "x" * 48)
    ms.reset_storage_for_tests()
    store = ms.LocalStorage(tmp_path)
    monkeypatch.setattr(ms, "_STORAGE", store)
    yield store
    ms.reset_storage_for_tests()


def test_a_signed_url_verifies_and_a_tampered_or_expired_one_does_not(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "y" * 48)
    url = rs.stream_url("tok123", "cam", now=1_000)
    assert url.startswith("/interview/recording/tok123/file/cam?exp=")
    exp = url.split("exp=")[1].split("&")[0]
    sig = url.split("sig=")[1].split("&")[0]
    assert rs.verify("tok123", "cam", exp, sig, now=1_000)
    assert not rs.verify("tok123", "screen", exp, sig, now=1_000)       # another stream
    assert not rs.verify("tok999", "cam", exp, sig, now=1_000)          # another interview
    assert not rs.verify("tok123", "cam", exp, sig + "0", now=1_000)    # tampered
    assert not rs.verify("tok123", "cam", exp, sig, now=1_000 + rs.SIGNED_TTL_S + 5)   # expired
    assert not rs.verify("tok123", "secret", exp, sig, now=1_000)       # unknown stream


def test_range_parsing():
    assert rs.parse_range(None, 100) is None
    assert rs.parse_range("bytes=0-", 100) == (0, 99)
    assert rs.parse_range("bytes=10-19", 100) == (10, 19)
    assert rs.parse_range("bytes=-10", 100) == (90, 99)
    assert rs.parse_range("bytes=90-500", 100) == (90, 99)
    big = 10 * rs.MAX_RANGE_BYTES
    assert rs.parse_range("bytes=0-", big) == (0, rs.MAX_RANGE_BYTES - 1)    # one window per request
    with pytest.raises(ValueError):
        rs.parse_range("bytes=200-", 100)


def test_read_window_serves_a_range_of_the_final_file(local_store):
    local_store.put(rec.final_key("tokA"), WEBM, content_type="video/webm")
    w = rs.read_window("tokA", "cam", "bytes=0-3")
    assert w["partial"] and w["data"] == rec.WEBM_MAGIC and w["size"] == len(WEBM)
    whole = rs.read_window("tokA", "cam", None)
    assert not whole["partial"] and whole["data"] == WEBM
    assert rs.read_window("tokA", "screen", None) is None


def test_playback_hands_out_same_origin_urls_and_reports_the_combined_file(local_store, monkeypatch):
    local_store.put(rec.final_key("tokB"), WEBM, content_type="video/webm")
    info = rec.recording_playback("tokB")
    assert info["available"] and info["streamed"]
    assert info["url"].startswith("/interview/recording/tokB/file/cam?")
    assert info["combined"] == {"available": False, "reason": "camera_only"}
    # With a screen stream and no ffmpeg, the two separate files stay and the
    # reason says why there is no one-file version.
    local_store.put(rec.final_key("tokB", "screen"), WEBM, content_type="video/webm")
    monkeypatch.setattr(rs, "ffmpeg_path", lambda: None)
    info = rec.recording_playback("tokB")
    assert info["screen"]["url"].startswith("/interview/recording/tokB/file/screen?")
    assert info["combined"]["available"] is False and info["combined"]["reason"] == "no_ffmpeg"
    # Once the combined file exists it is what the viewer plays.
    local_store.put(rs.combined_key("tokB"), WEBM, content_type="video/webm")
    info = rec.recording_playback("tokB")
    assert info["combined"]["available"] and "/file/combined?" in info["combined"]["url"]


def test_the_combine_command_puts_the_camera_left_and_keeps_its_audio():
    cmd = rs.combine_command("ffmpeg", "cam.webm", "screen.webm", "out.webm")
    assert cmd.index("cam.webm") < cmd.index("screen.webm")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "hstack=inputs=2" in graph and graph.index("[c]") < graph.index("[s]")
    assert "0:a?" in cmd and cmd[-1] == "out.webm"


def test_the_file_route_checks_the_signature_and_answers_ranges():
    src = (BACKEND / "main.py").read_text(encoding="utf-8")
    body = src.split('@app.get("/interview/recording/{invite_token}/file/{stream}")', 1)[1].split("@app.", 1)[0]
    assert "rs.verify(token, stream, exp, sig)" in body
    assert "status_code=206" in body and "Content-Range" in body and "status_code=416" in body
