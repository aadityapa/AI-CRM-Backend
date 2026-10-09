"""Whole-session interview recordings (22 Sep 2026; live view 23 Sep 2026;
screen stream 7 Oct 2026).

A continuous audio+video recording of the candidate for the whole interview,
watchable from the Integrity tab WHILE it runs (a few seconds behind) and
replayable once it is over. It replaced the per-event camera snapshots on
23 Sep 2026 — a recording of the whole session answers every question a
still frame did, and more.

How it works
------------
The candidate's browser records with ``MediaRecorder`` and POSTs a chunk every
``CHUNK_SECONDS``. Recording client-side and uploading in pieces is the only
approach that survives the things that actually happen: a laptop lid closing, a
dropped Wi-Fi, a browser crash. A single upload at the end would lose the entire
recording of any interview that did not finish cleanly — and an interview that
did not finish cleanly is precisely the one someone wants to watch.

Each chunk becomes its own stored object under ``parts/``. While the interview
is live, the Integrity tab reads those parts as they land (``live_manifest`` +
``part_bytes``) and appends them to a MediaSource player — the first chunk
carries the WebM header, every later one is a run of clusters, so appending
them in sequence IS the stream. ``finalize`` joins them, in sequence order,
into one playable object once the session is over. A recording that was never
finalized is still finalizable afterwards from whatever parts arrived, which is
what ``finalize_from_parts`` is for (the submit path and the recovery worker
both call it).

⚠️ Never finalize a LIVE session: finalize is followed by ``discard_parts``,
which would delete the header chunk while the browser is still uploading — the
final rebuild would then start mid-stream and be unplayable. The routes in
``main.py`` check the schedule's ``session_status`` before finalizing on demand.

Two streams (7 Oct 2026)
------------------------
The camera is only half of what a reviewer wants: "which question was on the
screen while they said that?" is the other half. So the candidate's browser
also records its own SCREEN (``getDisplayMedia``, the interview tab, no audio)
as a second, independent stream — ``STREAMS`` = ``cam`` (the original keys:
``parts/`` → ``session.webm``, untouched so every recording made before this
change still plays) and ``screen`` (``parts-screen/`` → ``screen.webm``). The
two are uploaded, finalized and served side by side and the viewer plays them
in lock-step; a session whose browser could not share the screen (a phone, a
refused prompt) simply has no screen stream and the viewer shows the camera
alone. Nothing about the camera stream depends on the screen stream.

Size
----
The defaults below target roughly **22 MB for a 45-minute interview**: 320x240
at 6 fps, VP8 video at 52 kbps plus mono Opus at 12 kbps. This is a proctoring
artefact, not a portrait — it has to answer "is this the same person, alone, in
the room", and it does that at this bitrate. The numbers are served to the
client by ``recording_client_config`` so they are tuned in ONE place (and are
overridable per-deployment without a frontend build).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from services.media_storage import (
    S3Storage,
    build_key,
    get_storage,
    safe_key_part,
    url_ttl_seconds,
)

logger = logging.getLogger("karnex.interview.recording")

#: Container + codecs. WebM/VP8/Opus plays natively in every browser the
#: dashboard supports and needs no transcode on our side.
RECORDING_MIME = "video/webm"

#: Chunk cadence. Short enough that a crash loses very little, long enough that
#: a 45-minute interview is ~180 requests rather than thousands.
CHUNK_SECONDS = 15

#: Hard ceiling per chunk. At the configured bitrate a 15 s chunk is ~120 KB;
#: 4 MB is generous headroom for a burst while still refusing a junk upload.
MAX_CHUNK_BYTES = 4 * 1024 * 1024

#: Ceiling for one interview. ~22 MB is the expected size for 45 minutes, so
#: 400 MB covers a very long interview and still bounds a runaway client.
MAX_RECORDING_BYTES = 400 * 1024 * 1024

#: Refuse a chunk sequence number beyond this (guards an infinite-loop client).
MAX_CHUNKS = 4000

#: Session statuses in which the browser may still be uploading chunks. The
#: on-demand finalize must leave these alone (see the module docstring).
LIVE_SESSION_STATUSES = frozenset({"active", "verified", "pending", "scheduled"})

#: The streams one interview may carry. ``cam`` keeps the original key layout.
STREAMS = ("cam", "screen")
_STREAM_PARTS = {"cam": "parts", "screen": "parts-screen"}
_STREAM_FINAL = {"cam": "session.webm", "screen": "screen.webm"}


def normalize_stream(stream: str | None) -> str:
    """``cam`` unless the client named a known stream."""
    value = str(stream or "cam").strip().lower()
    return value if value in _STREAM_PARTS else "cam"


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    try:
        return max(low, min(int(os.getenv(name) or default), high))
    except (TypeError, ValueError):
        return default


def recording_enabled() -> bool:
    """On by default. One env flag turns the whole feature off."""
    raw = str(os.getenv("INTERVIEW_SESSION_RECORDING_ENABLED", "true")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def recording_client_config() -> dict:
    """Everything the candidate runtime needs to record at the right size."""
    return {
        "enabled": recording_enabled(),
        "mime": RECORDING_MIME,
        "chunk_seconds": _int_env("INTERVIEW_RECORDING_CHUNK_SEC", CHUNK_SECONDS, low=5, high=60),
        "video_width": _int_env("INTERVIEW_RECORDING_WIDTH", 320, low=160, high=1280),
        "video_height": _int_env("INTERVIEW_RECORDING_HEIGHT", 240, low=120, high=720),
        "frame_rate": _int_env("INTERVIEW_RECORDING_FPS", 6, low=1, high=30),
        "video_bps": _int_env("INTERVIEW_RECORDING_VIDEO_BPS", 52_000, low=16_000, high=1_000_000),
        "audio_bps": _int_env("INTERVIEW_RECORDING_AUDIO_BPS", 12_000, low=8_000, high=128_000),
        "max_chunk_bytes": MAX_CHUNK_BYTES,
        # The screen stream (7 Oct 2026): a mostly static page at a low frame
        # rate — 960x540 @ 2 fps capped at 100 kbps is readable text and ~10 MB
        # for a 45-minute interview. Off = camera only, exactly as before.
        "screen_enabled": str(os.getenv("INTERVIEW_SCREEN_RECORDING_ENABLED", "true")).strip().lower()
        not in {"0", "false", "no", "off"},
        "screen_width": _int_env("INTERVIEW_SCREEN_RECORDING_WIDTH", 960, low=320, high=1920),
        "screen_height": _int_env("INTERVIEW_SCREEN_RECORDING_HEIGHT", 540, low=180, high=1080),
        "screen_fps": _int_env("INTERVIEW_SCREEN_RECORDING_FPS", 2, low=1, high=15),
        "screen_bps": _int_env("INTERVIEW_SCREEN_RECORDING_BPS", 100_000, low=32_000, high=2_000_000),
    }


def is_live_session(session_status: str | None) -> bool:
    """True while the candidate's browser may still be sending chunks."""
    return str(session_status or "pending").strip().lower() in LIVE_SESSION_STATUSES


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

