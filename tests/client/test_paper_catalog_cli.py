"""Tests for the `qatlas paper list` / `qatlas paper lookup` CLI commands."""

from __future__ import annotations

import argparse
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from qatlas.client import paper as cli


def _args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        # list flags
        "has_md": None,
        "status": None,
        "q": None,
        "arxiv_id": None,
        "doi": None,
        "paper_id": None,
        "page": None,
        "per_page": None,
        "sort": None,
        # lookup flags
        "refs": [],
        # shared
        "json": False,
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
    monkeypatch.setattr(cli, "base_url_from_args", lambda args: "http://server.test")
    monkeypatch.setattr(cli, "auth_headers", lambda args: {"Authorization": "Bearer t"})
    monkeypatch.setattr(cli, "client_version_headers", lambda: {})
    monkeypatch.setattr(cli, "request_verify", lambda args: True)
    monkeypatch.setattr(cli, "check_response_version", lambda resp, write: None)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


_LIST_BODY = {
    "items": [
        {
            "paper_id": "qa_01ABC",
            "arxiv_id": "2501.00010",
            "doi": "10.1103/physrevx.15.011012",
            "title": "A very long title " + "x" * 100,
            "status": "ready",
            "has_pdf": True,
            "has_md": True,
            "image_count": 3,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
        {
            "paper_id": "qa_01DEF",
            "arxiv_id": None,
            "doi": "10.1/none",
            "title": None,
            "status": "pending",
            "has_pdf": False,
            "has_md": False,
            "image_count": 0,
            "created_at": "2026-01-03T00:00:00Z",
            "updated_at": "2026-01-03T00:00:00Z",
        },
    ],
    "total": 42,
    "page": 1,
    "per_page": 2,
}


def test_list_human_output(capsys):
    with patch.object(cli.requests, "get", return_value=_resp(200, _LIST_BODY)) as get:
        assert cli.cmd_list(_args()) == 0
    assert get.call_args.args[0] == "http://server.test/api/papers"
    out = capsys.readouterr().out
    assert "42 paper(s)" in out
    assert "qa_01ABC" in out and "md:yes" in out
    assert "qa_01DEF" in out and "md:no" in out
    assert "…" in out  # long title truncated


def test_list_passes_filters_as_params():
    with patch.object(cli.requests, "get", return_value=_resp(200, _LIST_BODY)) as get:
        assert cli.cmd_list(
            _args(has_md=True, status="ready", q="surface code", page=2, sort="updated_at")
        ) == 0
    assert get.call_args.kwargs["params"] == {
        "has_md": "true",
        "status": "ready",
        "q": "surface code",
        "page": "2",
        "sort": "updated_at",
    }


def test_list_identity_filters():
    with patch.object(cli.requests, "get", return_value=_resp(200, _LIST_BODY)) as get:
        assert cli.cmd_list(_args(arxiv_id="2501.00010", doi="10.1/x", paper_id="qa_X")) == 0
    assert get.call_args.kwargs["params"] == {
        "arxiv_id": "2501.00010",
        "doi": "10.1/x",
        "paper_id": "qa_X",
    }


def test_list_no_filters_omits_params():
    with patch.object(cli.requests, "get", return_value=_resp(200, _LIST_BODY)) as get:
        assert cli.cmd_list(_args()) == 0
    assert get.call_args.kwargs.get("params") is None


def test_list_json_output(capsys):
    with patch.object(cli.requests, "get", return_value=_resp(200, _LIST_BODY)):
        assert cli.cmd_list(_args(json=True)) == 0
    assert json.loads(capsys.readouterr().out) == _LIST_BODY


def test_list_503(capsys):
    with patch.object(cli.requests, "get", return_value=_resp(503, {"detail": "catalog unavailable"})):
        assert cli.cmd_list(_args()) == 1
    assert "503" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------


_LOOKUP_BODY = {
    "results": [
        {
            "ref": "arxiv:2501.00010",
            "title": "Qubit Paper",
            "authors": ["A. Author"],
            "year": 2026,
            "hosted": True,
            "has_md": True,
            "resolved": True,
        },
        {
            "ref": "doi:10.1/missing",
            "title": "Unhosted Work",
            "year": 2020,
            "hosted": False,
            "has_md": False,
            "resolved": True,
        },
        {"ref": "openalex:W999", "resolved": False, "hosted": False, "has_md": False},
    ],
    "corpus_available": False,
}


def test_lookup_human_output(capsys):
    with patch.object(cli.requests, "get", return_value=_resp(200, _LOOKUP_BODY)) as get:
        assert cli.cmd_lookup(_args(refs=["arxiv:2501.00010", "doi:10.1/missing", "openalex:W999"])) == 0
    assert get.call_args.args[0] == "http://server.test/api/papers/lookup"
    assert get.call_args.kwargs["params"] == {
        "ids": "arxiv:2501.00010,doi:10.1/missing,openalex:W999"
    }
    captured = capsys.readouterr()
    out = captured.out
    assert "hosted, markdown ready (2026) — Qubit Paper" in out
    assert "not hosted (2020) — Unhosted Work" in out
    assert "unresolved" in out
    assert "corpus unavailable" in captured.err


def test_lookup_over_limit_without_http():
    with patch.object(cli.requests, "get") as get:
        assert cli.cmd_lookup(_args(refs=[f"doi:10.1/x{i}" for i in range(201)])) == 2
    get.assert_not_called()


def test_lookup_empty_refs_without_http():
    with patch.object(cli.requests, "get") as get:
        assert cli.cmd_lookup(_args(refs=["  "])) == 2
    get.assert_not_called()


def test_lookup_json_output(capsys):
    with patch.object(cli.requests, "get", return_value=_resp(200, _LOOKUP_BODY)):
        assert cli.cmd_lookup(_args(refs=["arxiv:2501.00010"], json=True)) == 0
    assert json.loads(capsys.readouterr().out) == _LOOKUP_BODY


def test_lookup_http_error(capsys):
    with patch.object(cli.requests, "get", return_value=_resp(400, {"detail": "ids query param required"})):
        assert cli.cmd_lookup(_args(refs=["bad"])) == 1
    assert "400" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# paper dispatcher integration
# ---------------------------------------------------------------------------


def test_paper_main_dispatches_list():
    with patch.object(cli.requests, "get", return_value=_resp(200, _LIST_BODY)):
        assert cli.main(["list", "--json"]) == 0


def test_paper_main_dispatches_lookup():
    with patch.object(cli.requests, "get", return_value=_resp(200, _LOOKUP_BODY)):
        assert cli.main(["lookup", "arxiv:2501.00010"]) == 0
