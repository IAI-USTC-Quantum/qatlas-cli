"""Offline compatibility for source-pinned lazy assets, not just paper read."""
from __future__ import annotations

import json

import pytest

from qatlas.client import blockapi, paper
from tests.client.mocktransport import MockTransport, make_response, mount
from tests.client.test_paper_read_cli import isolate as isolate

PAPER = "qa_01h5e0aaaabbbbccccddddeeffff"
SOURCE = "src_v2"
REVISION = "revision-1"
STATUS = f"/api/papers/{PAPER}/read/status"
KINDS = [("markdown", "/markdown", []), ("images", "/images/zip", []), ("figures", "/figures", []), ("image", "/images/figure.png", ["figure.png"])]


def invoke(monkeypatch, transport, kind, positional, *flags):
    mount(monkeypatch, transport)
    monkeypatch.setattr(paper.requests, "get", lambda url, **kwargs: blockapi.http_session().get(url, **kwargs))
    return paper.main(["get", kind, PAPER, *positional, *flags])


def pending(location=STATUS + "?source_id=" + SOURCE):
    return make_response(202, json_body={"state": "queued", "paper_id": PAPER, "source_id": SOURCE}, headers={"Operation-Location": location, "Retry-After": "3"})


@pytest.mark.parametrize("kind,suffix,positional", KINDS)
def test_all_assets_poll_new_ready_and_reget_exact_source_revision(monkeypatch, tmp_path, capsys, isolate, kind, suffix, positional):
    transport = MockTransport()
    calls = []
    payload = b"original verified asset bytes"
    figures = {"paper_id": PAPER, "source_id": SOURCE, "revision": REVISION, "figures": []}

    def asset(req):
        calls.append(req)
        if len(calls) == 1:
            return pending()
        assert MockTransport.query_of(req) == {"source_id": [SOURCE], "revision": [REVISION]}
        headers = {"X-QAtlas-Source-Id": SOURCE, "X-QAtlas-Parse-Revision": REVISION}
        return make_response(200, json_body=figures, headers=headers) if kind == "figures" else make_response(200, content=payload, headers=headers)

    transport.add("GET", f"/api/papers/{PAPER}{suffix}", asset)
    transport.add("GET", STATUS, lambda req: make_response(200, json_body={"state": "ready", "ready": True, "source_id": SOURCE, "revision": REVISION}))
    output = tmp_path / "asset.bin"
    flags = [] if kind == "figures" else ["-o", str(output)]
    assert invoke(monkeypatch, transport, kind, positional, *flags, "--max-wait", "30") == 0
    assert isolate == [3]
    assert len(calls) == 2
    if kind == "figures":
        assert json.loads(capsys.readouterr().out) == figures
    else:
        assert output.read_bytes() == payload
        assert capsys.readouterr().out == ""


@pytest.mark.parametrize("kind,suffix,positional", KINDS)
def test_all_asset_no_wait_returns_operation_json_without_poll(monkeypatch, capsys, kind, suffix, positional):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}{suffix}", lambda req: pending())
    assert invoke(monkeypatch, transport, kind, positional, "--no-wait") == 0
    body = json.loads(capsys.readouterr().out)
    assert body["operation"]["status_url"] == STATUS + "?source_id=" + SOURCE
    assert len(transport.requests_seen) == 1


@pytest.mark.parametrize("kind,suffix,positional", KINDS)
def test_all_asset_poll_urls_are_same_origin(monkeypatch, capsys, kind, suffix, positional):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}{suffix}", lambda req: pending("https://attacker.test/status"))
    assert invoke(monkeypatch, transport, kind, positional) == 1
    assert len(transport.requests_seen) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("state", ["failed", "cooldown", "unavailable"])
def test_asset_terminal_status_does_not_emit_bytes(monkeypatch, capsys, state):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/images/zip", lambda req: pending())
    transport.add("GET", STATUS, lambda req: make_response(200, json_body={"state": state, "detail": "parse failed"}))
    assert invoke(monkeypatch, transport, "images", []) == 1
    assert capsys.readouterr().out == ""


def test_asset_status_cannot_replace_initial_source(monkeypatch, capsys):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/figures", lambda req: pending())
    transport.add("GET", STATUS, lambda req: make_response(200, json_body={"state": "ready", "ready": True, "source_id": "other", "revision": REVISION}))
    assert invoke(monkeypatch, transport, "figures", []) == 1
    assert capsys.readouterr().out == ""


def test_asset_wait_bound_respects_initial_retry_after(monkeypatch, capsys):
    transport = MockTransport()
    transport.add("GET", f"/api/papers/{PAPER}/images/zip", lambda req: pending())
    assert invoke(monkeypatch, transport, "images", [], "--max-wait", "2") == 1
    assert len(transport.requests_seen) == 1
    assert capsys.readouterr().out == ""