_SEQ = re.compile(r"/(\d{6})\.webm$")


def _token(invite_token: str) -> str:
    return safe_key_part(invite_token, limit=64)


def recording_base(invite_token: str) -> str:
    return build_key("recordings", _token(invite_token))


def part_key(invite_token: str, seq: int, stream: str = "cam") -> str:
    return f"{recording_base(invite_token)}/{_STREAM_PARTS[normalize_stream(stream)]}/{int(seq):06d}.webm"


def final_key(invite_token: str, stream: str = "cam") -> str:
    return f"{recording_base(invite_token)}/{_STREAM_FINAL[normalize_stream(stream)]}"


@dataclass(frozen=True)
class RecordingResult:
    key: str
    size_bytes: int
    parts: int
    backend: str
    #: False when the joined file does not start with the WebM header — the
    #: first chunk (the only one carrying it) was lost, so no player can open
    #: the file. Recorded on the schedule row as `recording_status="corrupt"`.
    valid: bool = True
    #: Parts that were APPENDED to an already-final file (see finalize).
    appended: int = 0


#: Every WebM / Matroska file starts with this EBML header id.
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"


def is_webm(blob: bytes | None) -> bool:
    """PURE: does the data begin with the EBML header a player needs?"""
    return bool(blob) and blob[:4] == WEBM_MAGIC


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def store_chunk(invite_token: str, seq: int, data: bytes, stream: str = "cam") -> int:
    """Persist one recorded chunk. Returns its size, or 0 when refused.

    Refusing is never fatal to the interview: the caller logs and carries on.
    A recording with a hole in it is worth more than a candidate whose
    interview died because a chunk was oversized.
    """
    if not recording_enabled():
        return 0
    token = _token(invite_token)
    if not token or not data:
        return 0
    if len(data) > MAX_CHUNK_BYTES:
        logger.warning(
            "recording.chunk_too_large",
            extra={"event": "recording.chunk_too_large", "bytes": len(data)},
        )
        return 0
    if seq < 0 or seq > MAX_CHUNKS:
        return 0
    get_storage().put(part_key(token, seq, stream), data, content_type=RECORDING_MIME)
    return len(data)


