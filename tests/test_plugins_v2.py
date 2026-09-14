"""Tests for the plugin protocol v2 (CliContext / dispatch / pluginsupport)."""

from __future__ import annotations

import pytest
import requests

from qatlas import cli
from qatlas.client.plugins import registry
from qatlas.client.plugins.base import (
    PLUGIN_API_VERSION,
    CliContext,
    CommandSpec,
    QatlasPlugin,
)
from qatlas.client import pluginsupport


def _ctx(**overrides) -> CliContext:
    defaults = dict(
        server_base_url="https://qatlas.example.org",
        token="tok123",
        request_timeout=30.0,
        insecure=False,
        client_version="9.9.9",
    )
    defaults.update(overrides)
    return CliContext(**defaults)


# ---------------------------------------------------------------------------
# registry.invoke / signature detection
# ---------------------------------------------------------------------------


def test_invoke_v1_handler_receives_only_argv():
    seen = {}

    def handler(argv):
        seen["argv"] = argv
        return 7

    assert registry.invoke(CommandSpec(handler=handler, summary="x"), _ctx(), ["a"]) == 7
    assert seen == {"argv": ["a"]}


def test_invoke_v2_handler_receives_context_and_argv():
    seen = {}

    def handler(ctx, argv):
        seen["ctx"] = ctx
        seen["argv"] = argv
        return 3

    assert registry.invoke(CommandSpec(handler=handler, summary="x"), _ctx(), ["b"]) == 3
    assert isinstance(seen["ctx"], CliContext)
    assert seen["argv"] == ["b"]


def test_invoke_normalizes_return_values(capsys):
    def returns_none(ctx, argv):
        return None

    def returns_string(ctx, argv):
        return "boom"

    assert registry.invoke(CommandSpec(handler=returns_none, summary=""), _ctx(), []) == 0
    assert registry.invoke(CommandSpec(handler=returns_string, summary=""), _ctx(), []) == 1
    assert "boom" in capsys.readouterr().err


@pytest.mark.parametrize(
    "handler,expected",
    [
        (lambda argv: 0, False),
        (lambda ctx, argv: 0, True),
        (lambda ctx, argv, extra=None: 0, True),
        (lambda *args: 0, True),
        (lambda: 0, False),
    ],
)
def test_handler_takes_context_detection(handler, expected):
    assert registry._handler_takes_context(handler) is expected


# ---------------------------------------------------------------------------
# protocol version gating
# ---------------------------------------------------------------------------


class _FuturePlugin(QatlasPlugin):
    name = "future"
    cli_api_version = PLUGIN_API_VERSION + 1

    def top_level_commands(self):
        return {"future": CommandSpec(handler=lambda ctx, argv: 0, summary="s")}


class _CurrentPlugin(QatlasPlugin):
    name = "current"

    def top_level_commands(self):
        return {"current": CommandSpec(handler=lambda ctx, argv: 5, summary="s")}


def test_future_protocol_plugin_is_skipped_with_warning(monkeypatch, capsys):
    monkeypatch.setattr(registry, "_entrypoint_plugins", lambda: [_FuturePlugin()])
    commands = registry.top_level_commands()
    assert commands == {}
    err = capsys.readouterr().err
    assert "future" in err
    assert f"v{PLUGIN_API_VERSION + 1}" in err
    assert "upgrade qatlas-cli" in err


def test_future_protocol_warning_respects_quiet(monkeypatch, capsys):
    monkeypatch.setattr(registry, "_entrypoint_plugins", lambda: [_FuturePlugin()])
    monkeypatch.setenv("QATLAS_QUIET", "1")
    assert registry.top_level_commands() == {}
    assert capsys.readouterr().err == ""


def test_current_protocol_plugin_passes(monkeypatch):
    monkeypatch.setattr(registry, "_entrypoint_plugins", lambda: [_CurrentPlugin()])
    assert set(registry.top_level_commands()) == {"current"}


# ---------------------------------------------------------------------------
# cli.py integration
# ---------------------------------------------------------------------------


def test_cli_dispatches_plugin_v2_command_with_context(monkeypatch):
    seen = {}

    spec = CommandSpec(
        handler=lambda ctx, argv: (seen.update(ctx=ctx, argv=argv), 0)[1],
        summary="test plugin",
    )
    monkeypatch.setattr(registry, "top_level_commands", lambda: {"plug": spec})
    monkeypatch.setattr(
        registry, "build_cli_context", lambda *a, **kw: _ctx(token="")
    )

    assert cli.main(["plug", "--flag", "x"]) == 0
    assert isinstance(seen["ctx"], CliContext)
    assert seen["ctx"].server_base_url == "https://qatlas.example.org"
    assert seen["argv"] == ["--flag", "x"]


def test_cli_help_renders_plugin_usage_line(monkeypatch, capsys):
    spec = CommandSpec(
        handler=lambda argv: 0,
        summary="multi-source search",
        usage="search QUERY [--sources a,b] [--json]",
    )
    monkeypatch.setattr(registry, "top_level_commands", lambda: {"search": spec})

    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "multi-source search" in out
    assert "search QUERY [--sources a,b] [--json]" in out


