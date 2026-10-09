"""Whole-session interview recording (22 Sep 2026).

Covers the storage driver, the chunk→finalize lifecycle and the rule that no
failure in any of it may reach the candidate. The S3 driver itself is not
exercised here (that would need a live bucket); what IS pinned is that the
selection logic never leaves the platform without somewhere to write.
"""

import importlib

import pytest

from services import interview_recording as rec
from services import media_storage as ms


@pytest.fixture()
def local_store(tmp_path, monkeypatch):
    """Force the local driver, rooted in a temp dir, for one test."""
    monkeypatch.setenv("MEDIA_STORAGE_BACKEND", "local")
    # Playback URLs are signed with AUTH_SECRET since 8 Oct 2026.
    monkeypatch.setenv("AUTH_SECRET", "s" * 48)
    ms.reset_storage_for_tests()
    store = ms.LocalStorage(tmp_path)
    monkeypatch.setattr(ms, "_STORAGE", store)
    yield store
    ms.reset_storage_for_tests()


# ---------------------------------------------------------------- storage ---


def test_keys_are_sanitised_so_a_token_cannot_walk_out_of_the_prefix():
    """The token reaches us from a JWT claim; it is still treated as input."""
    key = rec.part_key("../../etc/passwd", 1)
    assert ".." not in key
    assert key.startswith("recordings/")
    assert ms.build_key("a/../b", "c") == "a.b/c"


def test_local_driver_refuses_a_key_that_escapes_its_root(tmp_path):
    store = ms.LocalStorage(tmp_path)
    with pytest.raises(ValueError):
        store.put("../outside.webm", b"x", content_type="video/webm")


def test_selection_never_leaves_us_without_a_driver(monkeypatch):
    """A misconfigured bucket must degrade to local, not raise.

    Deliberate: a wrong bucket name is an operations mistake. Losing a
    recording over it is bad; refusing to run an interview over it is worse.
    """
    monkeypatch.setenv("MEDIA_STORAGE_BACKEND", "s3")
    monkeypatch.delenv("MEDIA_S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_S3_BUCKET", raising=False)
    ms.reset_storage_for_tests()
    try:
        assert ms.get_storage().name == "local"
        assert ms.storage_health()["durable"] is False
    finally:
        ms.reset_storage_for_tests()


# -------------------------------------------------------------- lifecycle ---


def test_chunks_are_joined_in_sequence_order_not_arrival_order(local_store):
    """Uploads are serialised client-side, but the join must not rely on it."""
    token = "tok-abc"
    for seq, payload in ((2, b"CCC"), (0, b"AAA"), (1, b"BBB")):
        assert rec.store_chunk(token, seq, payload) == 3
    result = rec.finalize_from_parts(token)
    assert result is not None
    assert local_store.get(result.key) == b"AAABBBCCC"
    assert result.parts == 3


def test_more_than_nine_chunks_still_order_correctly(local_store):
    """Zero-padded names, so chunk 10 must not sort between 1 and 2."""
    token = "tok-order"
    for seq in range(12):
        rec.store_chunk(token, seq, bytes([65 + seq]))
    result = rec.finalize_from_parts(token)
    assert local_store.get(result.key) == bytes(range(65, 65 + 12))


def test_finalize_is_idempotent(local_store):
    """Submit and the recovery worker both call it; a termination reaches
    submit twice."""
    token = "tok-twice"
    rec.store_chunk(token, 0, b"AAA")
    first = rec.finalize_from_parts(token)
    second = rec.finalize_from_parts(token)
    assert first.key == second.key
    assert local_store.get(second.key) == b"AAA"


def test_a_crashed_interview_still_finalizes_what_arrived(local_store):
    """The interviews worth watching are the ones that ended badly."""
    token = "tok-crash"
    rec.store_chunk(token, 0, b"AAA")
    rec.store_chunk(token, 1, b"BBB")
    # No `complete` call — the client never got to make one.
    info = rec.recording_playback(token)
    assert info["available"] is False
    assert info["reason"] == "not_finalized"
    assert info["parts"] == 2

    assert rec.finalize_from_parts(token) is not None
    after = rec.recording_playback(token)
    assert after["available"] is True
    assert after["size_bytes"] == 6