def _part_keys(invite_token: str, stream: str = "cam") -> list[str]:
    """Every chunk we hold for this interview's stream, in sequence order."""
    store = get_storage()
    prefix = f"{recording_base(invite_token)}/{_STREAM_PARTS[normalize_stream(stream)]}/"
    if isinstance(store, S3Storage):
        keys = store.list_keys(prefix)
    else:
        root = getattr(store, "root", None)
        if root is None:
            return []
        folder = root / prefix
        if not folder.is_dir():
            return []
        keys = [f"{prefix}{p.name}" for p in folder.iterdir() if p.is_file()]

    return sorted(keys, key=part_seq)


def part_seq(key: str) -> int:
    """The sequence number encoded in a part key (0 when it has none)."""
    found = _SEQ.search(key)
    return int(found.group(1)) if found else 0


def finalize_from_parts(invite_token: str, stream: str = "cam") -> RecordingResult | None:
    """Join the uploaded chunks of one stream into one playable object.

    Idempotent: calling it again on an already-finalized recording rebuilds the
    same object from the same parts. That matters because BOTH the normal
    submit path and the recovery worker call it, and a terminated interview can
    reach submit twice.
    """
    token = _token(invite_token)
    if not token:
        return None
    store = get_storage()
    parts = _part_keys(token, stream)
    if not parts:
        return None
    key = final_key(token, stream)
    try:
        chunks = [store.get(p) for p in parts]
    except Exception as exc:
        logger.warning(
            "recording.finalize_read_failed: %s", exc,
            extra={"event": "recording.finalize_read_failed"},
        )
        return None
    appended = 0
    blob = b"".join(chunks)
    if not is_webm(blob):
        # The parts on hand do not start with the header chunk. That happens
        # when a final file was ALREADY built and its parts discarded (the
        # server finalized first — a termination, the recovery worker, a
        # reviewer opening the page) while the browser was still uploading
        # its last slices: rebuilding from the tail alone would REPLACE a
        # playable file with one no player can open (8 Oct 2026). Only the
        # first slice carries the WebM header, and every later slice is a
        # plain continuation, so the right file is the existing one with the
        # new slices appended — then those slices are redundant and go.
        existing = None
        try:
            if store.exists(key):
                existing = store.get(key)
        except Exception:
            existing = None
        if existing and is_webm(existing):
            blob = existing + blob
            appended = len(parts)
            logger.info(
                "recording.finalize_appended",
                extra={"event": "recording.finalize_appended", "parts": len(parts),
                       "stream": normalize_stream(stream)},
            )
        else:
            logger.warning(
                "recording.header_missing",
                extra={"event": "recording.header_missing", "parts": len(parts),
                       "first_seq": part_seq(parts[0]), "stream": normalize_stream(stream)},
            )
    if len(blob) > MAX_RECORDING_BYTES:
        blob = blob[:MAX_RECORDING_BYTES]
    store.put(key, blob, content_type=RECORDING_MIME)
    if appended:
        # Appended slices must never be appended twice (a second finalize
        # before the caller's discard would do exactly that).
        for p in parts:
            try:
                store.delete(p)
            except Exception:
                pass
    valid = is_webm(blob)
    logger.info(
        "recording.finalized",
        extra={
            "event": "recording.finalized",
            "parts": len(parts),
            "bytes": len(blob),
            "backend": store.name,
            "stream": normalize_stream(stream),
            "valid": valid,
        },
    )
    return RecordingResult(key=key, size_bytes=len(blob), parts=len(parts), backend=store.name,
                           valid=valid, appended=appended)


