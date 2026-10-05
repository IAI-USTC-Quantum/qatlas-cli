"""External registration wire/CLI contract against a real loopback server.

The fixture never fetches an external PDF; these are client wire tests, not
claims that server provenance, registry or deployment acceptance has passed.
"""

from __future__ import annotations

import json

import pytest

from qatlas import cli
from qatlas.client import blockapi
from tests.client.test_write_compatibility_http import (
    NEXT_VERSION,
    PAPER_ID,
    assert_one_write,
    assert_probe_only,
    compatibility_server,
    isolated_client as isolated_client,
)

PATH = "/api/papers/source-register"
URL = "https://eprint.iacr.org/2025/1234"
TITLE = "Quantum protocols — 量子协议"
AUTHORS = ["Alice Example", "Bob Example"]


def register(url=URL, *, title=TITLE, authors=AUTHORS, year="2025", json_output=True):
    command = ["paper", "source-register", url, "--request-timeout", "2"]
    if title is not None:
        command.extend(["--title", title])
    for author in authors:
        command.extend(["--author", author])
    if year is not None:
        command.extend(["--year", year])
    if json_output:
        command.append("--json")
    return cli.main(command)


def registration_result(url=URL, external_id="eprint:2025/1234", *, created=True):
    # Fields mirror the actual source projection in papers_external_sources.go.
    return {
        "paper_id": PAPER_ID, "created": created, "external_id": external_id,
        "source_url": url,
        "source": {"source_id": "src_fixture", "origin": external_id,
                   "sha256": "a" * 64, "size_bytes": 1234,
                   "created_at": "2026-10-05T00:00:00Z", "source_url": url,
                   "retrieved_url": "https://eprint.iacr.org/2025/1234.pdf",
                   "retrieved_at": "2026-10-05T00:00:00Z",
                   "pdf_endpoint": f"/api/papers/{PAPER_ID}/sources/src_fixture/pdf"},
    }


@pytest.mark.parametrize("failure", [401, 403, 429, 500, 503, 302, 307, "disconnect", "newer"])
def test_registration_preflight_failure_sends_zero_posts(isolated_client, capsys, failure):
    kwargs = {"probe_status": failure} if isinstance(failure, int) else {}
    if failure == "disconnect":
        kwargs["drop"] = "probe"
    elif failure == "newer":
        kwargs["probe_version"] = NEXT_VERSION
    with compatibility_server(**kwargs) as server:
        isolated_client(server.base_url)
        assert register() == (4 if failure == "newer" else 1)
        assert_probe_only(server)
    output = capsys.readouterr()
    assert output.out == ""
    assert "write request was not sent" in output.err.lower()
    assert "UNKNOWN" not in output.err


@pytest.mark.parametrize("created", [True, False])
@pytest.mark.parametrize("url,external_id", [
    (URL, "eprint:2025/1234"),
    ("https://papers.example.org/preprint.pdf?revision=2&download=1",
     "source_url:https://papers.example.org/preprint.pdf?revision=2&download=1"),
])
def test_registration_single_post_preserves_entire_json_without_inventing_doi(
    isolated_client, capsys, url, external_id, created,
):
    result = registration_result(url, external_id, created=created)
    # Forward-compatible response fields must also remain untouched.
    result["future_server_field"] = {"opaque": ["keep", "all"]}
    with compatibility_server() as server:
        server.result = result
        isolated_client(server.base_url)
        assert register(url) == 0
        assert_one_write(server, path=PATH)
        request = server.requests[1]
        assert json.loads(request["body"]) == {
            "source_url": url, "title": TITLE, "authors": AUTHORS, "year": 2025,
        }
        assert request["headers"]["Idempotency-Key"] == blockapi.idempotency_key(
            "POST", PATH, request["body"],
        )
        assert request["headers"]["Content-Type"] == "application/json"
    output = capsys.readouterr()
    assert json.loads(output.out) == result
    assert "doi" not in json.loads(output.out)
    assert output.err == ""


