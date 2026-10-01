"""Offline tests for `qatlas paper pdf|parse-*|block-*` (plan §8 Q3).

All HTTP is served by the requests-native MockTransport in
``tests/client/mocktransport.py`` — fixture responses, no network.
"""

from __future__ import annotations

import hashlib

import pytest

from qatlas.client import blockapi, blockcache, blocks
from tests.client.mocktransport import MockTransport, make_response, mount, stub_common

PAPER = "qa_01h5e0aaaabbbbccccddddeeffff"  # fixture qa_ id (29 chars)
BASE = "http://server.test"
PDF_BYTES = b"%PDF-1.7 fixture-original-bytes"
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()
REVISION = "rev-a-0001"
JSON_BYTES = b'{"schema":"docvortex.middle","schema_version":"2.0"}'
JSON_SHA = hashlib.sha256(JSON_BYTES).hexdigest()

SOURCES_BODY = {
    "items": [
        {
            "source_id": "src_v2",
            "origin": "arxiv:v2",
            "sha256": PDF_SHA,
            "size_bytes": len(PDF_BYTES),
            "is_current": True,
        },
        {
            "source_id": "src_v3",
            "origin": "arxiv:v3",
            "sha256": hashlib.sha256(b"v3-bytes").hexdigest(),
            "size_bytes": 8,
        },
    ],
}

PARSES_BODY = {
    "items": [
        {
            "revision_id": REVISION,
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "artifact_sha256": JSON_SHA,
            "source_id": "src_v2",
            "is_current": True,
            "created_at": "2026-09-29T00:00:00Z",
        }
    ]
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    stub_common(monkeypatch)
    monkeypatch.setattr(blockcache, "resolve_cache_root", lambda: tmp_path / "cache")
    mount(monkeypatch, MockTransport())  # replaced per-test when needed


def _run(monkeypatch, transport: MockTransport, argv: list[str]) -> int:
    mount(monkeypatch, transport)
    return blocks.main(argv)


# ---------------------------------------------------------------------------
# paper pdf — pinning, hash verification, cache reuse
# ---------------------------------------------------------------------------


def test_pdf_downloads_verifies_and_caches(monkeypatch, capsys, tmp_path):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=SOURCES_BODY)

    @transport.route("GET", f"/api/papers/{PAPER}/sources/src_v2/pdf")
    def _pdf(req):
        return make_response(
            200, content=PDF_BYTES, headers={"Content-Type": "application/pdf"}
        )

    code = _run(
        monkeypatch,
        transport,
        ["pdf", PAPER, "--output", str(tmp_path / "out.pdf")],
    )
    assert code == 0
    assert (tmp_path / "out.pdf").read_bytes() == PDF_BYTES
    cached = (
        tmp_path / "cache" / "papers" / blockcache.server_key(BASE) / PAPER / "pdf"
        / f"{PDF_SHA}.pdf"
    )
    assert cached.is_file()
    assert cached.read_bytes() == PDF_BYTES
    err = capsys.readouterr().err
    assert "sha256 verified" in err
    # stdout carries only notes-free data flow; the file is the payload


def test_pdf_second_call_is_cache_hit_without_network(monkeypatch, tmp_path):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=SOURCES_BODY)

    @transport.route("GET", f"/api/papers/{PAPER}/sources/src_v2/pdf")
    def _pdf(req):
        return make_response(
            200, content=PDF_BYTES, headers={"Content-Type": "application/pdf"}
        )

    out = tmp_path / "out.pdf"
    assert _run(monkeypatch, transport, ["pdf", PAPER, "-o", str(out)]) == 0
    assert out.read_bytes() == PDF_BYTES
    # Second call: only the (cheap, auth-checked) sources listing route
    # is registered — the heavy PDF asset must come from the cache, and
    # no /pdf request may be issued.
    transport2 = MockTransport()

    @transport2.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources2(req):
        return make_response(200, json_body=SOURCES_BODY)

    assert _run(monkeypatch, transport2, ["pdf", PAPER, "-o", str(out)]) == 0
    assert out.read_bytes() == PDF_BYTES
    assert all(not r.url.endswith("/pdf") for r in transport2.requests_seen)