def finalize_all(invite_token: str) -> dict[str, RecordingResult | None]:
    """`finalize_from_parts` for every stream — ``{"cam": …, "screen": …}``.
    A stream that was never uploaded is ``None``; a failure in one stream
    never stops the other (the camera must survive a broken screen stream)."""
    out: dict[str, RecordingResult | None] = {}
    for stream in STREAMS:
        try:
            out[stream] = finalize_from_parts(invite_token, stream)
        except Exception as exc:
            logger.warning(
                "recording.finalize_failed: %s", exc,
                extra={"event": "recording.finalize_failed", "stream": stream},
            )
            out[stream] = None
    return out


def discard_parts(invite_token: str, stream: str | None = None) -> int:
    """Delete the chunk objects once a final recording exists (per stream —
    every stream when ``stream`` is None).

    Called only AFTER `finalize_from_parts` succeeded, so the parts are
    redundant at that point — and leaving them doubles the storage bill.
    """
    token = _token(invite_token)
    if not token:
        return 0
    store = get_storage()
    removed = 0
    for name in (STREAMS if stream is None else (normalize_stream(stream),)):
        if not store.exists(final_key(token, name)):
            continue
        for key in _part_keys(token, name):
            store.delete(key)
            removed += 1
    return removed


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def live_manifest(invite_token: str, after_seq: int = -1, screen_after_seq: int = -1) -> dict:
    """The chunks a live viewer has not seen yet.

    Returns ``{"live": True, "parts": [{"seq", "bytes"}], "next_after"}`` —
    the viewer fetches each part through ``part_bytes`` and appends it to its
    player, then polls again with ``next_after``. ``finalized`` flips to True
    once the session object exists, which is the viewer's cue to switch to the
    ordinary player. The screen stream rides along as ``screen`` (its own
    ``parts`` / ``next_after``; ``recorded`` says whether the browser ever sent
    one), so one poll serves both players. Never raises: a storage outage is a
    paused stream, not a broken page.
    """
    token = _token(invite_token)
    if not token:
        return {"live": False, "reason": "no_token"}
    try:
        store = get_storage()
        finalized = store.exists(final_key(token))

        def _new_parts(stream: str, after: int) -> list[dict]:
            keys = [k for k in _part_keys(token, stream) if part_seq(k) > int(after)]
            return [{"seq": part_seq(k), "bytes": store.size(k)} for k in keys]

        parts = _new_parts("cam", after_seq)
        screen_parts = _new_parts("screen", screen_after_seq)
        return {
            "live": True,
            "finalized": finalized,
            "parts": parts,
            "next_after": parts[-1]["seq"] if parts else int(after_seq),
            "screen": {
                "parts": screen_parts,
                "next_after": screen_parts[-1]["seq"] if screen_parts else int(screen_after_seq),
                "recorded": bool(screen_parts) or int(screen_after_seq) >= 0,
            },
            "chunk_seconds": recording_client_config()["chunk_seconds"],
            "mime": RECORDING_MIME,
        }
    except Exception as exc:
        logger.warning(
            "recording.live_manifest_failed: %s", exc,
            extra={"event": "recording.live_manifest_failed"},
        )
        return {"live": False, "reason": "storage_error"}


