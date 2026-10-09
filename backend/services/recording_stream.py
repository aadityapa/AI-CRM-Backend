"""Playing a finished recording in the page, and ONE file with both halves
(8 Oct 2026).

Why the page showed a black frame at 0:00 while the downloaded file played
--------------------------------------------------------------------------
The viewer used to load the camera / screen files from wherever the storage
driver said: a presigned S3 URL straight into ``<video src>``, or (local
driver) the whole file fetched into a blob first. Both are fragile in the
page — the app's Content-Security-Policy only allows ``media-src 'self'
blob:`` (an S3 origin is refused, silently: the element just stays black),
and a 20 MB blob must download completely before the first frame. A
download is a navigation, which CSP does not govern, so the same file played
from disk.

The fix is to serve every recording through the APP, same origin, with HTTP
``Range`` support, so the browser streams and seeks it like any video:

* ``stream_url(token, stream)`` → ``/interview/recording/<token>/file/<stream>
  ?exp=…&sig=…``. A ``<video>`` element cannot send our bearer header, so the
  URL carries its own short-lived HMAC signature (``AUTH_SECRET``), handed out
  only to a reader who passed the recording gate — the same idea as an S3
  presigned URL, minus the cross-origin problem.
* ``parse_range`` (PURE) turns the ``Range`` header into a byte window; the
  drivers' ``read_range`` read exactly that window (S3 passes the Range on).

One recording with both halves
------------------------------
``combined.webm`` = camera on the left, screen on the right, the camera's
audio — built by ffmpeg once both streams are final (``start_combined_build``,
a background thread; a storage marker stops two workers building the same
file). ffmpeg comes from PATH or the ``imageio-ffmpeg`` wheel; without either
the feature simply reports ``reason: "no_ffmpeg"`` and the two separate files
stay available. A camera-only interview needs no combined file — the camera
file already IS the whole recording.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

from services.interview_recording import RECORDING_MIME, final_key, recording_base
from services.media_storage import get_storage, safe_key_part

logger = logging.getLogger("karnex.interview.recording")

#: The files a viewer may stream. ``combined`` has no parts of its own.
FILE_STREAMS = ("cam", "screen", "combined")

#: How long a signed playback URL stays valid. Long enough for a reviewer to
#: keep the page open through a long interview; re-issued on every page load.
SIGNED_TTL_S = 6 * 3600

#: Largest window served per Range request — the browser asks again for more.
MAX_RANGE_BYTES = 2 * 1024 * 1024

#: A build marker older than this is considered abandoned (a worker died).
BUILD_STALE_S = 30 * 60

#: Ceiling for one ffmpeg run.
BUILD_TIMEOUT_S = 30 * 60


def _token(invite_token: str) -> str:
    return safe_key_part(invite_token, limit=64)


def combined_key(invite_token: str) -> str:
    return f"{recording_base(_token(invite_token))}/combined.webm"


def _marker_key(invite_token: str) -> str:
    return f"{recording_base(_token(invite_token))}/combined.building"


def key_for(invite_token: str, stream: str) -> str | None:
    """The stored object behind a playable stream, or None for an unknown one."""
    if stream == "combined":
        return combined_key(invite_token)
    if stream in ("cam", "screen"):
        return final_key(_token(invite_token), stream)
    return None


# --------------------------------------------------------------------------
# Signed same-origin URLs
# --------------------------------------------------------------------------


def _secret() -> bytes:
    from auth_secret import auth_secret

    return auth_secret().encode("utf-8")


def sign(invite_token: str, stream: str, exp: int) -> str:
    msg = f"rec|{_token(invite_token)}|{stream}|{int(exp)}".encode("utf-8")
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()[:40]


def stream_url(invite_token: str, stream: str, *, download: bool = False, now: float | None = None) -> str:
    """App-relative, signed URL that plays (or downloads) one recording file."""
    exp = int((now if now is not None else time.time()) + SIGNED_TTL_S)
    token = _token(invite_token)
    url = (f"/interview/recording/{quote(token)}/file/{stream}"
           f"?exp={exp}&sig={sign(token, stream, exp)}")
    return url + ("&download=1" if download else "")


def verify(invite_token: str, stream: str, exp: str | int, sig: str, *, now: float | None = None) -> bool:
    """True when the signature matches and has not expired. Never raises."""
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < int(now if now is not None else time.time()):
        return False
    if stream not in FILE_STREAMS:
        return False
    try:
        return hmac.compare_digest(sign(invite_token, stream, exp_i), str(sig or ""))
    except Exception:
        return False


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """PURE: ``bytes=a-b`` / ``bytes=a-`` / ``bytes=-n`` → (start, end) inclusive,
    clipped to ``MAX_RANGE_BYTES``. None = no (or an unusable) range → the
    caller serves from the start. Raises ValueError for a range past the end
    (the caller answers 416)."""
    if not header or size <= 0:
        return None
    value = header.strip().lower()
    if not value.startswith("bytes="):
        return None
    spec = value[6:].split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    first, last = spec.split("-", 1)
    try:
        if first == "":
            n = int(last)
            if n <= 0:
                return None
            start, end = max(0, size - n), size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size:
        raise ValueError("range not satisfiable")
    end = min(end, size - 1, start + MAX_RANGE_BYTES - 1)
    if end < start:
        return None
    return start, end


def read_window(invite_token: str, stream: str, range_header: str | None) -> dict | None:
    """Bytes for one request: ``{data, start, end, size, partial}`` or None
    when the file does not exist. Raises ValueError for an unsatisfiable range."""
    key = key_for(invite_token, stream)
    if not key:
        return None
    store = get_storage()
    if not store.exists(key):
        return None
    size = store.size(key)
    window = parse_range(range_header, size)
    if window is None:
        # No (usable) Range header: the whole file — a download, or a client
        # that does not ask for ranges.
        start, end, partial = 0, size - 1, False
    else:
        (start, end), partial = window, True
    reader = getattr(store, "read_range", None)
    if callable(reader):
        data = reader(key, start, end)
    else:  # pragma: no cover — every driver has read_range
        data = store.get(key)[start:end + 1]
    return {"data": data, "start": start, "end": start + len(data) - 1, "size": size, "partial": partial}


# --------------------------------------------------------------------------
# One file with both halves
# --------------------------------------------------------------------------

_BUILDING: set[str] = set()
_BUILD_LOCK = threading.Lock()
_FAILED_AT: dict[str, float] = {}

#: After a failed build, wait this long before trying again.
RETRY_AFTER_S = 3600


def ffmpeg_path() -> str | None:
    """ffmpeg from PATH, else the binary the imageio-ffmpeg wheel ships."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def combine_command(ffmpeg: str, cam: str, screen: str, out: str) -> list[str]:
    """PURE: camera left, screen right, both at 360 px high, the camera's audio,
    VP8 + Opus in WebM — tuned for speed (a proctoring artefact, not a film)."""
    graph = ("[0:v]scale=-2:360,setsar=1,fps=6[c];"
             "[1:v]scale=-2:360,setsar=1,fps=6[s];"
             "[c][s]hstack=inputs=2[v]")
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", cam, "-i", screen,
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libvpx", "-b:v", "350k", "-deadline", "realtime", "-cpu-used", "8",
        "-c:a", "libopus", "-b:a", "24k",
        "-threads", "2",
        out,
    ]


