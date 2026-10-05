"""Offline direct PDF delivery tests: authenticated headers, pins and integrity."""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from qatlas.client import blockapi, blockcache, blocks, paper
from tests.client.mocktransport import MockTransport, make_response, mount, stub_common

PAPER = "qa_01h5e0aaaabbbbccccddddeeffff"
BASE = "http://server.test"
PDF = b"%PDF-1.7 fixture-original-bytes"
SHA = hashlib.sha256(PDF).hexdigest()


def pdf_response(content=PDF, **header_overrides):
    headers = {
        "Content-Type": "application/pdf",
        "X-QAtlas-Paper-Id": PAPER,
        "X-QAtlas-Resolved-Id": PAPER,
        "X-QAtlas-Source-Id": "src_v2",
        "X-QAtlas-Source-Origin": "arxiv:v2",
        "X-QAtlas-PDF-SHA256": SHA,
    }
    headers.update(header_overrides)
    return make_response(200, content=content, headers=headers)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    stub_common(monkeypatch)
    monkeypatch.setattr(blockcache, "resolve_cache_root", lambda: tmp_path / "cache")


def run(monkeypatch, transport, *args):
    mount(monkeypatch, transport)
    return paper.main(["pdf", *args])


def cached_path(tmp_path):
    return tmp_path / "cache" / "papers" / blockcache.server_key(BASE) / PAPER / "pdf" / f"{SHA}.pdf"


@pytest.mark.parametrize("identity", [PAPER, "0811.3171v2", "quant-ph/9508027v2", "10.1103/PhysRevLett.103.150502"])
def test_direct_pdf_all_identities_verified_without_source_listing(monkeypatch, tmp_path, identity):
    transport = MockTransport()
    route = f"/api/papers/{blockapi.encode_path_segment(identity)}/pdf"
    transport.add("GET", route, lambda req: pdf_response())
    out = tmp_path / "out.pdf"
    assert run(monkeypatch, transport, identity, "-o", str(out)) == 0
    assert out.read_bytes() == PDF
    assert cached_path(tmp_path).read_bytes() == PDF
    assert len(transport.requests_seen) == 1
    assert transport.requests_seen[0].headers["Authorization"] == "Bearer test-token"


def test_authenticated_response_required_even_for_cache_hit(monkeypatch, tmp_path, capsys):
    transport = MockTransport()
    seen = []

    def serve(req):
        response = pdf_response()
        original = response.iter_content
        response.iter_content = lambda **kwargs: (seen.append(1) or original(**kwargs))
        return response

    transport.add("GET", f"/api/papers/{PAPER}/pdf", serve)
    out = tmp_path / "out.pdf"
    assert run(monkeypatch, transport, PAPER, "-o", str(out)) == 0
    assert run(monkeypatch, transport, PAPER, "-o", str(out)) == 0
    assert len(transport.requests_seen) == 2
    assert len(seen) == 1  # second authorized response body need not be drained
    assert "server authorized" in capsys.readouterr().err


@pytest.mark.parametrize("flag,value,key", [("--source", "src_v2", "source_id"), ("--source-id", "src_v2", "source_id"), ("--version", "v2", "version")])
def test_explicit_pins_sent_to_direct_endpoint(monkeypatch, tmp_path, flag, value, key):
    transport = MockTransport()

    def serve(req):
        assert MockTransport.query_of(req) == {key: [value]}
        return pdf_response()

    transport.add("GET", f"/api/papers/{PAPER}/pdf", serve)
    assert run(monkeypatch, transport, PAPER, flag, value, "-o", str(tmp_path / "out.pdf")) == 0


def test_pdf_source_and_version_mutually_exclusive_without_network(monkeypatch):
    with patch.object(blockapi, "http_session") as http:
        with pytest.raises(SystemExit) as error:
            blocks.main(["pdf", PAPER, "--source", "src_v2", "--version", "v2"])
    assert error.value.code == 2
    http.assert_not_called()


@pytest.mark.parametrize("flags", [["--source", "other"], ["--version", "v9"]])
def test_substituted_pin_is_never_emitted(monkeypatch, tmp_path, flags):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: pdf_response())
    out = tmp_path / "out.pdf"
    assert run(monkeypatch, transport, PAPER, *flags, "-o", str(out)) == 1
    assert not out.exists()
    assert not cached_path(tmp_path).exists()


@pytest.mark.parametrize("identity", [PAPER, "0811.3171v2", "10.1/alias"])
def test_hash_mismatch_emits_nothing_for_all_identities(monkeypatch, tmp_path, capsys, identity):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{blockapi.encode_path_segment(identity)}/pdf", lambda req: pdf_response(b"%PDF-tampered"))
    out = tmp_path / "out.pdf"
    out.write_bytes(b"existing output")
    assert run(monkeypatch, transport, identity, "-o", str(out)) == 1
    assert out.read_bytes() == b"existing output"
    assert capsys.readouterr().out == ""
    assert not cached_path(tmp_path).exists()