def part_bytes(invite_token: str, seq: int, stream: str = "cam") -> bytes | None:
    """One uploaded chunk, or None when it does not exist."""
    token = _token(invite_token)
    if not token or seq < 0 or seq > MAX_CHUNKS:
        return None
    store = get_storage()
    key = part_key(token, seq, stream)
    if not store.exists(key):
        return None
    return store.get(key)


def _final_is_webm(store, key: str) -> bool:
    """Read the first bytes of the final object (`get_head` when the driver
    has one, else the whole object) and check the EBML magic. Never raises."""
    try:
        head = getattr(store, "get_head", None)
        data = head(key, 4) if callable(head) else store.get(key)
        return is_webm(data)
    except Exception:
        return True   # a read blip must not label a good file as corrupt


def recording_playback(invite_token: str) -> dict:
    """What the Integrity tab needs to play (or explain the absence of) a
    recording. Never raises — a storage outage renders as "unavailable", not
    as a 500 on the integrity page."""
    token = _token(invite_token)
    if not token:
        return {"available": False, "reason": "no_token"}
    try:
        # Every `url` is a SIGNED, SAME-ORIGIN streaming URL (8 Oct 2026): the
        # page's CSP refuses an S3 origin as a media source and a blob must
        # download in full before the first frame — that was the black player
        # whose downloaded file played fine. See services/recording_stream.py.
        from services import recording_stream as rs

        store = get_storage()
        key = final_key(token)
        if store.exists(key):
            size = store.size(key)

            def _download(k: str, stream: str, name: str) -> str:
                # S3 keeps its presigned download (a navigation — no CSP, no
                # bytes through the app); local downloads through the app.
                if store.name == "s3":
                    return store.url(k, ttl_s=url_ttl_seconds(), download_name=name)
                return rs.stream_url(token, stream, download=True)

            info = {
                "available": True,
                # The file starts with the WebM header? A headerless join
                # (lost first slice) is reported, never silently played black.
                "valid": _final_is_webm(store, key),
                "url": rs.stream_url(token, "cam"),
                "download_url": _download(key, "cam", f"interview-{token[:12]}-camera.webm"),
                "size_bytes": size,
                "mime": RECORDING_MIME,
                "backend": store.name,
                "streamed": True,
                "expires_in_s": rs.SIGNED_TTL_S,
                # The screen stream (7 Oct 2026) — absent when the browser
                # could not share it; the viewer then shows the camera alone.
                "screen": {"available": False},
                # ONE file with both halves (8 Oct 2026), built in the background.
                "combined": {"available": False, "reason": "camera_only"},
            }
            screen_key = final_key(token, "screen")
            if store.exists(screen_key):
                info["screen"] = {
                    "available": True,
                    "url": rs.stream_url(token, "screen"),
                    "download_url": _download(screen_key, "screen", f"interview-{token[:12]}-screen.webm"),
                    "size_bytes": store.size(screen_key),
                    "mime": RECORDING_MIME,
                }
                state = rs.combined_state(token)
                if state.get("available"):
                    info["combined"] = {
                        "available": True,
                        "url": rs.stream_url(token, "combined"),
                        "download_url": _download(rs.combined_key(token), "combined",
                                                  f"interview-{token[:12]}-full.webm"),
                        "size_bytes": state.get("size_bytes") or 0,
                        "mime": RECORDING_MIME,
                    }
                else:
                    building = rs.start_combined_build(token)
                    info["combined"] = {**state, "building": bool(building or state.get("building"))}
            return info
        pending = len(_part_keys(token))
        if pending:
            return {"available": False, "reason": "not_finalized", "parts": pending}
        return {"available": False, "reason": "not_recorded"}
    except Exception as exc:
        logger.warning(
            "recording.playback_lookup_failed: %s", exc,
            extra={"event": "recording.playback_lookup_failed"},
        )
        return {"available": False, "reason": "storage_error"}
