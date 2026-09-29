"""Offline tests for `qatlas comments …` (plan §8 Q3 / §12.2 Q2)."""

from __future__ import annotations

import hashlib
import json

import pytest

from qatlas.client import blockapi, comments
from tests.client.mocktransport import MockTransport, make_response, mount, stub_common

PAPER = "qa_01h5e0aaaabbbbccccddddeeffff"
REVISION = "rev-a-0001"
DISCUSSION = "disc_0000000000000001"
REPLY = "repl_0000000000000002"

DISCUSSION_BODY = {
    "discussion_id": DISCUSSION,
    "paper_id": PAPER,
    "parse_revision": REVISION,
    "page_idx": 0,
    "block_index": 1,
    "type": "transcription_error",
    "scope": "public",
    "status": "pending",
    "body": "sqrt range looks wrong",
    "created_by": "alice",
    "model": None,
    "revision": 3,
    "created_at": "2026-09-29T01:00:00Z",
    "updated_at": "2026-09-29T02:00:00Z",
    "replies": [
        {
            "reply_id": REPLY,
            "body": "checked against the original scan",
            "created_by": "bob",
            "revision": 1,
            "created_at": "2026-09-29T01:30:00Z",
            "updated_at": "2026-09-29T01:30:00Z",
        }
    ],
    "next_cursor": None,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    stub_common(monkeypatch)
    mount(monkeypatch, MockTransport())


def _run(monkeypatch, transport: MockTransport, argv: list[str]) -> int:
    mount(monkeypatch, transport)
    return comments.main(argv)


# ---------------------------------------------------------------------------
# list / show
# ---------------------------------------------------------------------------


def test_list_passes_filters_and_cursor(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/discussions")
    def _list(req):
        q = MockTransport.query_of(req)
        assert q["scope"] == ["lean"]
        assert q["type"] == ["transcription_error"]
        assert q["status"] == ["pending"]
        assert q["page_idx"] == ["0"]
        assert q["block_index"] == ["1"]
        assert q["cursor"] == ["c1"]
        return make_response(
            200,
            json_body={"items": [DISCUSSION_BODY], "next_cursor": "c2"},
        )

    code = _run(
        monkeypatch,
        transport,
        [
            "list", PAPER,
            "--scope", "lean",
            "--type", "transcription_error",
            "--status", "pending",
            "--page-idx", "0",
            "--block-index", "1",
            "--cursor", "c1",
        ],
    )
    assert code == 0
    out = capsys.readouterr().out
    assert DISCUSSION in out
    assert "sqrt range looks wrong" in out
    assert "c2" in out


def test_list_json_prints_full_response(monkeypatch, capsys):
    transport = MockTransport()
    body = {"items": [DISCUSSION_BODY], "next_cursor": None}

    @transport.route("GET", f"/api/papers/{PAPER}/discussions")
    def _list(req):
        return make_response(200, json_body=body)

    assert _run(monkeypatch, transport, ["list", PAPER, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == body


def test_show_renders_detail_with_replies(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/discussions/{DISCUSSION}")
    def _show(req):
        q = MockTransport.query_of(req)
        assert q["per_page"] == ["20"]
        return make_response(200, json_body=DISCUSSION_BODY)

    assert _run(monkeypatch, transport, ["show", DISCUSSION]) == 0
    out = capsys.readouterr().out
    assert "transcription_error/public" in out
    assert "sqrt range looks wrong" in out
    assert REPLY in out
    assert "checked against the original scan" in out


# ---------------------------------------------------------------------------
# create / reply — idempotency + body budget
# ---------------------------------------------------------------------------


def test_create_sends_anchor_payload_and_idempotency_key(monkeypatch, capsys):
    transport = MockTransport()
    path = f"/api/papers/{PAPER}/discussions"

    @transport.route("POST", path)
    def _create(req):
        body = MockTransport.json_body_of(req)
        assert body["parse_revision"] == REVISION
        assert body["page_idx"] == 0
        assert body["block_index"] == 1
        assert body["type"] == "transcription_error"
        assert body["scope"] == "public"
        assert body["status"] == "pending"
        assert body["model"] == "glm-5.3"
        assert body["body"] == "sqrt range looks wrong"
        # §12.2: key = SHA-256(method|path|body)
        expected = hashlib.sha256(
            f"POST|{path}|{req.body.decode()}".encode()
        ).hexdigest()
        assert req.headers["Idempotency-Key"] == expected
        assert req.headers["Content-Type"] == "application/json"
        return make_response(201, json_body=DISCUSSION_BODY)

    code = _run(
        monkeypatch,
        transport,
        [
            "create", PAPER, REVISION, "0", "1", "sqrt range looks wrong",
            "--type", "transcription_error",
            "--status", "pending",
            "--model", "glm-5.3",
        ],
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "Idempotency-Key" in err


def test_create_body_over_limit_fails_client_side(monkeypatch, capsys):
    transport = MockTransport()
    huge = "x" * (blockapi.MAX_BODY_CHARS + 1)
    code = _run(
        monkeypatch, transport,
        ["create", PAPER, REVISION, "0", "1", huge],
    )
    assert code == blockapi.EXIT_USAGE
    assert transport.requests_seen == []
    assert "split" in capsys.readouterr().err


def test_create_invalid_type_slug_rejected(monkeypatch):
    transport = MockTransport()
    code = _run(
        monkeypatch, transport,
        ["create", PAPER, REVISION, "0", "1", "hi", "--type", "Not A Slug!"],
    )
    assert code == blockapi.EXIT_USAGE
    assert transport.requests_seen == []


def test_create_custom_type_slug_accepted(monkeypatch):
    transport = MockTransport()

    @transport.route("POST", f"/api/papers/{PAPER}/discussions")
    def _create(req):
        return make_response(201, json_body=DISCUSSION_BODY)

    code = _run(
        monkeypatch, transport,
        ["create", PAPER, REVISION, "0", "1", "note", "--type", "lean:remark-2"],
    )
    assert code == 0


def test_create_body_file_stdin(monkeypatch):
    transport = MockTransport()

    @transport.route("POST", f"/api/papers/{PAPER}/discussions")
    def _create(req):
        body = MockTransport.json_body_of(req)
        assert body["body"] == "from stdin"
        return make_response(201, json_body=DISCUSSION_BODY)

    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    code = _run(
        monkeypatch, transport,
        ["create", PAPER, REVISION, "0", "1", "--body-file", "-", "--json"],
    )
    assert code == 0


def test_create_body_conflict_sources(monkeypatch):
    transport = MockTransport()
    code = _run(
        monkeypatch,
        transport,
        ["create", PAPER, REVISION, "0", "1", "positional", "--body-file", "x"],
    )
    assert code == blockapi.EXIT_USAGE
    assert transport.requests_seen == []


def test_reply_payload_and_key(monkeypatch):
    transport = MockTransport()

    @transport.route("POST", f"/api/discussions/{DISCUSSION}/replies")
    def _reply(req):
        body = MockTransport.json_body_of(req)
        assert body == {"body": "evidence here", "model": "glm-5.3"}
        return make_response(201, json_body=DISCUSSION_BODY["replies"][0])

    code = _run(
        monkeypatch, transport,
        ["reply", DISCUSSION, "evidence here", "--model", "glm-5.3", "--json"],
    )
    assert code == 0


# ---------------------------------------------------------------------------
# status / edit — CAS + permission errors
# ---------------------------------------------------------------------------


def test_status_change_requires_reason(monkeypatch):
    transport = MockTransport()
    # argparse enforces --reason (SystemExit 2 in-process; exit code 2
    # for the CLI).
    with pytest.raises(SystemExit) as excinfo:
        _run(monkeypatch, transport, ["status", DISCUSSION, "confirmed"])
    assert excinfo.value.code == 2
    code = _run(
        monkeypatch, transport,
        ["status", DISCUSSION, "confirmed", "--reason", "   "],
    )
    assert code == blockapi.EXIT_USAGE
    assert transport.requests_seen == []


def test_status_sends_payload(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("PATCH", f"/api/discussions/{DISCUSSION}/status")
    def _status(req):
        body = MockTransport.json_body_of(req)
        assert body == {"status": "confirmed", "reason": "verified against scan"}
        return make_response(200, json_body={**DISCUSSION_BODY, "status": "confirmed"})

    code = _run(
        monkeypatch, transport,
        ["status", DISCUSSION, "confirmed", "--reason", "verified against scan"],
    )
    assert code == 0
    assert "confirmed" in capsys.readouterr().out


def test_edit_discussion_auto_fetches_revision_and_sends_if_match(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/discussions/{DISCUSSION}")
    def _get(req):
        return make_response(200, json_body=DISCUSSION_BODY)

    @transport.route("PATCH", f"/api/discussions/{DISCUSSION}/body")
    def _patch(req):
        assert req.headers["If-Match"] == "3"
        body = MockTransport.json_body_of(req)
        assert body == {"body": "edited root body"}
        return make_response(
            200, json_body={**DISCUSSION_BODY, "body": "edited root body", "revision": 4}
        )

    code = _run(
        monkeypatch, transport,
        ["edit", "discussion", DISCUSSION, "edited root body"],
    )
    assert code == 0
    assert "auto-fetched current revision 3" in capsys.readouterr().err


def test_edit_discussion_explicit_if_match(monkeypatch):
    transport = MockTransport()

    @transport.route("PATCH", f"/api/discussions/{DISCUSSION}/body")
    def _patch(req):
        assert req.headers["If-Match"] == "7"
        return make_response(200, json_body=DISCUSSION_BODY)

    code = _run(
        monkeypatch, transport,
        ["edit", "discussion", DISCUSSION, "x", "--if-match", "7", "--json"],
    )
    assert code == 0
    # No GET should have happened with an explicit --if-match.
    assert all(r.method == "PATCH" for r in transport.requests_seen)


def test_edit_stale_revision_is_conflict(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/discussions/{DISCUSSION}")
    def _get(req):
        return make_response(200, json_body=DISCUSSION_BODY)

    @transport.route("PATCH", f"/api/discussions/{DISCUSSION}/body")
    def _patch(req):
        return make_response(
            409, json_body={"detail": "revision mismatch: current is 9"}
        )

    code = _run(monkeypatch, transport, ["edit", "discussion", DISCUSSION, "x"])
    assert code == blockapi.EXIT_CONFLICT
    assert "revision mismatch" in capsys.readouterr().err


def test_edit_reply_finds_reply_revision(monkeypatch):
    transport = MockTransport()

    @transport.route("GET", f"/api/discussions/{DISCUSSION}")
    def _get(req):
        return make_response(200, json_body=DISCUSSION_BODY)

    @transport.route("PATCH", f"/api/discussions/{DISCUSSION}/replies/{REPLY}/body")
    def _patch(req):
        assert req.headers["If-Match"] == "1"
        return make_response(200, json_body=DISCUSSION_BODY["replies"][0])

    code = _run(
        monkeypatch, transport,
        ["edit", "reply", DISCUSSION, REPLY, "edited reply", "--json"],
    )
    assert code == 0


def test_edit_reply_unknown_reply(monkeypatch):
    transport = MockTransport()

    @transport.route("GET", f"/api/discussions/{DISCUSSION}")
    def _get(req):
        return make_response(200, json_body=DISCUSSION_BODY)

    code = _run(
        monkeypatch, transport,
        ["edit", "reply", DISCUSSION, "repl_unknown", "x"],
    )
    assert code == blockapi.EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# error contract on writes
# ---------------------------------------------------------------------------


def test_write_403_system_pat_hint(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("POST", f"/api/papers/{PAPER}/discussions")
    def _create(req):
        return make_response(403, json_body={"detail": "system PAT is read-only"})

    code = _run(monkeypatch, transport, ["create", PAPER, REVISION, "0", "1", "hi"])
    assert code == blockapi.EXIT_FORBIDDEN
    err = capsys.readouterr().err
    assert "comments:write" in err
    assert "system PAT" in err


def test_write_429_rate_limited(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("POST", f"/api/papers/{PAPER}/discussions")
    def _create(req):
        return make_response(
            429, json_body={"detail": "too many"}, headers={"Retry-After": "17"}
        )

    code = _run(monkeypatch, transport, ["create", PAPER, REVISION, "0", "1", "hi"])
    assert code == blockapi.EXIT_RATE_LIMITED
    assert "Retry-After: 17" in capsys.readouterr().err


def test_write_413_body_too_large(monkeypatch):
    transport = MockTransport()

    @transport.route("POST", f"/api/papers/{PAPER}/discussions")
    def _create(req):
        return make_response(413, json_body={"detail": "payload too large"})

    code = _run(monkeypatch, transport, ["create", PAPER, REVISION, "0", "1", "hi"])
    assert code == blockapi.EXIT_TOO_LARGE


def test_unsupported_old_server(monkeypatch, capsys):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/discussions")
    def _list(req):
        return make_response(404, content=b"404 page not found")

    code = _run(monkeypatch, transport, ["list", PAPER])
    assert code == blockapi.EXIT_UNSUPPORTED
    assert "does not implement" in capsys.readouterr().err


def test_401_unauthorized(monkeypatch):
    transport = MockTransport()

    @transport.route("GET", f"/api/papers/{PAPER}/discussions")
    def _list(req):
        return make_response(401, json_body={"detail": "credentials required"})

    assert _run(monkeypatch, transport, ["list", PAPER]) == blockapi.EXIT_UNAUTHORIZED


def test_write_goes_through_version_preflight_guard(monkeypatch):
    """§8 Q3: writes reuse the version-negotiation preflight — the guard
    must fire BEFORE the POST leaves the client."""
    transport = MockTransport()

    from qatlas.client import blockapi as api

    calls: list[str] = []
    monkeypatch.setattr(
        api, "check_server_before_write", lambda *a, **kw: calls.append("preflight")
    )

    @transport.route("POST", f"/api/papers/{PAPER}/discussions")
    def _create(req):
        calls.append("post")
        return make_response(201, json_body=DISCUSSION_BODY)

    code = _run(monkeypatch, transport, ["create", PAPER, REVISION, "0", "1", "hi"])
    assert code == 0
    assert calls == ["preflight", "post"]


def test_help_and_dispatch_errors(capsys):
    assert comments.main([]) == 0
    assert "qatlas comments" in capsys.readouterr().out
    assert comments.main(["nope"]) == 2
    assert "unknown subcommand" in capsys.readouterr().err
    assert comments.main(["edit", "bogus"]) == 2
    assert "unknown 'comments edit' target" in capsys.readouterr().err


def test_cli_table_registers_comments(monkeypatch, capsys):
    from qatlas import cli as top

    monkeypatch.setattr("sys.argv", ["qatlas", "--help"])
    top.main()
    out = capsys.readouterr().out
    assert "comments" in out