def combined_state(invite_token: str) -> dict:
    """What the viewer shows for the combined file. Never raises."""
    token = _token(invite_token)
    try:
        store = get_storage()
        if store.exists(combined_key(token)):
            return {"available": True, "size_bytes": store.size(combined_key(token))}
        if not store.exists(final_key(token, "screen")):
            return {"available": False, "reason": "camera_only"}
        if token in _BUILDING or _marker_fresh(store, token):
            return {"available": False, "building": True}
        if not ffmpeg_path():
            return {"available": False, "reason": "no_ffmpeg"}
        return {"available": False, "reason": "not_built"}
    except Exception:
        return {"available": False, "reason": "storage_error"}


def _marker_fresh(store, token: str) -> bool:
    try:
        if not store.exists(_marker_key(token)):
            return False
        raw = store.get(_marker_key(token)).decode("ascii", "ignore").strip() or "0"
        return time.time() - float(raw) < BUILD_STALE_S
    except Exception:
        return False


def start_combined_build(invite_token: str) -> bool:
    """Start building ``combined.webm`` in the background when it is missing
    and both halves exist. True when a build is running (now or already)."""
    token = _token(invite_token)
    if not token:
        return False
    state = combined_state(token)
    if state.get("available") or state.get("reason") in ("camera_only", "no_ffmpeg", "storage_error"):
        return False
    if state.get("building"):
        return True
    if time.time() - _FAILED_AT.get(token, 0) < RETRY_AFTER_S:
        return False
    with _BUILD_LOCK:
        if token in _BUILDING:
            return True
        _BUILDING.add(token)
    threading.Thread(target=_build_safely, args=(token,), daemon=True,
                     name=f"rec-combine-{token[:8]}").start()
    return True