@pytest.mark.parametrize("headers", [{"Content-Type": "text/html"}, {"X-QAtlas-PDF-SHA256": ""}, {"X-QAtlas-PDF-SHA256": "../../unsafe"}, {"X-QAtlas-Source-Id": ""}])
def test_unverifiable_or_non_pdf_response_rejected(monkeypatch, tmp_path, headers):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: pdf_response(**headers))
    out = tmp_path / "out.pdf"
    assert run(monkeypatch, transport, PAPER, "-o", str(out)) == 1
    assert not out.exists()


def test_sha256_header_alias_and_no_cache(monkeypatch, tmp_path):
    transport = MockTransport()
    transport.add("GET", "/api/papers/0811.3171/pdf", lambda req: pdf_response(**{"X-QAtlas-PDF-SHA256": "", "X-QAtlas-Sha256": SHA}))
    out = tmp_path / "out.pdf"
    assert run(monkeypatch, transport, "0811.3171", "--no-cache", "-o", str(out)) == 0
    assert out.read_bytes() == PDF
    assert not cached_path(tmp_path).exists()


@pytest.mark.parametrize("status,code,detail", [(401, 4, "token revoked"), (403, 5, "insufficient scope"), (404, 3, "paper access disabled"), (404, 3, "source not found"), (409, 6, "ambiguous source"), (429, 8, "rate limited")])
def test_http_failure_never_serves_or_deletes_cached_pdf(monkeypatch, tmp_path, capsys, status, code, detail):
    cache = cached_path(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(PDF)
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: make_response(status, json_body={"detail": detail}))
    out = tmp_path / "out.pdf"
    assert run(monkeypatch, transport, PAPER, "-o", str(out)) == code
    assert not out.exists()
    assert cache.read_bytes() == PDF
    assert detail in capsys.readouterr().err


def test_unknown_route_reports_unsupported(monkeypatch):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: make_response(404, content=b"not found", headers={"Content-Type": "text/plain"}))
    assert run(monkeypatch, transport, PAPER) == 9


def test_forced_refresh_rechecks_bytes_and_corrupt_cache_is_replaced(monkeypatch, tmp_path):
    cache = cached_path(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"corrupt cache")
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: pdf_response())
    out = tmp_path / "out.pdf"
    assert run(monkeypatch, transport, PAPER, "-o", str(out)) == 0
    assert cache.read_bytes() == PDF
    assert run(monkeypatch, transport, PAPER, "--force-refresh", "-o", str(out)) == 0


def test_pdf_stdout_is_only_verified_original_bytes(monkeypatch, capsysbinary):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: pdf_response())
    assert run(monkeypatch, transport, PAPER, "--no-cache") == 0
    captured = capsysbinary.readouterr()
    assert captured.out == PDF
    assert b"sha256 verified" in captured.err


def test_large_pdf_is_streamed_before_output(monkeypatch, tmp_path):
    data = b"%PDF-" + b"x" * (2 << 20)
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: pdf_response(data, **{"X-QAtlas-PDF-SHA256": hashlib.sha256(data).hexdigest()}))
    out = tmp_path / "large.pdf"
    assert run(monkeypatch, transport, PAPER, "--no-cache", "-o", str(out)) == 0
    assert out.read_bytes() == data


@pytest.mark.parametrize("headers", [{"X-QAtlas-Paper-Id": "qa_other"}, {"X-QAtlas-Sha256": "0" * 64}])
def test_conflicting_identity_or_hash_headers_never_emitted(monkeypatch, tmp_path, headers):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: pdf_response(**headers))
    out = tmp_path / "out.pdf"
    assert run(monkeypatch, transport, PAPER, "-o", str(out)) == 1
    assert not out.exists()


@pytest.mark.parametrize("origin,code", [("arxiv:v2", 0), ("arxiv:0811.3171v2", 0), ("arxiv:quant-ph/9508027v2", 0), ("arxiv:0811.3171v3", 1), ("S3VersionId-v2", 1)])
def test_version_pin_uses_semantic_arxiv_origin_not_s3_version(monkeypatch, tmp_path, origin, code):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: pdf_response(**{"X-QAtlas-Source-Origin": origin}))
    out = tmp_path / "version.pdf"
    assert run(monkeypatch, transport, PAPER, "--version", "v2", "-o", str(out)) == code
    assert out.exists() == (code == 0)


def test_interrupted_pdf_stream_preserves_output_and_cache(monkeypatch, tmp_path):
    import requests
    transport = MockTransport()

    def serve(req):
        response = pdf_response()

        def chunks(**kwargs):
            yield b"%PDF-"
            raise requests.ConnectionError("stream interrupted")

        response.iter_content = chunks
        return response

    transport.add("GET", f"/api/papers/{PAPER}/pdf", serve)
    out = tmp_path / "out.pdf"
    out.write_bytes(b"existing output")
    cache = cached_path(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(PDF)
    assert run(monkeypatch, transport, PAPER, "--force-refresh", "-o", str(out)) == 1
    assert out.read_bytes() == b"existing output"
    assert cache.read_bytes() == PDF
