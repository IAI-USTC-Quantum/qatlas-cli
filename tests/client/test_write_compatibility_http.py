"""Real loopback HTTP regression coverage for QuantumAtlas issue #24.

No mocked requests or version guards: the server records received writes and
commits before sending its response, including a lost-response scenario.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from threading import Thread
from types import SimpleNamespace

import pytest
import requests

from qatlas import cli
from qatlas.client import _common, blockapi, pluginsupport
from qatlas.client.plugins.base import CliContext

PAPER_ID = "qa_01m40trpkx066jvrrnd7sp5q5v"
SERVER_VERSION = "0.37.0-rc.6"
NEXT_VERSION = "0.38.0-rc.1"
FETCH_RESULT = {
    "enqueued": 1,
    "items": [{"input": "2609.40263", "paper_id": PAPER_ID,
               "kind": "arxiv", "created": True}],
}


@contextmanager
def compatibility_server(*, probe_version=SERVER_VERSION, write_version=SERVER_VERSION,
                         probe_status=200, write_status=200, drop=None):
    state = SimpleNamespace(requests=[], committed=[], result=FETCH_RESULT)

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.requests.append({"method": self.command, "path": self.path,
                                   "headers": dict(self.headers), "body": body.decode()})
            probe = self.command == "GET" and self.path == "/atlas/api/server/info"
            status = probe_status if probe else write_status
            version = probe_version if probe else write_version
            if self.command in {"POST", "PUT", "PATCH", "DELETE"} and 200 <= status < 300:
                # Simulate the committed server state, not merely an HTTP mock.
                state.committed.append(PAPER_ID)
            if drop == ("probe" if probe else "write") or drop == self.path:
                self.close_connection = True
                return
            result = ({"version": version} if probe else state.result)
            if status >= 400:
                result = {"detail": "fixture unavailable"}
            data = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if 300 <= status < 400:
                self.send_header("Location", "/atlas/api/redirected")
            if version is not None:
                self.send_header("X-Qatlas-Server-Version", version)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        state.base_url = f"http://127.0.0.1:{server.server_port}/atlas"
        thread = Thread(target=server.serve_forever,
                        kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture(autouse=True)
def isolated_client(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # Loopback must never go through user-configured proxies.
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(_common, "_CLIENT_VERSION", "0.37.0rc2")
    monkeypatch.setattr(_common, "_WARNED_VERSION_MISMATCH", set())
    config = tmp_path / "config" / "qatlas" / "config.yaml"
    config.parent.mkdir(parents=True)

    def configure(base_url):
        config.write_text(f"server_url: {base_url}\n", encoding="utf-8")

    return configure


def fetch(*items):
    return cli.main(["paper", "fetch", *(items or ("2609.40263",)), "--json",
                     "--request-timeout", "2"])


def assert_probe_only(server):
    assert [(r["method"], r["path"]) for r in server.requests] == [
        ("GET", "/atlas/api/server/info"),
    ]
    assert server.committed == []


def assert_one_write(server, method="POST", path="/api/downloader/fetch"):
    assert [(r["method"], r["path"]) for r in server.requests] == [
        ("GET", "/atlas/api/server/info"), (method, "/atlas" + path),
    ]
    assert server.committed == [PAPER_ID]


@pytest.mark.parametrize("client,server_version", [
    ("0.34.0", SERVER_VERSION), ("0.37.0rc2", NEXT_VERSION),
])
def test_fetch_newer_server_refused_before_commit(
    isolated_client, monkeypatch, capsys, client, server_version,
):
    monkeypatch.setattr(_common, "_CLIENT_VERSION", client)
    with compatibility_server(probe_version=server_version) as server:
        isolated_client(server.base_url)
        assert fetch() == 4
        assert_probe_only(server)
        assert server.requests[0]["headers"]["X-Qatlas-Client-Version"] == client
    output = capsys.readouterr()
    assert output.out == ""
    assert "Write request was not sent" in output.err


@pytest.mark.parametrize("probe_status,probe_version,write_version,warning", [
    (200, SERVER_VERSION, SERVER_VERSION, False),
    (200, SERVER_VERSION, NEXT_VERSION, True),
    (404, None, NEXT_VERSION, True),
    (200, None, NEXT_VERSION, True),
    (200, "dev-build", NEXT_VERSION, True),
])
def test_fetch_committed_result_survives_version_drift(
    isolated_client, capsys, probe_status, probe_version, write_version, warning,
):
    with compatibility_server(probe_status=probe_status, probe_version=probe_version,
                              write_version=write_version) as server:
        isolated_client(server.base_url)
        assert fetch() == 0
        assert_one_write(server)
        assert json.loads(server.requests[1]["body"]) == {"items": ["2609.40263"]}
        assert all(r["headers"]["X-Qatlas-Client-Version"] == "0.37.0rc2"
                   for r in server.requests)
    output = capsys.readouterr()
    assert json.loads(output.out) == FETCH_RESULT
    assert ("already sent" in output.err) is warning
    assert "was not sent" not in output.err
    assert "ERROR" not in output.err


def test_fetch_server_error_is_not_replaced_by_version_refusal(isolated_client, capsys):
    with compatibility_server(write_status=503, write_version=NEXT_VERSION) as server:
        isolated_client(server.base_url)
        assert fetch() == 1
        assert len(server.requests) == 2
        assert server.committed == []
    output = capsys.readouterr()
    assert output.out == ""
    assert "HTTP 503: fixture unavailable" in output.err
    assert "already sent" in output.err
    assert "was not sent" not in output.err


def test_fetch_lost_response_does_not_retry_committed_write(isolated_client, capsys):
    with compatibility_server(drop="write") as server:
        isolated_client(server.base_url)
        assert fetch() == 1
        assert_one_write(server)
    output = capsys.readouterr()
    assert output.out == ""
    assert "Request failed" in output.err
    assert "UNKNOWN" in output.err and "No automatic retry" in output.err
    assert "Check server state" in output.err
    assert "Retrying is safe" not in output.err
    assert "Idempotency-Key" not in server.requests[1]["headers"]
    assert "was not sent" not in output.err


def mutate(kind, base_url):
    body = {"body": "fixture comment"}
    if kind.startswith("plugin-"):
        method = kind.removeprefix("plugin-")
        ctx = CliContext(server_base_url=base_url, request_timeout=2)
        return pluginsupport.server_request(ctx, method, "/api/example",
                                            write=True, json=body).json()
    args = argparse.Namespace(request_timeout=2)
    if kind == "block-POST":
        return blockapi.post_json(args, "/api/example", what="create",
                                  json_body=body, base_url=base_url)
    return blockapi.patch_json(args, "/api/example", what="edit", json_body=body,
                               if_match=1, base_url=base_url)


@pytest.mark.parametrize("kind", ["plugin-POST", "plugin-PUT", "plugin-PATCH",
                                  "plugin-DELETE", "block-POST", "block-PATCH"])
@pytest.mark.parametrize("refuse", [False, True])
def test_shared_mutations_guard_before_write_and_preserve_committed_response(
    isolated_client, capsys, kind, refuse,
):
    with compatibility_server(probe_version=NEXT_VERSION if refuse else SERVER_VERSION,
                              write_version=NEXT_VERSION) as server:
        isolated_client(server.base_url)
        if refuse:
            with pytest.raises(SystemExit) as exc:
                mutate(kind, server.base_url)
            assert exc.value.code == 4
            assert_probe_only(server)
        else:
            assert mutate(kind, server.base_url) == FETCH_RESULT
            assert_one_write(server, method=kind.split("-")[1], path="/api/example")
    output = capsys.readouterr()
    assert ("already sent" in output.err) is not refuse
    assert ("Write request was not sent" in output.err) is refuse


@pytest.mark.parametrize("failure", [401, 403, 429, 500, 503, 302, 307, "disconnect"])
def test_block_preflight_failure_is_unambiguously_unsent(isolated_client, failure):
    with compatibility_server(probe_status=failure if isinstance(failure, int) else 200,
                              drop="probe" if failure == "disconnect" else None) as server:
        isolated_client(server.base_url)
        with pytest.raises(blockapi.ApiError) as exc:
            mutate("block-POST", server.base_url)
        assert_probe_only(server)
    assert exc.value.exit_code == 1
    rendered = exc.value.render()
    assert "write request was not sent" in rendered.lower()
    assert "UNKNOWN" not in rendered
    assert "Retrying is safe" not in rendered


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_block_lost_response_remains_unknown_without_retry(isolated_client, method):
    with compatibility_server(drop="write") as server:
        isolated_client(server.base_url)
        with pytest.raises(blockapi.ApiError) as exc:
            mutate(f"block-{method}", server.base_url)
        assert_one_write(server, method=method, path="/api/example")
        headers = server.requests[1]["headers"]
        assert headers["Idempotency-Key"] == blockapi.idempotency_key(
            method, "/api/example", server.requests[1]["body"]
        )
        if method == "PATCH":
            assert headers["If-Match"] == "1"
    assert exc.value.exit_code == 1
    assert isinstance(exc.value.__cause__, requests.RequestException)
    assert "UNKNOWN" in exc.value.render()
    assert "was not sent" not in exc.value.render()


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("status,kind,exit_code", [
    (403, "forbidden", 5), (409, "conflict", 6), (503, "server", 1),
])
def test_block_error_response_version_drift_preserves_business_error(
    isolated_client, capsys, method, status, kind, exit_code,
):
    with compatibility_server(write_status=status, write_version=NEXT_VERSION) as server:
        isolated_client(server.base_url)
        with pytest.raises(blockapi.ApiError) as exc:
            mutate(f"block-{method}", server.base_url)
        assert len(server.requests) == 2
        assert server.requests[1]["method"] == method
        assert server.committed == []
    assert exc.value.status == status
    assert exc.value.kind == kind
    assert exc.value.exit_code == exit_code
    assert exc.value.body == {"detail": "fixture unavailable"}
    assert "was not sent" not in exc.value.render()
    err = capsys.readouterr().err
    assert "already sent" in err
    assert "ERROR" not in err and "was not sent" not in err


@pytest.mark.parametrize("probe_status", [200, 404])
def test_block_known_newer_header_refuses_even_on_legacy_probe_404(
    isolated_client, capsys, probe_status,
):
    with compatibility_server(probe_status=probe_status, probe_version=NEXT_VERSION) as server:
        isolated_client(server.base_url)
        with pytest.raises(SystemExit) as exc:
            mutate("block-POST", server.base_url)
        assert_probe_only(server)
    assert exc.value.code == 4
    assert "Write request was not sent" in capsys.readouterr().err


def test_block_read_does_not_probe(isolated_client, capsys):
    with compatibility_server(write_version=NEXT_VERSION) as server:
        isolated_client(server.base_url)
        assert blockapi.get_json(
            argparse.Namespace(request_timeout=2), "/api/example",
            what="read", base_url=server.base_url,
        ) == FETCH_RESULT
        assert [(r["method"], r["path"]) for r in server.requests] == [
            ("GET", "/atlas/api/example"),
        ]
        assert server.committed == []
    err = capsys.readouterr().err
    assert "WARNING" in err and "already sent" not in err


@pytest.mark.parametrize("url", [
    "https://eprint.iacr.org/2025/1234",
    "https://eprint.iacr.org/2025/1234.pdf",
    "https://papers.example.org/preprint.pdf?revision=2&download=1",
])
def test_fetch_external_url_is_passed_through_without_new_client_contract(
    isolated_client, capsys, url,
):
    # Tests CLI passthrough only: identity, URL policy and metadata belong to
    # the server. The fixture never resolves or downloads an external URL.
    with compatibility_server() as server:
        isolated_client(server.base_url)
        assert fetch(url) == 0
        assert_one_write(server)
        assert json.loads(server.requests[1]["body"]) == {"items": [url]}
    assert json.loads(capsys.readouterr().out) == FETCH_RESULT


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_plugin_committed_lost_response_preserves_requests_exception_and_unknown(
    isolated_client, capsys, method,
):
    with compatibility_server(drop="write") as server:
        isolated_client(server.base_url)
        with pytest.raises(requests.ConnectionError) as exc:
            mutate(f"plugin-{method}", server.base_url)
        assert_one_write(server, method=method, path="/api/example")
    assert type(exc.value) is requests.ConnectionError
    assert any("UNKNOWN" in note for note in exc.value.__notes__)
    output = capsys.readouterr()
    assert output.out == ""
    assert "UNKNOWN" in output.err and "No automatic retry" in output.err
    assert "was not sent" not in output.err
    assert "Retrying is safe" not in output.err


@pytest.mark.parametrize("failure", [503, 307, "disconnect"])
def test_plugin_failed_preflight_is_unsent_not_unknown(isolated_client, capsys, failure):
    with compatibility_server(probe_status=failure if isinstance(failure, int) else 200,
                              drop="probe" if failure == "disconnect" else None) as server:
        isolated_client(server.base_url)
        with pytest.raises(requests.RequestException) as exc:
            mutate("plugin-POST", server.base_url)
        assert_probe_only(server)
    assert "write request was not sent" in str(exc.value)
    assert "UNKNOWN" not in capsys.readouterr().err