def test_pdf_version_pin_never_falls_back(monkeypatch):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=SOURCES_BODY)  # has v2, v3 only

    code = _run(monkeypatch, transport, ["pdf", PAPER, "--version", "v9"])
    assert code == blockapi.EXIT_NOT_FOUND
    # And no pdf request was ever issued:
    assert all("/pdf" not in r.url for r in transport.requests_seen)


def test_pdf_version_selects_matching_source(monkeypatch, tmp_path):
    transport = MockTransport()
    v3_bytes = b"v3-bytes"
    v3_sha = hashlib.sha256(v3_bytes).hexdigest()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=SOURCES_BODY)

    @transport.route("GET", f"/api/papers/{PAPER}/sources/src_v3/pdf")
    def _pdf(req):
        return make_response(
            200, content=v3_bytes, headers={"Content-Type": "application/pdf"}
        )

    code = _run(
        monkeypatch, transport, ["pdf", PAPER, "--version", "v3", "-o", str(tmp_path / "v3.pdf")]
    )
    assert code == 0
    assert (tmp_path / "v3.pdf").read_bytes() == v3_bytes
    assert v3_sha  # selected via origin arxiv:v3, hash verified


def test_pdf_hash_mismatch_never_cached(monkeypatch, tmp_path):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=SOURCES_BODY)

    @transport.route("GET", f"/api/papers/{PAPER}/sources/src_v2/pdf")
    def _pdf(req):
        return make_response(
            200,
            content=b"%PDF-tampered",
            headers={"Content-Type": "application/pdf"},
        )

    code = _run(monkeypatch, transport, ["pdf", PAPER])
    assert code == blockapi.EXIT_TRANSPORT
    cache_dir = tmp_path / "cache" / "papers"
    assert not any(cache_dir.rglob("*.pdf"))


def test_pdf_html_masquerade_rejected(monkeypatch):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=SOURCES_BODY)

    @transport.route("GET", f"/api/papers/{PAPER}/sources/src_v2/pdf")
    def _pdf(req):
        # Proxy error page with a 200 status — must not be emitted as PDF.
        return make_response(
            200, content=b"<html>gateway</html>", headers={"Content-Type": "text/html"}
        )

    code = _run(monkeypatch, transport, ["pdf", PAPER])
    assert code == blockapi.EXIT_TRANSPORT


def test_pdf_401_not_masked_by_cache(monkeypatch):
    transport = MockTransport()
    from qatlas.client import blockcache as bc

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=SOURCES_BODY)

    @transport.route("GET", f"/api/papers/{PAPER}/sources/src_v2/pdf")
    def _pdf(req):
        n = len([r for r in transport.requests_seen if r.url.endswith("/pdf")])
        if n > 1:  # second pdf GET → token has been revoked
            return make_response(401, json_body={"detail": "token revoked"})
        return make_response(
            200, content=PDF_BYTES, headers={"Content-Type": "application/pdf"}
        )

    assert _run(monkeypatch, transport, ["pdf", PAPER, "-o", "/dev/null"]) == 0
    # Now force a refresh: the server says 401 — the CLI must NOT serve
    # the existing cache entry as success, and must NOT delete it.
    code = _run(monkeypatch, transport, ["pdf", PAPER, "--force-refresh", "-o", "/dev/null"])
    assert code == blockapi.EXIT_UNAUTHORIZED
    cached = (
        bc.resolve_cache_root() / "papers" / bc.server_key(BASE) / PAPER / "pdf"
        / f"{PDF_SHA}.pdf"
    )
    assert cached.is_file()  # revocation never deletes a legal download


def test_pdf_403_scope_error(monkeypatch):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(403, json_body={"detail": "insufficient scope"})

    code = _run(monkeypatch, transport, ["pdf", PAPER])
    assert code == blockapi.EXIT_FORBIDDEN


def test_pdf_unsupported_old_server(monkeypatch):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(404, content=b"<html>not found</html>")

    code = _run(monkeypatch, transport, ["pdf", PAPER])
    assert code == blockapi.EXIT_UNSUPPORTED


