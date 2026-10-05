"""Content-addressed cache for immutable original assets (plan §8 Q3).

Cache key layout (plan §12.4): ``服务来源 + qa_ + 固定 sha256``::

    <cache_root>/papers/<server_key>/<paper_id>/<kind>/<sha256>[.ext]

* ``cache_root`` is configurable via the ``cache_dir`` field of
  ``~/.config/qatlas/config.yaml`` (default: the OS user cache dir,
  e.g. ``~/.cache/qatlas`` on Linux).
* ``server_key`` is a stable hash of the normalized server base URL,
  so two servers never share files.
* ``sha256`` is the *fixed* hash from the server's sources/parses
  listing — the download is verified against it before publication,
  so cache entries are immutable and safe to reuse across accounts.

Publication is atomic (``tmp + os.replace``) and concurrent downloads
of the same asset deduplicate through an ``flock``-guarded lock file:
the second process waits, then finds the already-published file.

Security rules baked in (plan §8 Q3 / §9):

* A 401 from the server is NEVER masked by serving a cached copy —
  cache hits happen only when the network is never consulted (the
  caller decides) or after a successful (2xx, hash-verified) download.
* A failed hash check leaves no partial file behind and does not
  overwrite an existing good entry.
* Revoking a token never deletes previously-downloaded originals
  (nothing here removes entries on auth errors).

Only immutable originals are cached. Mutable comment data is never
persisted here.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from qatlas.client import blockapi
from qatlas.config import ServerConfig
from qatlas.paths import user_cache_dir

# Extensions for readability only — identity is the sha256 directory
# entry, not the filename.
_KIND_EXT = {
    "pdf": ".pdf",
    "parse-json": ".json",
}

_LOCK_TIMEOUT_S = 120.0


def resolve_cache_root() -> Path:
    """Resolve the configurable cache root.

    ``cache_dir`` in config.yaml wins (absolute, or relative to the
    project root like raw_dir); unset → platformdirs user cache dir.
    """
    try:
        cfg = ServerConfig.from_env()
        raw = getattr(cfg, "cache_dir", None)
    except Exception:
        raw = None
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            from qatlas.config import get_project_root

            path = get_project_root() / path
        return path.resolve()
    return user_cache_dir()


def server_key(base_url: str) -> str:
    """Stable 16-hex key for a server origin (normalized base URL)."""
    normalized = base_url.rstrip("/").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


_QA_ID_RE = re.compile(r"^qa_[0-9a-z]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_parts(paper_id: str, sha256: str) -> None:
    """Defend the layout against path tricks from server data."""
    if not _QA_ID_RE.match(paper_id):
        raise blockapi.ApiError(
            f"refusing cache path for non-canonical paper id {paper_id!r}",
            kind="bad_content",
            exit_code=blockapi.EXIT_TRANSPORT,
        )
    if not _SHA_RE.match(sha256):
        raise blockapi.ApiError(
            f"server reported invalid sha256 {sha256!r}",
            kind="bad_content",
            exit_code=blockapi.EXIT_TRANSPORT,
        )


def asset_cache_path(
    cache_root: Path, base_url: str, paper_id: str, kind: str, sha256: str
) -> Path:
    """Compute (not create) the cache path for one immutable asset."""
    _validate_parts(paper_id, sha256)
    ext = _KIND_EXT.get(kind, "")
    return (
        cache_root
        / "papers"
        / server_key(base_url)
        / paper_id
        / kind
        / f"{sha256}{ext}"
    )


class _DirLock:
    """flock-based advisory lock with a timeout (best-effort on non-POSIX)."""

    def __init__(self, path: Path, timeout_s: float = _LOCK_TIMEOUT_S) -> None:
        self._path = path
        self._timeout_s = timeout_s
        self._fd: int | None = None

    def __enter__(self) -> "_DirLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() - start > self._timeout_s:
                    os.close(self._fd)
                    self._fd = None
                    raise TimeoutError(
                        f"cache lock {self._path} busy for >{self._timeout_s}s"
                    )
                time.sleep(0.05)

    def __exit__(self, *exc: Any) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def publish_atomic(data: bytes, final: Path) -> None:
    """Write ``data`` to ``final`` via tmp-file + atomic rename."""
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{final.name}.", suffix=".tmp", dir=str(final.parent)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, final)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def cached_download(
    args: Any,
    *,
    base_url: str,
    paper_id: str,
    kind: str,
    sha256: str,
    url_path: str,
    what: str,
    expected_content_prefixes: tuple[str, ...],
    force_refresh: bool = False,
    use_cache: bool = True,
    log: Callable[[str], None] | None = None,
) -> tuple[bytes, bool]:
    """Fetch an immutable asset with hash verification + cache reuse.

    Returns ``(data, cache_hit)``. Flow:

    1. Without ``force_refresh``, an existing verified entry is served
       immediately (no network, no auth needed — the entry is
       content-addressed and was verified when published).
    2. Otherwise download under the dedup lock; a concurrent process
       that published the same hash first lets us reuse its result.
    3. The downloaded bytes must hash to exactly ``sha256``; a mismatch
       raises and publishes nothing. An HTML/JSON masquerade is
       rejected by the content-type guard in :func:`download_bytes`.

    Auth failures (401 etc.) propagate — a cache entry exists or not,
    it is never *served* on the error path (see module docstring).
    """
    final = asset_cache_path(resolve_cache_root(), base_url, paper_id, kind, sha256)

    def _verified_read(path: Path) -> bytes | None:
        if not path.is_file():
            return None
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() == sha256:
            return data
        if log:
            log(f"cache entry failed hash check, ignoring: {path}")
        return None

    if use_cache and not force_refresh:
        cached = _verified_read(final)
        if cached is not None:
            if log:
                log(f"cache hit: {final}")
            return cached, True

    def _download() -> bytes:
        data, _resp = blockapi.download_bytes(
            args,
            url_path,
            what=what,
            expected_content_prefixes=expected_content_prefixes,
            base_url=base_url,
        )
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise blockapi.ApiError(
                f"{what}: downloaded bytes hash to {actual}, expected {sha256} — "
                "asset NOT cached and NOT emitted (server/corruption mismatch)",
                kind="bad_content",
                exit_code=blockapi.EXIT_TRANSPORT,
            )
        return data

    if not use_cache:
        return _download(), False

    lock_path = final.with_suffix(final.suffix + ".lock")
    try:
        with _DirLock(lock_path):
            # Dedup re-check under the lock: a concurrent process may
            # have published the identical asset while we waited.
            if not force_refresh:
                cached = _verified_read(final)
                if cached is not None:
                    if log:
                        log(f"cache hit (after lock): {final}")
                    return cached, True
            data = _download()
            publish_atomic(data, final)
            return data, False
    except (TimeoutError, OSError):
        # Lock unavailable (exotic platform / stuck holder): the cache
        # is an optimization, never a hard dependency — download bare.
        if log:
            log("warning: cache lock unavailable; downloading without cache")
        return _download(), False


def verified_response_to_output(
    response: Any,
    *,
    sha256: str,
    output: str | None,
    base_url: str,
    paper_id: str | None,
    force_refresh: bool = False,
    use_cache: bool = True,
    log: Callable[[str], None] | None = None,
) -> None:
    """Verify a streamed PDF before emitting any bytes, using bounded memory.

    The caller first obtains an authenticated, paper-access-gated response and
    validates its source/hash headers. Even cache hits must pass that fresh
    authorization check. Aliases receive exactly the same SHA check as qa_ ids.
    """
    if not _SHA_RE.fullmatch(sha256):
        raise blockapi.ApiError(
            f"pdf: server reported invalid sha256 {sha256!r}",
            kind="bad_content", exit_code=blockapi.EXIT_TRANSPORT,
        )
    final = None
    if use_cache and paper_id and _QA_ID_RE.fullmatch(paper_id):
        final = asset_cache_path(resolve_cache_root(), base_url, paper_id, "pdf", sha256)

    def emit(source: Any) -> None:
        source.seek(0)
        if output is None or output == "-":
            shutil.copyfileobj(source, sys.stdout.buffer, length=1 << 16)
            sys.stdout.buffer.flush()
        else:
            dest = Path(output).expanduser()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as target:
                shutil.copyfileobj(source, target, length=1 << 16)

    def cached() -> bool:
        if final is None or force_refresh or not final.is_file():
            return False
        with final.open("rb") as source:
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1 << 16), b""):
                digest.update(chunk)
            if digest.hexdigest() != sha256:
                if log:
                    log(f"cache entry failed hash check, ignoring: {final}")
                return False
            emit(source)
        if log:
            log(f"cache hit (server authorized): {final}")
        return True

    def download() -> None:
        # Never emit an unverified prefix to stdout or overwrite an output file
        # when the transfer fails/hash mismatches. Larger files spill to disk.
        with tempfile.SpooledTemporaryFile(max_size=1 << 20, mode="w+b") as spool:
            digest = hashlib.sha256()
            size = 0
            for chunk in response.iter_content(chunk_size=1 << 16):
                if chunk:
                    digest.update(chunk)
                    spool.write(chunk)
                    size += len(chunk)
            actual = digest.hexdigest()
            if actual != sha256:
                raise blockapi.ApiError(
                    f"pdf: downloaded bytes hash to {actual}, expected {sha256} — "
                    "asset NOT cached and NOT emitted (server/corruption mismatch)",
                    kind="bad_content", exit_code=blockapi.EXIT_TRANSPORT,
                )
            if final is not None:
                final.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(prefix=f".{final.name}.", suffix=".tmp", dir=final.parent)
                try:
                    spool.seek(0)
                    with os.fdopen(fd, "wb") as target:
                        shutil.copyfileobj(spool, target, length=1 << 16)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(tmp_name, final)
                except BaseException:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
            if log:
                log(f"downloaded {size} bytes (sha256 verified: {sha256})")
            emit(spool)

    try:
        if cached():
            return
        if final is None:
            download()
            return
        # A failed lock must not cause a duplicate network download/output.
        lock = _DirLock(final.with_suffix(final.suffix + ".lock"))
        try:
            lock.__enter__()
        except (TimeoutError, OSError):
            if log:
                log("warning: cache lock unavailable; downloading without cache")
            final = None
            download()
            return
        try:
            if not cached():
                download()
        finally:
            lock.__exit__()
    finally:
        response.close()


def stream_out(data: bytes, output: str | None, *, binary_stdout_ok: bool = True) -> None:
    """Write bytes to ``--output FILE`` (or ``-``/None → stdout.buffer)."""
    if output is None or output == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    dest = Path(output).expanduser()
    if dest.parent and str(dest.parent):
        dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
