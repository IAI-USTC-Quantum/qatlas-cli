"""Offline originals/parse/block tests; direct PDF tests live separately."""
from __future__ import annotations

import hashlib

import pytest

from qatlas.client import blockapi, blockcache, blocks
from tests.client.mocktransport import MockTransport, make_response, mount, stub_common

PAPER = "qa_01h5e0aaaabbbbccccddddeeffff"
PDF_BYTES = b"%PDF-1.7 fixture-original-bytes"
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()
REVISION = "rev-a-0001"
JSON_BYTES = b'{"schema":"docvortex.middle","schema_version":"2.0"}'
JSON_SHA = hashlib.sha256(JSON_BYTES).hexdigest()

SOURCES_BODY = {
    "items": [
        {"source_id": "src_v2", "origin": "arxiv:v2", "sha256": PDF_SHA, "size_bytes": len(PDF_BYTES), "is_current": True},
        {"source_id": "src_v3", "origin": "arxiv:v3", "sha256": hashlib.sha256(b"v3-bytes").hexdigest(), "size_bytes": 8},
    ],
}
PARSES_BODY = {
    "items": [{"revision_id": REVISION, "schema": "docvortex.middle", "schema_version": "2.0", "artifact_sha256": JSON_SHA, "source_id": "src_v2", "is_current": True, "created_at": "2026-09-29T00:00:00Z"}],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    stub_common(monkeypatch)
    monkeypatch.setattr(blockcache, "resolve_cache_root", lambda: tmp_path / "cache")
    mount(monkeypatch, MockTransport())


def _run(monkeypatch, transport: MockTransport, argv: list[str]) -> int:
    mount(monkeypatch, transport)
    return blocks.main(argv)


# PDF contract tests: tests/client/test_paper_pdf_cli.py.


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
        return make_response(200, content=JSON_BYTES, headers={"Content-Type": "application/json"})

    code = _run(monkeypatch, transport, ["parse-json", PAPER, REVISION, "-o", str(tmp_path / "p.json")])
    assert code == 0
    assert (tmp_path / "p.json").read_bytes() == JSON_BYTES


def test_parse_json_unknown_revision(monkeypatch):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/parses")
    def _parses(req):
        return make_response(200, json_body=PARSES_BODY)

    assert _run(monkeypatch, transport, ["parse-json", PAPER, "rev-does-not-exist"]) == blockapi.EXIT_NOT_FOUND


def test_block_list_pagination_params_and_cursor(monkeypatch, capsys):
    transport = MockTransport()
    first = {"items": [{"page_idx": 0, "index": 1, "type": "text", "content": "hello block"}], "next_cursor": "cursor-42"}

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks")
    def _blocks(req):
        q = MockTransport.query_of(req)
        if q.get("cursor") == ["cursor-42"]:
            return make_response(200, json_body={"items": [], "next_cursor": None})
        assert q["page_idx"] == ["0"]
        assert q["per_page"] == ["20"]
        return make_response(200, json_body=first)

    assert _run(monkeypatch, transport, ["block-list", PAPER, REVISION, "--page-idx", "0"]) == 0
    out = capsys.readouterr().out
    assert "hello block" in out
    assert "cursor-42" in out
    assert _run(monkeypatch, transport, ["block-list", PAPER, REVISION, "--cursor", "cursor-42"]) == 0


def test_block_list_per_page_bounds(monkeypatch):
    transport = MockTransport()
    assert _run(monkeypatch, transport, ["block-list", PAPER, REVISION, "--per-page", "101"]) == blockapi.EXIT_USAGE
    assert transport.requests_seen == []


def test_block_get_combined_json_and_hint(monkeypatch, capsys):
    transport = MockTransport()
    combined = {
        "source": {"paper_id": PAPER, "pdf_sha256": PDF_SHA, "schema": "docvortex.middle"},
        "anchor": {"page_idx": 0, "block_index": 1},
        "content": {"type": "equation", "content": "E = mc^2"},
        "discussions": [{"discussion_id": "d1", "status": "pending", "type": "transcription_error", "body": "sqrt range"}],
        "next_cursor": None,
    }

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks/0/1")
    def _block(req):
        return make_response(200, json_body=combined)

    assert _run(monkeypatch, transport, ["block-get", PAPER, REVISION, "0", "1"]) == 0
    captured = capsys.readouterr()
    assert "E = mc^2" in captured.out
    assert "transcription_error" in captured.out
    assert "qatlas comments create" in captured.err
    assert _run(monkeypatch, transport, ["block-get", PAPER, REVISION, "0", "1", "--json", "--quiet-notes"]) == 0
    import json
    assert json.loads(capsys.readouterr().out) == combined


def test_block_image_bytes_and_content_type_guard(monkeypatch, tmp_path, capsys):
    transport = MockTransport()
    png = b"\x89PNG fixture"

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks/0/1/image")
    def _img(req):
        return make_response(200, content=png, headers={"Content-Type": "image/png"})

    code = _run(monkeypatch, transport, ["block-image", PAPER, REVISION, "0", "1", "-o", str(tmp_path / "b.png")])
    assert code == 0
    assert (tmp_path / "b.png").read_bytes() == png

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks/0/2/image")
    def _img2(req):
        return make_response(200, json_body={"detail": "no bbox"})

    assert _run(monkeypatch, transport, ["block-image", PAPER, REVISION, "0", "2"]) == blockapi.EXIT_TRANSPORT
    assert "no bbox" in capsys.readouterr().err


def test_block_get_missing_block_is_not_found(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/parses/{REVISION}/blocks/9/9")
    def _block(req):
        return make_response(404, json_body={"detail": "block not found"})

    assert _run(monkeypatch, transport, ["block-get", PAPER, REVISION, "9", "9", "--quiet-notes"]) == blockapi.EXIT_NOT_FOUND


def test_paper_main_dispatches_block_subcommands(monkeypatch):
    from qatlas.client import paper as paper_cli
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/parses")
    def _parses(req):
        return make_response(200, json_body=PARSES_BODY)

    mount(monkeypatch, transport)
    assert paper_cli.main(["parse-list", PAPER]) == 0


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


@pytest.mark.parametrize("tampered", [False, True])
def test_parse_json_alias_preserves_original_bytes_and_verifies_hash(monkeypatch, tmp_path, tampered):
    transport = MockTransport()
    alias = "0811.3171v2"
    transport.add("GET", f"/api/papers/{alias}/parses", lambda req: make_response(200, json_body=PARSES_BODY))
    transport.add("GET", f"/api/papers/{alias}/parses/{REVISION}/json", lambda req: make_response(200, content=b'{"tampered": true}' if tampered else JSON_BYTES, headers={"Content-Type": "application/json"}))
    out = tmp_path / "parse.json"
    assert _run(monkeypatch, transport, ["parse-json", alias, REVISION, "-o", str(out)]) == (1 if tampered else 0)
    if tampered:
        assert not out.exists()
    else:
        assert out.read_bytes() == JSON_BYTES


def test_source_list_empty_registry_is_success(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/sources")
    def _sources(req):
        return make_response(200, json_body={"sources": []})

    assert _run(monkeypatch, transport, ["source-list", PAPER]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no source PDF" in captured.err