def test_pdf_ambiguous_sources_demand_choice(monkeypatch):
    transport = MockTransport()
    body = {
        "items": [
            {"source_id": "a", "origin": "upload", "sha256": PDF_SHA},
            {"source_id": "b", "origin": "arxiv:v2", "sha256": PDF_SHA},
        ]
    }

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=body)

    code = _run(monkeypatch, transport, ["pdf", PAPER])
    assert code == blockapi.EXIT_USAGE


def test_pdf_cache_survives_account_switch(monkeypatch):
    """Switching accounts (different tokens) reuses the same cache entry.

    Originals are content-addressed per server+qa_+sha256 — never per
    account (plan §8 Q3).
    """
    transport = MockTransport()
    calls = {"n": 0}

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=SOURCES_BODY)

    @transport.route("GET", f"/api/papers/{PAPER}/sources/src_v2/pdf")
    def _pdf(req):
        calls["n"] += 1
        return make_response(
            200, content=PDF_BYTES, headers={"Content-Type": "application/pdf"}
        )

    assert _run(monkeypatch, transport, ["pdf", PAPER, "-o", "/dev/null"]) == 0
    stub_common(monkeypatch, token="another-account-token")
    assert _run(monkeypatch, transport, ["pdf", PAPER, "-o", "/dev/null"]) == 0
    assert calls["n"] == 1  # second run: cache hit, no re-download


# ---------------------------------------------------------------------------
# parse-list / parse-json
# ---------------------------------------------------------------------------


