"""One place that knows where interview media lives (22 Sep 2026).

Session recordings are the first thing this platform produces that is too big
to keep in Postgres and too valuable to keep on the app server's disk. The app
server's filesystem is ephemeral on every hosted platform we deploy to — the
per-event camera snapshots that preceded recordings (15–23 Sep 2026) were
written under `data/` and erased by every redeploy, which is exactly the bug
this module exists to avoid.

So: ONE driver interface, two drivers.

  * ``S3Storage``    — production. AWS S3 (or anything S3-compatible), reached
                       with boto3. Playback is a presigned GET, so the bytes
                       never pass through the app server and no bucket is ever
                       made public.
  * ``LocalStorage`` — development, `start_app.bat`, and the automatic fallback
                       when S3 is not configured. Writes under ``data/media/``
                       and hands back an app-relative URL.

The driver is chosen ONCE, at first use, from the environment, and the choice
is logged. A missing/broken S3 config degrades to local rather than refusing to
store — losing a recording is bad, but killing a live interview because a
bucket name is wrong is worse. `storage_health()` reports which driver is in
force so Settings can show it instead of anyone having to guess.

Environment
-----------
``MEDIA_STORAGE_BACKEND``   ``s3`` | ``local`` | ``auto`` (default ``auto``:
                            S3 when a bucket is named, else local)
``MEDIA_S3_BUCKET``         bucket name (also accepts ``AWS_S3_BUCKET``)
``MEDIA_S3_PREFIX``         key prefix, default ``karnex/interviews``
``MEDIA_S3_REGION``         region (also accepts ``AWS_REGION``)
``MEDIA_S3_ENDPOINT_URL``   optional, for S3-compatible providers
``MEDIA_S3_STORAGE_CLASS``  default ``STANDARD``; ``INTELLIGENT_TIERING`` is
                            the cheapest set-and-forget choice for this data
``MEDIA_URL_TTL_S``         presigned URL lifetime, default 3600, max 86400

Credentials are read by boto3 itself — an instance role, ``~/.aws/credentials``
or ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY``. This module never reads,
logs or stores a secret of its own.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("karnex.media")

#: Where the local driver writes. Sits beside the existing `data/` folders.
LOCAL_MEDIA_DIR = Path(__file__).resolve().parents[1] / "data" / "media"

#: Keys are built by this module, never by a request. A caller supplies parts;
#: every part is squeezed through this before it reaches a filesystem or a
#: bucket, so no caller can walk out of the prefix with `../`.
_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]")
#: Collapses "..", "..." etc. See safe_key_part.
_DOT_RUN = re.compile(r"\.{2,}")

DEFAULT_PREFIX = "karnex/interviews"
DEFAULT_URL_TTL_S = 3600
MAX_URL_TTL_S = 86400


def safe_key_part(value: str, *, limit: int = 80) -> str:
    """Reduce one path segment to characters that are safe everywhere.

    Separators are dropped rather than replaced, so a segment can never become
    two. Runs of dots are collapsed to one as well: stripping the slashes out
    of ``a/../b`` already makes ``a..b`` harmless, but a key with ``..`` in it
    reads like a traversal to the next person to look at it, and that is a bad
    thing for a security-relevant helper to leave lying around.
    """
    cleaned = _SAFE_PART.sub("", str(value or "").strip())
    cleaned = _DOT_RUN.sub(".", cleaned)
    cleaned = cleaned.strip("._-")
    return cleaned[:limit]


def build_key(*parts: str) -> str:
    """Join sanitised parts into a storage key. Empty parts are dropped."""
    safe = [safe_key_part(p) for p in parts]
    return "/".join(p for p in safe if p)


@dataclass(frozen=True)
class StoredObject:
    """What a driver returns after a successful write."""

    key: str
    size_bytes: int
    backend: str


class MediaStorage(Protocol):
    """The whole contract. Anything implementing this can back the feature."""

    name: str

    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject: ...

    def append(self, key: str, data: bytes, *, content_type: str) -> StoredObject: ...

    def get(self, key: str) -> bytes: ...

    def size(self, key: str) -> int: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def url(self, key: str, *, ttl_s: int = DEFAULT_URL_TTL_S, download_name: str = "") -> str: ...


# --------------------------------------------------------------------------
# Local disk
# --------------------------------------------------------------------------


class LocalStorage:
    """Files under ``data/media``. Development and the no-S3 fallback.

    ``url()`` returns an app-relative path, not an absolute one: the candidate
    runtime and the dashboard are served from the same origin as the API, and
    hard-coding a host here is how a link ends up pointing at localhost in a
    production email.
    """

    name = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or LOCAL_MEDIA_DIR)

    def _path(self, key: str) -> Path:
        # `key` is already built from safe_key_part, but resolve-and-check
        # anyway: this is the one place a traversal would become a file write.
        target = (self.root / key).resolve()
        root = self.root.resolve()
        if root != target and root not in target.parents:
            raise ValueError("media key escapes the storage root")
        return target

    def put(self, key: str, data: bytes, *, content_type: str = "") -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(key=key, size_bytes=len(data), backend=self.name)

    def append(self, key: str, data: bytes, *, content_type: str = "") -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as handle:
            handle.write(data)
        return StoredObject(key=key, size_bytes=path.stat().st_size, backend=self.name)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def get_head(self, key: str, n: int) -> bytes:
        """The first `n` bytes — enough to check a file's magic without
        reading a 20 MB recording (8 Oct 2026)."""
        with self._path(key).open("rb") as handle:
            return handle.read(max(0, int(n)))

    def read_range(self, key: str, start: int, end: int) -> bytes:
        """Bytes ``start..end`` inclusive — what an HTTP Range request needs
        to stream a recording through the app (8 Oct 2026)."""
        start = max(0, int(start))
        with self._path(key).open("rb") as handle:
            handle.seek(start)
            return handle.read(max(0, int(end) - start + 1))

    def size(self, key: str) -> int:
        try:
            return self._path(key).stat().st_size
        except (OSError, ValueError):
            return 0

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except (OSError, ValueError):
            return False

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except (OSError, ValueError):
            return

    def url(self, key: str, *, ttl_s: int = DEFAULT_URL_TTL_S, download_name: str = "") -> str:
        return f"/interview/media/{key}"


# --------------------------------------------------------------------------
# S3
# --------------------------------------------------------------------------


class S3Storage:
    """AWS S3 (or S3-compatible) via boto3.

    ``append`` is the interesting one. S3 objects are immutable, and a
    multipart upload cannot take a part smaller than 5 MB except as the last
    one — our recording chunks are ~120 KB. So a recording is stored as one
    object PER CHUNK under ``<key>/parts/NNNNNN.webm`` while the interview
    runs, and `finalize()` concatenates them into the single playable object at
    ``<key>``. That also means a crashed interview still leaves every chunk it
    managed to upload, which is exactly what a proctoring artefact should do.
    """

    name = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = DEFAULT_PREFIX,
        region: str = "",
        endpoint_url: str = "",
        storage_class: str = "STANDARD",
    ) -> None:
        import boto3  # imported here so the module loads without boto3 present

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.storage_class = storage_class or "STANDARD"
        kwargs: dict = {}
        if region:
            kwargs["region_name"] = region
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self._client = boto3.client("s3", **kwargs)

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> StoredObject:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._full(key),
            Body=data,
            ContentType=content_type or "application/octet-stream",
            StorageClass=self.storage_class,
        )
        return StoredObject(key=key, size_bytes=len(data), backend=self.name)

    def append(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> StoredObject:
        """Read-modify-write. Only used by the local-style callers; the
        recording path uses `put` per chunk plus `concat`, which is why this
        stays simple rather than clever."""
        existing = b""
        if self.exists(key):
            existing = self.get(key)
        return self.put(key, existing + data, content_type=content_type)

    def concat(self, key: str, part_keys: list[str], *, content_type: str) -> StoredObject:
        """Join already-uploaded parts into one object, in the given order."""
        blob = b"".join(self.get(pk) for pk in part_keys)
        return self.put(key, blob, content_type=content_type)

    def get(self, key: str) -> bytes:
        obj = self._client.get_object(Bucket=self.bucket, Key=self._full(key))
        return obj["Body"].read()

    def get_head(self, key: str, n: int) -> bytes:
        """The first `n` bytes via a Range request (8 Oct 2026)."""
        n = max(1, int(n))
        obj = self._client.get_object(Bucket=self.bucket, Key=self._full(key), Range=f"bytes=0-{n - 1}")
        return obj["Body"].read()

    def read_range(self, key: str, start: int, end: int) -> bytes:
        """Bytes ``start..end`` inclusive via a Range request (8 Oct 2026)."""
        start = max(0, int(start))
        obj = self._client.get_object(Bucket=self.bucket, Key=self._full(key),
                                      Range=f"bytes={start}-{max(start, int(end))}")
        return obj["Body"].read()

    def size(self, key: str) -> int:
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=self._full(key))
            return int(head.get("ContentLength") or 0)
        except Exception:
            return 0

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._full(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._full(key))
        except Exception:
            logger.warning("media.s3.delete_failed", extra={"event": "media.s3.delete_failed"})

    def list_keys(self, prefix: str) -> list[str]:
        """Keys under a sub-prefix, sorted. Used to gather recording parts."""
        out: list[str] = []
        token = ""
        base = self._full(prefix)
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": base, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for item in resp.get("Contents") or []:
                full = str(item.get("Key") or "")
                out.append(full[len(self.prefix) + 1:] if self.prefix else full)
            if not resp.get("IsTruncated"):
                break
            token = str(resp.get("NextContinuationToken") or "")
            if not token:
                break
        return sorted(out)

    def url(self, key: str, *, ttl_s: int = DEFAULT_URL_TTL_S, download_name: str = "") -> str:
        params: dict = {"Bucket": self.bucket, "Key": self._full(key)}
        if download_name:
            params["ResponseContentDisposition"] = f'attachment; filename="{safe_key_part(download_name, limit=120)}"'
        return self._client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=max(60, min(int(ttl_s or DEFAULT_URL_TTL_S), MAX_URL_TTL_S))
        )


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

_STORAGE: MediaStorage | None = None
_STORAGE_LOCK = threading.Lock()
_STORAGE_NOTE = ""


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return default


def url_ttl_seconds() -> int:
    try:
        raw = int(_env("MEDIA_URL_TTL_S", default=str(DEFAULT_URL_TTL_S)))
    except (TypeError, ValueError):
        raw = DEFAULT_URL_TTL_S
    return max(60, min(raw, MAX_URL_TTL_S))


def _build_storage() -> tuple[MediaStorage, str]:
    backend = _env("MEDIA_STORAGE_BACKEND", default="auto").lower()
    bucket = _env("MEDIA_S3_BUCKET", "AWS_S3_BUCKET")
    if backend == "local":
        return LocalStorage(), "local (MEDIA_STORAGE_BACKEND=local)"
    if backend in {"s3", "auto"} and bucket:
        try:
            store = S3Storage(
                bucket,
                prefix=_env("MEDIA_S3_PREFIX", default=DEFAULT_PREFIX),
                region=_env("MEDIA_S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
                endpoint_url=_env("MEDIA_S3_ENDPOINT_URL"),
                storage_class=_env("MEDIA_S3_STORAGE_CLASS", default="STANDARD"),
            )
            return store, f"s3 (bucket {bucket})"
        except Exception as exc:
            # Deliberate: a bad bucket name or a missing boto3 must not take
            # the interview down with it. Store locally, say so loudly.
            logger.error(
                "media.s3.unavailable_falling_back_to_local: %s", exc,
                extra={"event": "media.s3.unavailable"},
            )
            return LocalStorage(), f"local (S3 unavailable: {exc})"
    if backend == "s3" and not bucket:
        logger.error(
            "media.s3.no_bucket_configured",
            extra={"event": "media.s3.no_bucket"},
        )
        return LocalStorage(), "local (MEDIA_STORAGE_BACKEND=s3 but no bucket named)"
    return LocalStorage(), "local (no S3 bucket configured)"


def get_storage() -> MediaStorage:
    """The process-wide driver. Built once, on first use."""
    global _STORAGE, _STORAGE_NOTE
    if _STORAGE is not None:
        return _STORAGE
    with _STORAGE_LOCK:
        if _STORAGE is None:
            _STORAGE, _STORAGE_NOTE = _build_storage()
            logger.info(
                "media.storage.selected",
                extra={"event": "media.storage.selected", "backend": _STORAGE_NOTE},
            )
    return _STORAGE


def reset_storage_for_tests() -> None:
    """Drop the cached driver so a test can re-read the environment."""
    global _STORAGE, _STORAGE_NOTE
    with _STORAGE_LOCK:
        _STORAGE = None
        _STORAGE_NOTE = ""


def storage_health() -> dict:
    """What Settings / an operator needs to answer 'where did it go?'."""
    store = get_storage()
    return {
        "backend": store.name,
        "detail": _STORAGE_NOTE,
        "bucket": getattr(store, "bucket", ""),
        "prefix": getattr(store, "prefix", ""),
        "storage_class": getattr(store, "storage_class", ""),
        "url_ttl_s": url_ttl_seconds(),
        "durable": store.name != "local",
    }
