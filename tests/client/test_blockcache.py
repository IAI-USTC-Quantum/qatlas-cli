"""Unit tests for the content-addressed originals cache (plan §8 Q3)."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

import pytest

from qatlas.client import blockapi, blockcache


@dataclass
class _Cfg:
    cache_dir: str | None = None


class _StubConfig:
    """Stand-in for ServerConfig that never touches ~/.config.

    ``from_env`` returns the current :class:`_Cfg` (which exposes
    ``cache_dir`` directly, like the real pydantic model).
    """

    @classmethod
    def from_env(cls) -> _Cfg:
        cfg = _current_cfg[0]
        assert cfg is not None
        return cfg


_current_cfg: list[_Cfg] = [None]  # type: ignore[list-item]


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    _current_cfg[0] = _Cfg(cache_dir=None)
    monkeypatch.setattr(blockcache, "ServerConfig", _StubConfig)


# ---------------------------------------------------------------------------
# cache root resolution (configurable per §12.4)
# ---------------------------------------------------------------------------


def test_cache_root_defaults_to_user_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(blockcache, "user_cache_dir", lambda: tmp_path / "default")
    assert blockcache.resolve_cache_root() == tmp_path / "default"


def test_cache_root_honors_configured_absolute_dir(tmp_path):
    _current_cfg[0] = _Cfg(cache_dir=str(tmp_path / "custom"))
    assert blockcache.resolve_cache_root() == tmp_path / "custom"


def test_cache_root_relative_anchors_at_project_root():
    _current_cfg[0] = _Cfg(cache_dir="mycache")
    from qatlas.config import get_project_root

    assert (
        blockcache.resolve_cache_root()
        == (get_project_root() / "mycache").resolve()
    )


# ---------------------------------------------------------------------------
# identity: server origin + qa_ + fixed sha256
# ---------------------------------------------------------------------------


def test_server_key_normalizes_and_separates_origins():
    assert blockcache.server_key("http://a.test/") == blockcache.server_key(
        "http://a.test"
    )
    assert blockcache.server_key("http://a.test") != blockcache.server_key(
        "http://b.test"
    )
    assert blockcache.server_key("http://a.test") != blockcache.server_key(
        "https://a.test"
    )
    assert len(blockcache.server_key("http://a.test")) == 16


def test_asset_cache_path_layout_and_validation(tmp_path):
    sha = "a" * 64
    path = blockcache.asset_cache_path(
        tmp_path, "http://a.test", "qa_01h5e0aaaabbbbccccddddeeffff", "pdf", sha
    )
    assert path == (
        tmp_path / "papers" / blockcache.server_key("http://a.test")
        / "qa_01h5e0aaaabbbbccccddddeeffff" / "pdf" / f"{sha}.pdf"
    )
    with pytest.raises(blockapi.ApiError):
        blockcache.asset_cache_path(tmp_path, "http://a.test", "../evil", "pdf", sha)
    with pytest.raises(blockapi.ApiError):
        blockcache.asset_cache_path(
            tmp_path, "http://a.test", "qa_01h5e0aaaabbbbccccddddeeffff", "pdf", "zz"
        )


# ---------------------------------------------------------------------------
# atomic publish
# ---------------------------------------------------------------------------


def test_publish_atomic_leaves_no_tmp(tmp_path):
    final = tmp_path / "dir" / "file.bin"
    blockcache.publish_atomic(b"data-v1", final)
    assert final.read_bytes() == b"data-v1"
    assert list(final.parent.glob("*.tmp")) == []
    # Re-publish replaces atomically (rename over existing file).
    blockcache.publish_atomic(b"data-v2", final)
    assert final.read_bytes() == b"data-v2"
    assert list(final.parent.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# cached_download — verification, reuse, concurrency dedup
# ---------------------------------------------------------------------------


@dataclass
class _DownloadStub:
    data: bytes
    calls: int = 0
    delay: float = 0.0
    exc: Exception | None = None


def _patch_download(monkeypatch, stub: _DownloadStub):
    def fake(args, url_path, *, what, expected_content_prefixes, base_url):
        stub.calls += 1
        if stub.delay:
            time.sleep(stub.delay)
        if stub.exc:
            raise stub.exc
        return stub.data, None

    monkeypatch.setattr(blockapi, "download_bytes", fake)


def _args():
    import argparse

    return argparse.Namespace(request_timeout=5.0)


def test_cached_download_verifies_and_publishes(monkeypatch, tmp_path):
    monkeypatch.setattr(blockcache, "resolve_cache_root", lambda: tmp_path)
    data = b"asset-bytes"
    stub = _DownloadStub(data=data)
    _patch_download(monkeypatch, stub)

    got, hit = blockcache.cached_download(
        _args(),
        base_url="http://a.test",
        paper_id="qa_01h5e0aaaabbbbccccddddeeffff",
        kind="pdf",
        sha256=hashlib.sha256(data).hexdigest(),
        url_path="/x",
        what="pdf",
        expected_content_prefixes=("application/pdf",),
    )
    assert got == data
    assert hit is False
    assert stub.calls == 1

    # Second call: served from cache without downloading.
    got2, hit2 = blockcache.cached_download(
        _args(),
        base_url="http://a.test",
        paper_id="qa_01h5e0aaaabbbbccccddddeeffff",
        kind="pdf",
        sha256=hashlib.sha256(data).hexdigest(),
        url_path="/x",
        what="pdf",
        expected_content_prefixes=("application/pdf",),
    )
    assert hit2 is True
    assert stub.calls == 1


def test_cached_download_hash_mismatch_raises_and_publishes_nothing(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(blockcache, "resolve_cache_root", lambda: tmp_path)
    stub = _DownloadStub(data=b"corrupted")
    _patch_download(monkeypatch, stub)

    with pytest.raises(blockapi.ApiError) as excinfo:
        blockcache.cached_download(
            _args(),
            base_url="http://a.test",
            paper_id="qa_01h5e0aaaabbbbccccddddeeffff",
            kind="pdf",
            sha256="b" * 64,
            url_path="/x",
            what="pdf",
            expected_content_prefixes=("application/pdf",),
        )
    assert excinfo.value.kind == "bad_content"
    papers_root = tmp_path / "papers"
    assert not papers_root.exists() or not any(papers_root.rglob("*.pdf"))


def test_cached_download_corrupt_entry_is_redownloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(blockcache, "resolve_cache_root", lambda: tmp_path)
    good = b"good-bytes"
    sha = hashlib.sha256(good).hexdigest()
    # Pre-seed a corrupt cache entry that does not match the declared hash.
    final = blockcache.asset_cache_path(
        tmp_path, "http://a.test", "qa_01h5e0aaaabbbbccccddddeeffff", "pdf", sha
    )
    final.parent.mkdir(parents=True)
    final.write_bytes(b"tampered")
    stub = _DownloadStub(data=good)
    _patch_download(monkeypatch, stub)

    got, hit = blockcache.cached_download(
        _args(),
        base_url="http://a.test",
        paper_id="qa_01h5e0aaaabbbbccccddddeeffff",
        kind="pdf",
        sha256=sha,
        url_path="/x",
        what="pdf",
        expected_content_prefixes=("application/pdf",),
    )
    assert got == good
    assert hit is False
    assert stub.calls == 1
    assert final.read_bytes() == good  # repaired


def test_cached_download_concurrent_single_network_fetch(monkeypatch, tmp_path):
    """Two racing processes → one download, both get verified bytes."""
    monkeypatch.setattr(blockcache, "resolve_cache_root", lambda: tmp_path)
    data = b"shared-asset"
    stub = _DownloadStub(data=data, delay=0.3)  # long enough to overlap
    _patch_download(monkeypatch, stub)

    results: list[tuple[bytes, bool]] = []
    errors: list[Exception] = []

    def racer():
        try:
            results.append(
                blockcache.cached_download(
                    _args(),
                    base_url="http://a.test",
                    paper_id="qa_01h5e0aaaabbbbccccddddeeffff",
                    kind="pdf",
                    sha256=hashlib.sha256(data).hexdigest(),
                    url_path="/x",
                    what="pdf",
                    expected_content_prefixes=("application/pdf",),
                )
            )
        except Exception as exc:  # pragma: no cover — surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=racer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert stub.calls == 1  # dedup: exactly one download under the lock
    assert all(r[0] == data for r in results)
    final = blockcache.asset_cache_path(
        tmp_path, "http://a.test", "qa_01h5e0aaaabbbbccccddddeeffff",
        "pdf", hashlib.sha256(data).hexdigest(),
    )
    assert final.read_bytes() == data
    assert list(final.parent.glob("*.tmp")) == []


def test_cached_download_no_cache_never_touches_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(blockcache, "resolve_cache_root", lambda: tmp_path)
    data = b"direct"
    stub = _DownloadStub(data=data)
    _patch_download(monkeypatch, stub)

    got, hit = blockcache.cached_download(
        _args(),
        base_url="http://a.test",
        paper_id="qa_01h5e0aaaabbbbccccddddeeffff",
        kind="pdf",
        sha256=hashlib.sha256(data).hexdigest(),
        url_path="/x",
        what="pdf",
        expected_content_prefixes=("application/pdf",),
        use_cache=False,
    )
    assert (got, hit) == (data, False)
    assert not (tmp_path / "papers").exists()