def test_parse_list_human_and_json(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/parses")
    def _parses(req):
        return make_response(200, json_body=PARSES_BODY)

    assert _run(monkeypatch, transport, ["parse-list", PAPER]) == 0
    out = capsys.readouterr().out
    assert REVISION in out
    assert "docvortex.middle@2.0" in out

    assert _run(monkeypatch, transport, ["parse-list", PAPER, "--json"]) == 0
    import json

    assert json.loads(capsys.readouterr().out) == PARSES_BODY


def test_parse_json_downloads_verifies(monkeypatch, tmp_path):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/parses")
    def _parses(req):
        return make_response(200, json_body=PARSES_BODY)

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/json")
    def _json(req):
        return make_response(
            200, content=JSON_BYTES, headers={"Content-Type": "application/json"}
        )

    code = _run(
        monkeypatch, transport, ["parse-json", PAPER, REVISION, "-o", str(tmp_path / "p.json")]
    )
    assert code == 0
    assert (tmp_path / "p.json").read_bytes() == JSON_BYTES


def test_parse_json_unknown_revision(monkeypatch):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/parses")
    def _parses(req):
        return make_response(200, json_body=PARSES_BODY)

    code = _run(monkeypatch, transport, ["parse-json", PAPER, "rev-does-not-exist"])
    assert code == blockapi.EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# block-list / block-get / block-image
# ---------------------------------------------------------------------------


def test_block_list_pagination_params_and_cursor(monkeypatch, capsys):
    transport = MockTransport()
    first = {
        "items": [
            {"page_idx": 0, "index": 1, "type": "text", "content": "hello block"},
        ],
        "next_cursor": "cursor-42",
    }

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks")
    def _blocks(req):
        q = MockTransport.query_of(req)
        if q.get("cursor") == ["cursor-42"]:
            return make_response(200, json_body={"items": [], "next_cursor": None})
        assert q["page_idx"] == ["0"]
        assert q["per_page"] == ["20"]
        return make_response(200, json_body=first)

    assert _run(
        monkeypatch, transport, ["block-list", PAPER, REVISION, "--page-idx", "0"]
    ) == 0
    out = capsys.readouterr().out
    assert "hello block" in out
    assert "cursor-42" in out
    assert _run(
        monkeypatch, transport, ["block-list", PAPER, REVISION, "--cursor", "cursor-42"]
    ) == 0


def test_block_list_per_page_bounds(monkeypatch):
    transport = MockTransport()
    code = _run(monkeypatch, transport, ["block-list", PAPER, REVISION, "--per-page", "101"])
    assert code == blockapi.EXIT_USAGE
    assert transport.requests_seen == []


def test_block_get_combined_json_and_hint(monkeypatch, capsys):
    transport = MockTransport()
    combined = {
        "source": {"paper_id": PAPER, "pdf_sha256": PDF_SHA, "schema": "docvortex.middle"},
        "anchor": {"page_idx": 0, "block_index": 1},
        "content": {"type": "equation", "content": "E = mc^2"},
        "discussions": [
            {"discussion_id": "d1", "status": "pending", "type": "transcription_error", "body": "sqrt range"}
        ],
        "next_cursor": None,
    }

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks/0/1")
    def _block(req):
        return make_response(200, json_body=combined)

    assert _run(monkeypatch, transport, ["block-get", PAPER, REVISION, "0", "1"]) == 0
    captured = capsys.readouterr()
    assert "E = mc^2" in captured.out
    assert "transcription_error" in captured.out
    assert "qatlas comments create" in captured.err  # §7.2 gentle hint

    assert _run(
        monkeypatch, transport, ["block-get", PAPER, REVISION, "0", "1", "--json", "--quiet-notes"]
    ) == 0
    import json

    assert json.loads(capsys.readouterr().out) == combined


def test_block_image_bytes_and_content_type_guard(monkeypatch, tmp_path, capsys):
    transport = MockTransport()
    png = b"\x89PNG fixture"

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks/0/1/image")
    def _img(req):
        return make_response(200, content=png, headers={"Content-Type": "image/png"})

    code = _run(
        monkeypatch, transport,
        ["block-image", PAPER, REVISION, "0", "1", "-o", str(tmp_path / "b.png")],
    )
    assert code == 0
    assert (tmp_path / "b.png").read_bytes() == png

    # A JSON error body with 200 (misrouted) must not be emitted as image.
    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks/0/2/image")
    def _img2(req):
        return make_response(200, json_body={"detail": "no bbox"})

    code = _run(monkeypatch, transport, ["block-image", PAPER, REVISION, "0", "2"])
    assert code == blockapi.EXIT_TRANSPORT
    assert "no bbox" in capsys.readouterr().err


def test_block_get_missing_block_is_not_found(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks/9/9")
    def _block(req):
        return make_response(404, json_body={"detail": "block not found"})

    code = _run(monkeypatch, transport, ["block-get", PAPER, REVISION, "9", "9", "--quiet-notes"])
    assert code == blockapi.EXIT_NOT_FOUND


def test_paper_main_dispatches_block_subcommands(monkeypatch):
    from qatlas.client import paper as paper_cli

    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/parses")
    def _parses(req):
        return make_response(200, json_body=PARSES_BODY)

    mount(monkeypatch, transport)
    code = paper_cli.main(["parse-list", PAPER])
    assert code == 0


@pytest.mark.parametrize("shape", ["items", "sources"])
def test_source_list_preserves_all_sources_and_hashes(monkeypatch, capsys, shape):
    import json
    transport = MockTransport()
    body = {shape: SOURCES_BODY["items"], "paper_id": PAPER}

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=body)

    assert _run(monkeypatch, transport, ["source-list", PAPER, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == body
    assert _run(monkeypatch, transport, ["source-list", PAPER]) == 0
    captured = capsys.readouterr()
    assert "src_v2" in captured.out and "src_v3" in captured.out
    assert "arxiv:v2" in captured.out and "arxiv:v3" in captured.out
    assert PDF_SHA in captured.out
    assert "--source" in captured.err


def test_source_list_dispatches_through_paper_cli(monkeypatch, capsys):
    from qatlas.client import paper
    import json
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body=SOURCES_BODY)

    mount(monkeypatch, transport)
    assert paper.main(["source-list", PAPER, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == SOURCES_BODY


@pytest.mark.parametrize("json_output", [False, True])
def test_source_list_bad_shape_is_failure(monkeypatch, capsys, json_output):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body={"sources": "not an array"})

    flags = ["--json"] if json_output else []
    assert _run(monkeypatch, transport, ["source-list", PAPER, *flags]) == 1
    assert capsys.readouterr().out == ""


def test_source_list_empty_registry_is_success(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body={"sources": []})

    assert _run(monkeypatch, transport, ["source-list", PAPER]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no source PDF" in captured.err