def test_known_plugin_hint_survives_registry_import(capsys, monkeypatch):
    """The install hint table moved to registry.py but stays wired into cli.py."""
    assert cli._KNOWN_PLUGIN_COMMANDS["search"] == (
        "qatlas-search",
        "IAI-USTC-Quantum/qatlas-search",
    )
    monkeypatch.setattr(registry, "top_level_commands", lambda: {})
    result = cli.main(["search", "anything"])
    assert result == 2
    assert "qatlas-search" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# build_cli_context
# ---------------------------------------------------------------------------


def test_build_cli_context_degrades_gracefully(monkeypatch):
    import qatlas.client._common as common

    def boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(common, "default_base_url", boom)
    ctx = registry.build_cli_context()
    assert ctx.server_base_url == ""
    assert ctx.token == ""
    assert ctx.client_version  # version still resolves


# ---------------------------------------------------------------------------
# pluginsupport HTTP layer
# ---------------------------------------------------------------------------


def test_server_request_builds_url_headers_and_timeout(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs, method=method, url=url)
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(requests, "request", fake_request)

    resp = pluginsupport.server_request(_ctx(), "POST", "/api/search/multi", json={"q": 1})
    assert resp.status_code == 200
    assert captured["method"] == "POST"
    assert captured["url"] == "https://qatlas.example.org/api/search/multi"
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["X-Qatlas-Client-Version"]  # real package version
    assert captured["timeout"] == 30.0
    assert captured["verify"] is True


def test_server_request_without_token_omits_auth_header(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(requests, "request", fake_request)
    pluginsupport.server_request(_ctx(token=""), "GET", "api/papers")
    assert "Authorization" not in captured["headers"]


def test_insecure_request_warns_once_and_still_sends_request(monkeypatch, capsys):
    verifications = []
    monkeypatch.setattr(pluginsupport, "_WARNED_INSECURE", False)
    monkeypatch.setattr(requests.packages.urllib3, "disable_warnings", lambda **kwargs: None)

    def fake_request(method, url, **kwargs):
        verifications.append(kwargs["verify"])
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(requests, "request", fake_request)
    for _ in range(2):
        assert pluginsupport.server_request(_ctx(insecure=True), "GET", "/api/health").ok
    assert verifications == [False, False]
    assert capsys.readouterr().err.count("TLS certificate verification is disabled") == 1


def test_server_request_requires_configured_server():
    with pytest.raises(ValueError, match="server_url"):
        pluginsupport.server_request(_ctx(server_base_url=""), "GET", "/api/health")


def test_format_api_error_unwraps_detail_and_adds_401_hint():
    response = requests.Response()
    response.status_code = 401
    response._content = b'{"detail": "missing or invalid credentials"}'
    response.headers["Content-Type"] = "application/json"
    message = pluginsupport.format_api_error(response)
    assert "HTTP 401" in message
    assert "missing or invalid credentials" in message
    assert "qatlas auth login" in message


def test_format_api_error_truncates_long_bodies():
    response = requests.Response()
    response.status_code = 500
    response._content = b"x" * 1000
    message = pluginsupport.format_api_error(response)
    assert len(message) <= 330


def test_poll_lro_returns_when_done(monkeypatch):
    payloads = [{"state": "running"}, {"state": "running"}, {"state": "done"}]
    calls = {"n": 0}

    def fake_server_request(ctx, method, path, **kwargs):
        payload = payloads[calls["n"]]
        calls["n"] += 1
        response = requests.Response()
        response.status_code = 200
        response._content = __import__("json").dumps(payload).encode()
        return response

    monkeypatch.setattr(pluginsupport, "server_request", fake_server_request)
    monkeypatch.setattr(pluginsupport.time, "sleep", lambda s: None)

    progress = []
    result = pluginsupport.poll_lro(
        _ctx(), "/api/papers/x/markdown/status", done=lambda p: p["state"] == "done",
        interval=0.01, progress=progress.append,
    )
    assert result == {"state": "done"}
    assert calls["n"] == 3
    assert progress == [{"state": "running"}, {"state": "running"}]


def test_poll_lro_raises_timeout(monkeypatch):
    def fake_server_request(ctx, method, path, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"state": "running"}'
        return response

    monkeypatch.setattr(pluginsupport, "server_request", fake_server_request)
    monkeypatch.setattr(pluginsupport.time, "sleep", lambda s: None)

    with pytest.raises(TimeoutError):
        pluginsupport.poll_lro(
            _ctx(), "/status", done=lambda p: False, interval=0.01, timeout=0.05
        )


# ---------------------------------------------------------------------------
# auth header helper on CliContext
# ---------------------------------------------------------------------------


def test_cli_context_auth_headers():
    assert _ctx().auth_headers() == {"Authorization": "Bearer tok123"}
    assert _ctx(token="  ").auth_headers() == {}
