"""Defaults notes must survive real HTTP header decoding, not just mocks."""

from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
import requests

from qatlas.client import paper
from qatlas.client.paper import _print_notes


_REQUESTED = "qa_01m0qvmp3mm7czqhtrrvtzhfsf"
_RESOLVED = "quant-ph/9605043v3"
_LEGACY_DEFAULTS = f"paper_id_resolved ({_REQUESTED} → arxiv id {_RESOLVED})"
_ASCII_DEFAULTS = _LEGACY_DEFAULTS.replace("→", "->")


@contextmanager
def _http_response(defaults: bytes):
    """Serve exact header octets and let requests do its real decoding."""
    body = b'{"state": "cached"}'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # send_header() only accepts Latin-1; writing raw bytes models the
            # legacy Go server, which sent a UTF-8 arrow in this HTTP header.
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                b"Connection: close\r\n"
                b"X-QAtlas-Requested-Id: " + _REQUESTED.encode("ascii") + b"\r\n"
                b"X-QAtlas-Resolved-Id: " + _RESOLVED.encode("ascii") + b"\r\n"
                b"X-QAtlas-Defaults-Applied: " + defaults + b"\r\n\r\n" + body
            )

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            with requests.Session() as session:
                # Never route this loopback test through user-configured proxies.
                session.trust_env = False
                with session.get(
                    f"http://127.0.0.1:{server.server_port}/", timeout=5
                ) as response:
                    response.raise_for_status()
                    yield response
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize(
    ("wire_defaults", "display_defaults"),
    [
        pytest.param(_ASCII_DEFAULTS.encode("ascii"), _ASCII_DEFAULTS, id="ascii-contract"),
        pytest.param(_LEGACY_DEFAULTS.encode("utf-8"), _LEGACY_DEFAULTS, id="legacy-utf8-arrow"),
        pytest.param(b"source=caf\xe9", "source=café", id="real-latin1"),
        # Valid UTF-8 bytes can also be genuine Latin-1 text: do not guess the
        # encoding of the entire field just because a UTF-8 decode succeeds.
        pytest.param(b"literal=\xc3\xa9", "literal=Ã©", id="ambiguous-latin1"),
        pytest.param(
            b"source=caf\xe9; " + _LEGACY_DEFAULTS.encode("utf-8"),
            "source=café; " + _LEGACY_DEFAULTS,
            id="mixed-latin1-and-legacy-arrow",
        ),
        pytest.param(b"literal=\xe2\x86", "literal=\xe2\x86", id="incomplete-utf8"),
    ],
)
def test_print_notes_after_real_http_header_decoding(wire_defaults, display_defaults, capsys):
    with _http_response(wire_defaults) as response:
        received = response.headers["X-QAtlas-Defaults-Applied"]
        assert received == wire_defaults.decode("latin-1")
        _print_notes(response, quiet=False)
        # Rendering must not alter the response or its correctly decoded JSON.
        assert response.headers["X-QAtlas-Defaults-Applied"] == received
        assert response.json() == {"state": "cached"}

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"Note (server applied defaults): {_REQUESTED} → {_RESOLVED}; "
        f"{display_defaults}\n"
    )


def test_print_notes_quiet_after_real_http_header_decoding(capsys):
    with _http_response(_LEGACY_DEFAULTS.encode("utf-8")) as response:
        _print_notes(response, quiet=True)
    assert capsys.readouterr().err == ""


def test_print_notes_preserves_already_decoded_unicode(capsys):
    response = requests.Response()
    response.headers["X-QAtlas-Defaults-Applied"] = _LEGACY_DEFAULTS
    _print_notes(response, quiet=False)
    assert capsys.readouterr().err == f"Note (server applied defaults): {_LEGACY_DEFAULTS}\n"


@pytest.mark.parametrize("defaults", [_ASCII_DEFAULTS, _LEGACY_DEFAULTS])
@pytest.mark.parametrize("quiet", [False, True])
def test_paper_status_keeps_json_stdout_separate_from_decoded_notes(
    defaults, quiet, monkeypatch, tmp_path, capsys,
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    config = tmp_path / "config" / "qatlas" / "config.yaml"
    config.parent.mkdir(parents=True)
    with _http_response(defaults.encode("utf-8")) as response:
        config.write_text(f"server_url: {response.url.rstrip('/')}\n", encoding="utf-8")
        argv = ["status", _REQUESTED, "--request-timeout", "2"]
        if quiet:
            argv.append("--quiet-notes")
        assert paper.main(argv) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"state": "cached"}
    if quiet:
        assert output.err == ""
    else:
        assert output.err == (
            f"Note (server applied defaults): {_REQUESTED} → {_RESOLVED}; "
            f"{defaults}\n"
        )