def test_committed_registration_lost_response_unknown_and_same_body_same_key(isolated_client, capsys):
    with compatibility_server(drop="write") as server:
        isolated_client(server.base_url)
        assert register() == 1
        assert_one_write(server, path=PATH)
        first = server.requests[1]
        output = capsys.readouterr()
        assert output.out == ""
        assert "UNKNOWN" in output.err
        assert "No automatic retry" in output.err
        assert "was not sent" not in output.err
        assert "Retrying is safe" not in output.err
        # This second call is an EXPLICIT manual submission, never a retry loop.
        assert register() == 1
        assert len(server.requests) == 4
        assert server.committed == [PAPER_ID, PAPER_ID]
        second = server.requests[3]
        assert first["body"] == second["body"]
        assert first["headers"]["Idempotency-Key"] == second["headers"]["Idempotency-Key"]
        assert first["headers"]["Idempotency-Key"] == blockapi.idempotency_key("POST", PATH, first["body"])
    assert "UNKNOWN" in capsys.readouterr().err


@pytest.mark.parametrize("changes", [
    {"title": None}, {"title": ""}, {"title": " \t "}, {"title": "中" * 667},
    {"authors": []}, {"authors": [""]}, {"authors": [" \t "]},
    {"authors": ["中" * 167]}, {"authors": ["Name"] * 101},
    {"year": None}, {"year": "not-a-year"}, {"year": "0"}, {"year": "10000"},
    {"url": " \t "},
])
def test_required_metadata_invalid_locally_before_any_http(isolated_client, capsys, changes):
    with compatibility_server() as server:
        isolated_client(server.base_url)
        assert register(**changes) == 2
        assert server.requests == []
        assert server.committed == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("year", ["1", "9999"])
def test_metadata_byte_boundaries_and_whitespace_normalization(isolated_client, capsys, year):
    title = "中" * 666 + "ab"  # Exactly 2000 UTF-8 bytes after stripping.
    author = "中" * 166 + "ab"  # Exactly 500 UTF-8 bytes.
    with compatibility_server() as server:
        isolated_client(server.base_url)
        server.result = registration_result()
        assert register(title=f" {title} ", authors=[f" {author} "], year=year) == 0
        assert_one_write(server, path=PATH)
        assert json.loads(server.requests[1]["body"]) == {
            "source_url": URL, "title": title, "authors": [author], "year": int(year),
        }
    assert json.loads(capsys.readouterr().out) == registration_result()


@pytest.mark.parametrize("status,exit_code", [(403, 5), (409, 6), (413, 7), (429, 8), (503, 1)])
def test_registration_business_error_retains_status_and_scope_hint(isolated_client, capsys, status, exit_code):
    with compatibility_server(write_status=status, write_version=NEXT_VERSION) as server:
        isolated_client(server.base_url)
        assert register() == exit_code
        assert len(server.requests) == 2
        assert server.committed == []
    output = capsys.readouterr()
    assert output.out == ""
    assert f"HTTP {status}" in output.err and "fixture unavailable" in output.err
    assert "already sent" in output.err
    assert "was not sent" not in output.err
    if status == 403:
        assert "papers:write" in output.err
        assert "comments:write" not in output.err


def test_registration_human_output_uses_server_identity(isolated_client, capsys):
    with compatibility_server() as server:
        isolated_client(server.base_url)
        server.result = registration_result()
        assert register(json_output=False) == 0
        assert_one_write(server, path=PATH)
    output = capsys.readouterr()
    assert PAPER_ID in output.out and "src_fixture" in output.out
    assert "external_id: eprint:2025/1234" in output.out
    assert "doi" not in output.out.lower()
    assert output.err == ""


def test_registration_missing_capability_does_not_claim_comments_scope():
    import requests

    response = requests.Response()
    response.status_code = 404
    response.headers["Content-Type"] = "text/plain"
    response._content = b"missing endpoint"
    error = blockapi._error_from_response(response, what="source registration", write=True, url_path=PATH)
    assert error.exit_code == 9
    assert "external source registration" in error.render()
    assert "block-comments" not in error.render()


def test_registration_missing_json_route_is_unsupported_without_write_fallback(isolated_client, capsys):
    with compatibility_server(write_status=404) as server:
        isolated_client(server.base_url)
        assert register() == 9
        assert len(server.requests) == 2
        assert server.requests[1]["path"] == "/atlas" + PATH
        assert server.committed == []
    output = capsys.readouterr()
    assert output.out == ""
    assert "external source registration" in output.err
    assert "unsupported" in output.err
    assert "block-comments" not in output.err