def _build_safely(token: str) -> None:
    try:
        if not build_combined(token):
            _FAILED_AT[token] = time.time()
    except Exception as exc:  # pragma: no cover — logged, never raised
        _FAILED_AT[token] = time.time()
        logger.warning("recording.combine_failed: %s", exc, extra={"event": "recording.combine_failed"})
    finally:
        with _BUILD_LOCK:
            _BUILDING.discard(token)


def build_combined(invite_token: str) -> bool:
    """Download both halves, run ffmpeg, store ``combined.webm``. Blocking."""
    token = _token(invite_token)
    ffmpeg = ffmpeg_path()
    store = get_storage()
    cam_key, screen_key = final_key(token, "cam"), final_key(token, "screen")
    if not ffmpeg or not store.exists(cam_key) or not store.exists(screen_key):
        return False
    try:
        store.put(_marker_key(token), str(time.time()).encode("ascii"), content_type="text/plain")
    except Exception:
        pass
    try:
        with tempfile.TemporaryDirectory(prefix="karnex-rec-") as tmp:
            cam = Path(tmp) / "cam.webm"
            screen = Path(tmp) / "screen.webm"
            out = Path(tmp) / "combined.webm"
            cam.write_bytes(store.get(cam_key))
            screen.write_bytes(store.get(screen_key))
            started = time.time()
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                combine_command(ffmpeg, str(cam), str(screen), str(out)),
                capture_output=True, timeout=BUILD_TIMEOUT_S, check=False,
            )
            if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
                logger.warning(
                    "recording.combine_ffmpeg_failed",
                    extra={"event": "recording.combine_ffmpeg_failed", "code": proc.returncode,
                           "stderr": (proc.stderr or b"")[-500:].decode("utf-8", "ignore")},
                )
                return False
            store.put(combined_key(token), out.read_bytes(), content_type=RECORDING_MIME)
            logger.info(
                "recording.combined",
                extra={"event": "recording.combined", "bytes": out.stat().st_size,
                       "seconds": round(time.time() - started, 1)},
            )
            return True
    finally:
        try:
            store.delete(_marker_key(token))
        except Exception:
            pass


def playback_block(invite_token: str, stream: str, size_bytes: int, download_name: str) -> dict:
    """The ``{available, url, download_url, size_bytes}`` shape of one file."""
    return {
        "available": True,
        "url": stream_url(invite_token, stream),
        "download_url": stream_url(invite_token, stream, download=True),
        "download_name": download_name,
        "size_bytes": int(size_bytes or 0),
        "mime": RECORDING_MIME,
    }


def download_disposition(invite_token: str, stream: str) -> str:
    suffix = {"cam": "", "screen": "-screen", "combined": "-full"}.get(stream, "")
    return f'attachment; filename="interview-{_token(invite_token)[:12]}{suffix}.webm"'


__all__ = [
    "FILE_STREAMS", "SIGNED_TTL_S", "MAX_RANGE_BYTES", "combined_key", "key_for", "sign", "stream_url",
    "verify", "parse_range", "read_window", "ffmpeg_path", "combine_command", "combined_state",
    "start_combined_build", "build_combined", "playback_block", "download_disposition",
]
