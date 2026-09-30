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
