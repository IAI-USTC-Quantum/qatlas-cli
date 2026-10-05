"""Offline derived-view, pagination and asynchronous paper-read contract tests."""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from qatlas.client import blockapi, paper
from tests.client.mocktransport import MockTransport, make_response, mount

PAPER = "qa_01h5e0aaaabbbbccccddddeeffff"
SOURCE = "src_v2"
REVISION = "rev-a-0001"
PATH = f"/api/papers/{PAPER}/read"
STATUS = f"{PATH}/status"
VIEW = {
    "paper_id": PAPER, "source_id": SOURCE, "revision": REVISION,
    "renderer": "middle-reading-v1", "format": "markdown",
    "content": "## Title\n\nEquation $E=mc^2$ and 中文 content.",
    "request_scope": {"page": 1, "block": 2},
    "content_ranges": [{"start": 0, "end": 47, "page": 1, "block": 2}],
    "truncated": True, "next_request": {"cursor": "immutable-cursor"},
    "warnings": ["derived from Middle; compare source PDF"],
}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(paper, "base_url_from_args", lambda args: "http://server.test")
    monkeypatch.setattr(paper, "auth_headers", lambda args: {"Authorization": "Bearer fixture"})
    monkeypatch.setattr(paper, "client_version_headers", lambda: {})
    monkeypatch.setattr(paper, "request_verify", lambda args: True)
    monkeypatch.setattr(paper, "check_response_version", lambda *args, **kwargs: None)
    clock = [0.0]
    delays = []
    monkeypatch.setattr(paper.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        delays.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(paper.time, "sleep", sleep)
    return delays


def run(monkeypatch, transport, *args):
    mount(monkeypatch, transport)
    monkeypatch.setattr(paper.requests, "get", lambda url, **kwargs: blockapi.http_session().get(url, **kwargs))
    return paper.main(["read", *args])


def initial(status_url=STATUS + "?source_id=" + SOURCE):
    return make_response(202, json_body={"state": "queued", "paper_id": PAPER, "source_id": SOURCE}, headers={"Operation-Location": status_url, "Retry-After": "3"})


def test_read_stdout_preserves_complete_derived_view_and_params(monkeypatch, capsys):
    transport = MockTransport()

    def serve(req):
        assert MockTransport.query_of(req) == {"source_id": [SOURCE], "revision": [REVISION], "page": ["1"], "block": ["2"], "cursor": ["cursor-in"], "limit": ["30000"]}
        assert req.headers["Authorization"] == "Bearer fixture"
        return make_response(200, json_body=VIEW)

    transport.add("GET", PATH, serve)
    assert run(monkeypatch, transport, PAPER, "--source-id", SOURCE, "--revision", REVISION, "--page", "1", "--block", "2", "--cursor", "cursor-in", "--limit", "30000", "--json") == 0
    assert json.loads(capsys.readouterr().out) == VIEW


def test_next_request_cursor_continues_without_latest_revision(monkeypatch, capsys):
    transport = MockTransport()

    def serve(req):
        assert MockTransport.query_of(req) == {"cursor": [VIEW["next_request"]["cursor"]]}
        return make_response(200, json_body={**VIEW, "content": "remaining content", "truncated": False, "next_request": None})

    transport.add("GET", PATH, serve)
    assert run(monkeypatch, transport, PAPER, "--cursor", VIEW["next_request"]["cursor"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["revision"] == REVISION and got["source_id"] == SOURCE
    assert not got["truncated"] and got["next_request"] is None


def test_read_output_file_and_encoded_alias(monkeypatch, tmp_path, capsys):
    transport = MockTransport()
    alias = "10.1103/PhysRevLett.103.150502"
    transport.add("GET", f"/api/papers/{quote(alias, safe='')}/read", lambda req: make_response(200, json_body=VIEW))
    out = tmp_path / "nested" / "read.json"
    assert run(monkeypatch, transport, alias, "-o", str(out)) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == VIEW
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("flags", [["--page", "0"], ["--page", "-1"], ["--block", "0"], ["--limit", "0"], ["--limit", "100001"]])
def test_read_bounds_rejected_without_http(monkeypatch, flags):
    transport = MockTransport()
    with pytest.raises(SystemExit) as error:
        run(monkeypatch, transport, PAPER, *flags)
    assert error.value.code == 2
    assert not transport.requests_seen


def test_read_block_requires_page(monkeypatch, capsys):
    transport = MockTransport()
    assert run(monkeypatch, transport, PAPER, "--block", "2") == 2
    assert not transport.requests_seen
    assert "1-based" in capsys.readouterr().err


@pytest.mark.parametrize("ready", ["ready", "cached", "done"])
def test_async_read_honors_retry_after_polls_then_regets_pinned_view(monkeypatch, capsys, isolate, ready):
    transport = MockTransport()
    view_calls = []
    polls = []

    def serve(req):
        view_calls.append(MockTransport.query_of(req))
        if len(view_calls) == 1:
            return initial()
        assert MockTransport.query_of(req) == {"page": ["1"], "source_id": [SOURCE], "revision": [REVISION]}
        return make_response(200, json_body=VIEW)

    def status(req):
        assert MockTransport.query_of(req) == {"source_id": [SOURCE]}
        assert req.headers["Authorization"] == "Bearer fixture"
        polls.append(1)
        body = {"state": "running"} if len(polls) == 1 else {"state": ready, "source_id": SOURCE, "revision": REVISION}
        return make_response(200, json_body=body, headers={"Retry-After": "7"})

    transport.add("GET", PATH, serve)
    transport.add("GET", STATUS, status)
    assert run(monkeypatch, transport, PAPER, "--page", "1", "--max-wait", "30") == 0
    assert isolate == [3, 7]
    captured = capsys.readouterr()
    assert json.loads(captured.out) == VIEW
    assert "async read started" in captured.err
    assert len(view_calls) == 2


def test_async_no_wait_outputs_poll_locator_without_poll(monkeypatch, capsys):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: initial())
    assert run(monkeypatch, transport, PAPER, "--no-wait") == 0
    body = json.loads(capsys.readouterr().out)
    assert body["state"] == "queued"
    assert body["operation"]["status_url"] == STATUS + "?source_id=" + SOURCE
    assert body["retry_after"] == 3
    assert len(transport.requests_seen) == 1


@pytest.mark.parametrize("state", ["failed", "cooldown", "unavailable"])
def test_async_terminal_state_does_not_fetch_view(monkeypatch, capsys, state):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: initial())
    transport.add("GET", STATUS, lambda req: make_response(200, json_body={"state": state, "detail": "parse unavailable"}))
    assert run(monkeypatch, transport, PAPER) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "parse unavailable" in captured.err
    assert len(transport.requests_seen) == 2


def test_timeout_before_retry_after_does_not_poll_or_emit(monkeypatch, capsys, isolate):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: initial())
    assert run(monkeypatch, transport, PAPER, "--max-wait", "2") == 1
    assert len(transport.requests_seen) == 1
    assert isolate == []
    assert "timed out" in capsys.readouterr().err


