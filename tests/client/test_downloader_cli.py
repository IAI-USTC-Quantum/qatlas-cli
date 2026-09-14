"""Tests for the `qatlas paper fetch` / `qatlas paper jobs` CLI commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from qatlas.client import downloader as dl
from qatlas.client import paper as paper_cli


def _args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "items": [],
        "file": None,
        "json": False,
        "remote": False,
        "watch": False,
        "interval": 0.01,
        "request_timeout": 10.0,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _resp(status: int, json_body: dict[str, Any] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 400
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
        resp.text = json.dumps(json_body)
    return resp


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch):
    monkeypatch.setattr(dl, "base_url_from_args", lambda args: "http://server.test")
    monkeypatch.setattr(dl, "auth_headers", lambda args: {"Authorization": "Bearer t"})
    monkeypatch.setattr(dl, "client_version_headers", lambda: {})
    monkeypatch.setattr(dl, "request_verify", lambda args: True)
    monkeypatch.setattr(dl, "check_response_version", lambda resp, **kwargs: None)
    monkeypatch.setattr(dl, "check_server_before_write", lambda *args, **kwargs: None)


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def test_fetch_happy_path(capsys):
    body = {
        "items": [
            {"input": "10.1038/x", "kind": "doi", "paper_id": "qa_1", "created": True},
            {"input": "arXiv:2401.1", "kind": "arxiv", "paper_id": "qa_2", "created": False},
        ],
        "enqueued": 2,
    }
    with patch.object(dl.requests, "post", return_value=_resp(200, body)) as post:
        assert dl.cmd_fetch(_args(items=["10.1038/x", "arXiv:2401.1"])) == 0
    post.assert_called_once()
    kwargs = post.call_args.kwargs
    assert kwargs["json"] == {"items": ["10.1038/x", "arXiv:2401.1"]}
    assert post.call_args.args[0] == "http://server.test/api/downloader/fetch"
    out = capsys.readouterr().out
    assert "qa_1" in out and "(new)" in out
    assert "enqueued 2/2" in out


def test_fetch_partial_failure_still_succeeds(capsys):
    body = {
        "items": [
            {"input": "ok-id", "kind": "arxiv", "paper_id": "qa_1", "created": False},
            {"input": "garbage", "kind": "invalid", "error": "unrecognized"},
        ],
        "enqueued": 1,
    }
    with patch.object(dl.requests, "post", return_value=_resp(200, body)):
        assert dl.cmd_fetch(_args(items=["ok-id", "garbage"])) == 0
    out = capsys.readouterr().out
    assert "failed   garbage: unrecognized" in out
    assert "enqueued 1/2" in out


def test_fetch_all_failed_returns_1(capsys):
    body = {"items": [{"input": "x", "kind": "invalid", "error": "nope"}], "enqueued": 0}
    with patch.object(dl.requests, "post", return_value=_resp(200, body)):
        assert dl.cmd_fetch(_args(items=["x"])) == 1


def test_fetch_rejects_over_limit_without_http():
    with patch.object(dl.requests, "post") as post:
        assert dl.cmd_fetch(_args(items=[f"10.1/x{i}" for i in range(51)])) == 2
    post.assert_not_called()


def test_fetch_rejects_empty():
    with patch.object(dl.requests, "post") as post:
        assert dl.cmd_fetch(_args()) == 2
    post.assert_not_called()


def test_fetch_file_merges_dedupes_and_skips_comments(tmp_path: Path):
    listfile = tmp_path / "ids.txt"
    listfile.write_text(
        "# comment line\n10.1038/x\n\narXiv:2401.1\n10.1038/x\n", encoding="utf-8"
    )
    body = {"items": [], "enqueued": 2}
    with patch.object(dl.requests, "post", return_value=_resp(200, body)) as post:
        assert dl.cmd_fetch(_args(file=str(listfile))) == 0
    assert post.call_args.kwargs["json"] == {"items": ["10.1038/x", "arXiv:2401.1"]}


def test_fetch_server_503(capsys):
    with patch.object(dl.requests, "post", return_value=_resp(503, {"detail": "downloader off"})):
        assert dl.cmd_fetch(_args(items=["x"])) == 1
    assert "503" in capsys.readouterr().err


def test_fetch_json_output(capsys):
    body = {"items": [{"input": "x", "kind": "doi", "paper_id": "qa_9", "created": False}], "enqueued": 1}
    with patch.object(dl.requests, "post", return_value=_resp(200, body)):
        assert dl.cmd_fetch(_args(items=["x"], json=True)) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["enqueued"] == 1


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


def test_jobs_local_snapshot(capsys):
    body = {
        "jobs": [
            {
                "paper_id": "qa_1",
                "state": "running",
                "phase": "downloading_pdf",
                "strategy": "oa:unpaywall",
            }
        ],
        "counters": {"queued": 0, "in_flight": 1, "succeeded": 2, "failed": 1},
    }
    with patch.object(dl.requests, "get", return_value=_resp(200, body)) as get:
        assert dl.cmd_jobs(_args()) == 0
    assert get.call_args.args[0] == "http://server.test/api/downloader/jobs"
    out = capsys.readouterr().out
    assert "in_flight=1" in out
    assert "oa:unpaywall" in out


def test_jobs_remote_endpoint_and_fleet_disabled(capsys):
    body = {"enabled": False, "jobs": []}
    with patch.object(dl.requests, "get", return_value=_resp(200, body)) as get:
        assert dl.cmd_jobs(_args(remote=True)) == 0
    assert get.call_args.args[0] == "http://server.test/api/downloader/remote-jobs"
    assert "fleet enabled: False" in capsys.readouterr().out


def test_jobs_watch_polls_until_idle(capsys):
    busy = {
        "jobs": [{"paper_id": "qa_1", "state": "running", "phase": "fetching"}],
        "counters": {"queued": 1, "in_flight": 1, "succeeded": 0, "failed": 0},
    }
    idle = {"jobs": [], "counters": {"queued": 0, "in_flight": 0, "succeeded": 3, "failed": 0}}
    responses = [_resp(200, busy), _resp(200, idle)]
    with patch.object(dl.requests, "get", side_effect=responses) as get, patch.object(
        dl.time, "sleep", MagicMock()
    ) as sleep:
        assert dl.cmd_jobs(_args(watch=True)) == 0
    assert get.call_count == 2
    sleep.assert_called_once()


def test_jobs_watch_remote_terminates_on_terminal_states(capsys):
    busy = {
        "enabled": True,
        "jobs": [
            {"id": "t1", "worker_id": "w1", "state": "queued", "identifier": "10.1/x"},
            {"id": "t2", "worker_id": "w2", "state": "done", "identifier": "10.1/y"},
        ],
    }
    idle = {"enabled": True, "jobs": [{"id": "t1", "worker_id": "w9", "state": "done", "identifier": "10.1/x"}]}
    with patch.object(dl.requests, "get", side_effect=[_resp(200, busy), _resp(200, idle)]), patch.object(
        dl.time, "sleep", MagicMock()
    ):
        assert dl.cmd_jobs(_args(remote=True, watch=True)) == 0


def test_jobs_http_error_returns_1(capsys):
    with patch.object(dl.requests, "get", return_value=_resp(401, {"detail": "no auth"})):
        assert dl.cmd_jobs(_args()) == 1
    assert "401" in capsys.readouterr().err


def test_jobs_watch_json_lines(capsys):
    busy = {"jobs": [], "counters": {"queued": 1, "in_flight": 0, "succeeded": 0, "failed": 0}}
    idle = {"jobs": [], "counters": {"queued": 0, "in_flight": 0, "succeeded": 1, "failed": 0}}
    with patch.object(dl.requests, "get", side_effect=[_resp(200, busy), _resp(200, idle)]), patch.object(
        dl.time, "sleep", MagicMock()
    ):
        assert dl.cmd_jobs(_args(watch=True, json=True)) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 2
    assert lines[0]["counters"]["queued"] == 1


# ---------------------------------------------------------------------------
# paper dispatcher integration
# ---------------------------------------------------------------------------


def test_paper_main_dispatches_fetch(monkeypatch):
    body = {"items": [], "enqueued": 0}
    with patch.object(dl.requests, "post", return_value=_resp(200, body)):
        assert paper_cli.main(["fetch", "10.1038/x"]) == 1  # enqueued 0


def test_paper_main_dispatches_jobs(monkeypatch):
    body = {"jobs": [], "counters": {"queued": 0, "in_flight": 0}}
    with patch.object(dl.requests, "get", return_value=_resp(200, body)):
        assert paper_cli.main(["jobs"]) == 0


def test_paper_main_dispatches_unknown_still_errors(capsys):
    assert paper_cli.main(["nope"]) == 2