def test_parts_are_discarded_only_after_a_final_object_exists(local_store):
    token = "tok-parts"
    rec.store_chunk(token, 0, b"AAA")
    # Nothing finalized yet — discarding now would destroy the only copy.
    assert rec.discard_parts(token) == 0
    rec.finalize_from_parts(token)
    assert rec.discard_parts(token) == 1
    assert rec.recording_playback(token)["available"] is True


def test_an_oversized_chunk_is_refused_without_raising(local_store):
    """Refusing costs a gap in the recording. Raising would reach the
    candidate mid-answer, which is never acceptable."""
    assert rec.store_chunk("tok-big", 0, b"x" * (rec.MAX_CHUNK_BYTES + 1)) == 0
    assert rec.recording_playback("tok-big")["available"] is False


def test_nothing_recorded_reads_as_not_recorded_not_as_an_error(local_store):
    info = rec.recording_playback("tok-none")
    assert info == {"available": False, "reason": "not_recorded"}


def test_playback_never_raises_when_storage_is_broken(local_store, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("bucket on fire")

    monkeypatch.setattr(local_store, "exists", _boom)
    assert rec.recording_playback("tok-x") == {"available": False, "reason": "storage_error"}


def test_the_feature_can_be_switched_off_entirely(local_store, monkeypatch):
    monkeypatch.setenv("INTERVIEW_SESSION_RECORDING_ENABLED", "false")
    assert rec.recording_enabled() is False
    assert rec.store_chunk("tok-off", 0, b"AAA") == 0
    assert rec.recording_client_config()["enabled"] is False


# ------------------------------------------------------------------ sizing --


def test_the_defaults_stay_small_enough_to_be_affordable():
    """~22 MB for a 45-minute interview is the agreed target.

    If someone raises the bitrate, this is the line that says what it costs:
    at 200 interviews a month a 4x bump is 4x the S3 bill forever, because
    these are kept indefinitely.
    """
    cfg = rec.recording_client_config()
    total_bps = cfg["video_bps"] + cfg["audio_bps"]
    mb_for_45_min = total_bps * 45 * 60 / 8 / (1024 * 1024)
    assert mb_for_45_min < 30, f"{mb_for_45_min:.1f} MB for 45 minutes is above the agreed budget"
    assert cfg["video_width"] <= 320 and cfg["frame_rate"] <= 8


# ------------------------------------------------------------- live view ---


def test_live_manifest_returns_only_the_parts_the_viewer_has_not_seen(local_store):
    """The viewer polls with the last sequence it appended; nothing is resent."""
    token = "tok-live"
    for seq in range(3):
        rec.store_chunk(token, seq, b"x" * (seq + 1))
    first = rec.live_manifest(token)
    assert first["live"] is True and first["finalized"] is False
    assert [p["seq"] for p in first["parts"]] == [0, 1, 2]
    assert [p["bytes"] for p in first["parts"]] == [1, 2, 3]
    assert first["next_after"] == 2

    nothing_new = rec.live_manifest(token, after_seq=first["next_after"])
    assert nothing_new["parts"] == [] and nothing_new["next_after"] == 2

    rec.store_chunk(token, 3, b"yyyy")
    later = rec.live_manifest(token, after_seq=2)
    assert [p["seq"] for p in later["parts"]] == [3]
    assert rec.part_bytes(token, 3) == b"yyyy"
    assert rec.part_bytes(token, 99) is None


def test_live_manifest_tells_the_viewer_when_the_session_object_exists(local_store):
    """`finalized` is the viewer's cue to switch to the ordinary player."""
    token = "tok-live-done"
    rec.store_chunk(token, 0, b"AAA")
    rec.finalize_from_parts(token)
    assert rec.live_manifest(token)["finalized"] is True


def test_live_manifest_never_raises_when_storage_is_broken(local_store, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("bucket on fire")

    monkeypatch.setattr(local_store, "exists", _boom)
    assert rec.live_manifest("tok-x") == {"live": False, "reason": "storage_error"}


def test_a_live_session_is_never_finalized_on_demand(local_store, monkeypatch):
    """Finalize is followed by discard_parts, which would delete the header
    chunk from under the uploading browser. While the session is live the
    detail view must stream the parts, not join them."""
    main = importlib.import_module("main")
    rec.store_chunk("tok-open", 0, b"AAA")
    calls = []
    monkeypatch.setattr(main, "_finalize_session_recording", lambda t: calls.append(t))

    live = main._recording_detail("tok-open", "active")
    assert live["available"] is False and live["live"] is True and live["reason"] == "not_finalized"
    assert calls == []

    main._recording_detail("tok-open", "completed")
    assert calls == ["tok-open"]


def test_is_live_session_covers_every_pre_completion_status():
    for status in ("pending", "verified", "active", "scheduled", None, ""):
        assert rec.is_live_session(status) is True
    for status in ("completed", "terminated", "abandoned", "expired", "recovered"):
        assert rec.is_live_session(status) is False


# ------------------------------------------------------------------ routes --


def test_the_recording_routes_are_registered():
    """Route COUNTING is not a valid probe on this stack (FastAPI 0.141
    mounts a sub-app), so assert against the declared paths."""
    main = importlib.import_module("main")
    paths = {getattr(r, "path", "") for r in main.app.routes}
    for path in (
        "/interview/recording/chunk",
        "/interview/recording/complete",
        "/interview/recording/{invite_token}",
        "/interview/recording/{invite_token}/live",
        "/interview/recording/{invite_token}/part/{seq}",
        "/interview/recording-config",
        "/interview/media/{key:path}",
    ):
        assert path in paths, f"{path} is not registered"
    # 23 Sep 2026: per-event camera snapshots were removed — the recording is
    # the evidence. The old route must not come back by accident.
    assert "/interview/integrity-evidence/{invite_token}/{name}" not in paths


def test_recording_config_is_a_separate_path_from_the_token_route():
    """`/interview/recording-config` deliberately does NOT live under
    `/interview/recording/`, so it can never be shadowed by the parametric
    `/{invite_token}` sibling regardless of declaration order."""
    main = importlib.import_module("main")
    paths = [getattr(r, "path", "") for r in main.app.routes]
    assert "/interview/recording/config" not in paths
    assert "/interview/recording-config" in paths


# ------------------------------------------------------- screen stream (7 Oct 2026)


def test_the_screen_stream_is_stored_finalized_and_served_beside_the_camera(local_store):
    token = "tok-screen"
    assert rec.store_chunk(token, 0, b"CAM0") == 4
    assert rec.store_chunk(token, 1, b"CAM1") == 4
    assert rec.store_chunk(token, 0, b"SCR0", "screen") == 4
    # Separate key spaces: the camera keys are the ORIGINAL layout, so every
    # recording made before this change keeps playing.
    assert rec.part_key(token, 0) == rec.part_key(token, 0, "cam")
    assert rec.part_key(token, 0, "screen") != rec.part_key(token, 0)
    assert rec.final_key(token) == rec.final_key(token, "cam")
    done = rec.finalize_all(token)
    assert local_store.get(done["cam"].key) == b"CAM0CAM1"
    assert local_store.get(done["screen"].key) == b"SCR0"
    info = rec.recording_playback(token)
    assert info["available"] is True and info["size_bytes"] == 8
    assert info["screen"]["available"] is True and info["screen"]["size_bytes"] == 4
    # Both streams carry a download link (the report page offers Camera / Screen downloads).
    assert info["download_url"] and info["screen"]["download_url"]
    # Both streams' parts go once both final objects exist.
    assert rec.discard_parts(token) == 3


def test_a_recording_without_a_screen_stream_still_plays(local_store):
    """A phone, or a refused share prompt: the camera alone is the recording."""
    token = "tok-cam-only"
    rec.store_chunk(token, 0, b"CAM0")
    done = rec.finalize_all(token)
    assert done["cam"] is not None and done["screen"] is None
    info = rec.recording_playback(token)
    assert info["available"] is True
    assert info["screen"] == {"available": False}


def test_an_unknown_stream_name_falls_back_to_the_camera():
    assert rec.normalize_stream("../x") == "cam"
    assert rec.normalize_stream("SCREEN") == "screen"
    assert rec.normalize_stream(None) == "cam"


def test_live_manifest_carries_both_streams_with_their_own_cursors(local_store):
    token = "tok-live-two"
    for seq in range(3):
        rec.store_chunk(token, seq, b"c")
    rec.store_chunk(token, 0, b"s", "screen")
    m = rec.live_manifest(token, after_seq=0, screen_after_seq=-1)
    assert [p["seq"] for p in m["parts"]] == [1, 2]
    assert m["next_after"] == 2
    assert [p["seq"] for p in m["screen"]["parts"]] == [0]
    assert m["screen"]["next_after"] == 0 and m["screen"]["recorded"] is True
    quiet = rec.live_manifest("tok-nothing", after_seq=-1, screen_after_seq=-1)
    assert quiet["screen"] == {"parts": [], "next_after": -1, "recorded": False}


def test_part_bytes_reads_the_named_stream(local_store):
    token = "tok-part-stream"
    rec.store_chunk(token, 0, b"CAM", "cam")
    rec.store_chunk(token, 0, b"SCR", "screen")
    assert rec.part_bytes(token, 0) == b"CAM"
    assert rec.part_bytes(token, 0, "screen") == b"SCR"
    assert rec.part_bytes(token, 1, "screen") is None


def test_the_screen_defaults_stay_cheap():
    """~10 MB per 45-minute interview at the cap; a mostly static page is far
    less. Readable text at 960x540 is the point, not motion."""
    cfg = rec.recording_client_config()
    assert cfg["screen_enabled"] is True
    assert cfg["screen_fps"] <= 3
    assert cfg["screen_bps"] * 45 * 60 / 8 <= 40 * 1024 * 1024


def test_recording_routes_admit_report_readers(monkeypatch):
    """7 Oct 2026 — the candidate report page shows the recording, so a Reports
    reader (by role or template) may play it even without the Integrity tab."""
    import main
    src = __import__("inspect").getsource(main)
    for route in ("interview_recording_playback", "interview_recording_live",
                  "interview_recording_part", "interview_media"):
        body = src.split(f"def {route}(")[1].split("\n@app.")[0]
        assert "_recording_auth(request)" in body, route
    auth_src = __import__("inspect").getsource(main._recording_auth)
    assert "_integrity_auth(request)" in auth_src and "_require_interview_report_reader(request)" in auth_src


# ------------------------------------------- the headerless rebuild (8 Oct 2026) ---

HEAD = rec.WEBM_MAGIC + b"HEAD"


def test_late_slices_are_appended_to_a_final_file_never_rebuilt_from_the_tail(local_store):
    """The server finalized (a termination, the recovery worker, a reviewer
    opening the page) and discarded the parts while the browser was still
    uploading its last slices. Rebuilding from the tail alone used to REPLACE
    a playable file with a headerless one — a black frame at 0:00."""
    token = "tok-late"
    rec.store_chunk(token, 0, HEAD)
    rec.store_chunk(token, 1, b"BBB")
    first = rec.finalize_from_parts(token)
    assert first is not None and first.valid is True
    assert rec.discard_parts(token) == 2
    # Two slices arrive after the join; the client then calls complete.
    rec.store_chunk(token, 2, b"CCC")
    rec.store_chunk(token, 3, b"DDD")
    again = rec.finalize_from_parts(token)
    assert again is not None and again.valid is True and again.appended == 2
    assert local_store.get(again.key) == HEAD + b"BBB" + b"CCC" + b"DDD"
    # The appended slices are gone, so a third finalize cannot append them twice.
    assert rec._part_keys(token) == []
    assert rec.finalize_from_parts(token) is None
    assert rec.recording_playback(token)["valid"] is True


def test_a_join_with_no_header_is_flagged_not_hidden(local_store):
    token = "tok-nohead"
    rec.store_chunk(token, 3, b"CCC")
    rec.store_chunk(token, 4, b"DDD")
    result = rec.finalize_from_parts(token)
    assert result is not None and result.valid is False and result.appended == 0
    assert rec.recording_playback(token)["valid"] is False
    assert rec.is_webm(HEAD) and not rec.is_webm(b"CCC") and not rec.is_webm(b"")


def test_the_schedule_row_records_a_corrupt_join():
    """`_finalize_session_recording` stamps "corrupt" rather than "ready"."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    body = src.split("def _finalize_session_recording", 1)[1].split("\ndef ", 1)[0]
    assert 'recording_status="ready" if result.valid else "corrupt"' in body