def test_timeout_in_running_state_is_bounded(monkeypatch, capsys, isolate):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: initial())
    transport.add("GET", STATUS, lambda req: make_response(202, json_body={"state": "running"}, headers={"Retry-After": "3"}))
    assert run(monkeypatch, transport, PAPER, "--max-wait", "5", "--quiet-progress") == 1
    assert isolate == [3, 2]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("url", ["https://attacker.test/poll", "//attacker.test/poll", "http://fixture:secret@server.test/poll"])
def test_async_poll_url_cannot_leak_credentials(monkeypatch, capsys, url):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: initial(url))
    assert run(monkeypatch, transport, PAPER) == 1
    assert len(transport.requests_seen) == 1
    assert "refusing cross-origin" in capsys.readouterr().err


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 503])
def test_read_http_errors_do_not_emit_json_success(monkeypatch, capsys, status):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: make_response(status, json_body={"detail": "paper access disabled or unavailable"}))
    assert run(monkeypatch, transport, PAPER) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "paper access disabled" in captured.err


@pytest.mark.parametrize("pin,value", [("source-id", "other"), ("revision", "other")])
def test_read_cannot_substitute_explicit_pin(monkeypatch, capsys, pin, value):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: make_response(200, json_body=VIEW))
    assert run(monkeypatch, transport, PAPER, f"--{pin}", value) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("body", [{"pdf_info": [], "schema": "docvortex.middle"}, {**VIEW, "renderer": None}, {**VIEW, "next_request": None}, {**VIEW, "paper_id": "qa_other"}])
def test_raw_artifact_or_broken_view_is_not_emitted_as_reading_success(monkeypatch, capsys, body):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: make_response(200, json_body=body))
    assert run(monkeypatch, transport, PAPER) == 1
    assert capsys.readouterr().out == ""


def test_empty_final_view_and_future_fields_are_preserved(monkeypatch, capsys):
    transport = MockTransport()
    body = {**VIEW, "content": "", "truncated": False, "next_request": None, "future_field": {"proof": "preserved"}}
    transport.add("GET", PATH, lambda req: make_response(200, json_body=body))
    assert run(monkeypatch, transport, PAPER) == 0
    assert json.loads(capsys.readouterr().out) == body


def test_read_invalid_async_response_reports_contract_error(monkeypatch, capsys):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: make_response(202, json_body={"state": "queued"}))
    assert run(monkeypatch, transport, PAPER) == 1
    assert "missing Operation-Location" in capsys.readouterr().err


@pytest.mark.parametrize("response", [lambda: make_response(200, content=b"<html>proxy</html>", headers={"Content-Type": "text/html"}), lambda: make_response(200, json_body=[]), lambda: make_response(202, json_body=[])])
def test_read_malformed_response_never_emitted(monkeypatch, capsys, response):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: response())
    assert run(monkeypatch, transport, PAPER) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_nonfinite_or_negative_wait_rejected_without_http(monkeypatch, capsys, value):
    transport = MockTransport()
    assert run(monkeypatch, transport, PAPER, "--max-wait", value) == 2
    assert not transport.requests_seen


@pytest.mark.parametrize("body", [[], {"state": "queued"}])
def test_async_status_malformed_or_missing_ready_never_emits_success(monkeypatch, capsys, body):
    transport = MockTransport()
    transport.add("GET", PATH, lambda req: initial())
    transport.add("GET", STATUS, lambda req: make_response(200, json_body=body, headers={"Retry-After": "3"}))
    assert run(monkeypatch, transport, PAPER, "--max-wait", "5") == 1
    assert capsys.readouterr().out == ""


def test_post_parse_get_error_not_reported_as_ready_content(monkeypatch, capsys):
    transport = MockTransport()
    calls = []

    def serve(req):
        calls.append(1)
        if len(calls) == 1:
            return initial()
        return make_response(404, json_body={"detail": "frozen source no longer available"})

    transport.add("GET", PATH, serve)
    transport.add("GET", STATUS, lambda req: make_response(200, json_body={"state": "ready", "source_id": SOURCE, "revision": REVISION}))
    assert run(monkeypatch, transport, PAPER) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "frozen source no longer available" in captured.err
