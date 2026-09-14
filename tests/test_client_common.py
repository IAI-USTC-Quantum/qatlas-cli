"""Unit tests for qatlas.client._common (v0.17.0+: YAML-only).

v0.17.0 removed --base-url / --token / --insecure CLI flags and all
``QATLAS_*`` env-var reads. Values come exclusively from
``~/.config/qatlas/config.yaml``. ``--request-timeout`` is the only
flag ``add_common_http_args`` still registers.

The per-host ``hosts.yml`` store (populated by ``qatlas auth login``)
is the only token fallback when ``config.yaml`` has no ``token:``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from qatlas import config_yaml
from qatlas.client import _common


@pytest.fixture(autouse=True)
def _isolate_xdg(monkeypatch, tmp_path: Path):
    """Point XDG_CONFIG_HOME + HOME at a fresh empty dir for every test.

    Both are needed because ``user_config_yaml_path()`` uses
    XDG_CONFIG_HOME directly while ``auth.config_dir()`` honours
    XDG_CONFIG_HOME but also reads HOME as a base for ``.config``.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    (tmp_path / "xdg-config").mkdir(exist_ok=True)
    # Tests must not leak QATLAS_* env into ServerConfig (env source is
    # disabled but we belt-and-suspenders here in case a contributor
    # re-enables it).
    for var in ("QATLAS_TOKEN", "QATLAS_SERVER_URL", "QATLAS_INSECURE"):
        monkeypatch.delenv(var, raising=False)


def _ns(**overrides):
    """Make an argparse.Namespace mimicking add_common_http_args output."""
    base = dict(request_timeout=120.0)
    base.update(overrides)
    return argparse.Namespace(**base)


def _seed_config(server_url=None, insecure=None):
    """Write a config.yaml under the isolated XDG dir."""
    from qatlas.paths import user_config_yaml_path

    data = {}
    if server_url is not None:
        data["server_url"] = server_url
    if insecure is not None:
        data["insecure"] = insecure
    config_yaml.write_yaml_atomic(user_config_yaml_path(), data)


def _seed_hosts_yml(host: str, token: str) -> None:
    """Drop a per-host PAT into the isolated XDG dir's hosts.yml.

    v0.19.0 removed the config.yaml ``token:`` field — hosts.yml is
    now the only place we read bearer credentials from. Tests that
    used to seed config.yaml.token now have to go through this.
    """
    from qatlas.client import auth

    store = auth._load_store()
    store.setdefault("hosts", {})[host] = {
        "token": token,
        "added_at": "2026-05-28T00:00:00Z",
    }
    auth._save_store(store)


def test_resolve_token_from_hosts_yml():
    _seed_config(server_url="https://quantum-atlas.ai")
    _seed_hosts_yml("quantum-atlas.ai", "qat_from_hosts_yml")
    assert _common.resolve_token(_ns()) == "qat_from_hosts_yml"


def test_resolve_token_empty_when_unconfigured():
    # Fresh isolated XDG, no config.yaml, no hosts.yml. Should
    # return "" (no Authorization), not crash, not look at env.
    assert _common.resolve_token(_ns()) == ""


def test_resolve_token_empty_when_host_not_in_hosts_yml():
    """server_url points somewhere we haven't logged into → no token,
    not an error. The eventual HTTP request will get a 401 from the
    server if it needs auth.
    """
    _seed_config(server_url="https://other.example.com")
    _seed_hosts_yml("quantum-atlas.ai", "qat_OnlyForAnotherHost")
    assert _common.resolve_token(_ns()) == ""


def test_resolve_token_normalises_url_to_host_key():
    """server_url accepts URL form (scheme + port); hosts.yml stores
    hostname. resolve_token must normalize before lookup so a user who
    set `server_url: https://quantum-atlas.ai:4200` still finds the
    PAT they stored via `qatlas auth login -s quantum-atlas.ai:4200`.
    """
    _seed_config(server_url="https://quantum-atlas.ai")
    _seed_hosts_yml("quantum-atlas.ai", "qat_NormalisedOK")
    assert _common.resolve_token(_ns()) == "qat_NormalisedOK"


def test_auth_headers_no_token():
    # No yaml, no store. Empty dict so callers can splat
    # {**auth_headers(args), ...} safely.
    assert _common.auth_headers(_ns()) == {}


def test_auth_headers_bearer_format():
    _seed_config(server_url="https://quantum-atlas.ai")
    _seed_hosts_yml("quantum-atlas.ai", "abc123")
    assert _common.auth_headers(_ns()) == {"Authorization": "Bearer abc123"}


def test_default_base_url_from_yaml():
    _seed_config(server_url="https://my.atlas.example/")
    # get_server_url strips trailing slash for consistency.
    assert _common.default_base_url() == "https://my.atlas.example"


def test_default_base_url_no_trailing_slash_preserved():
    _seed_config(server_url="https://my.atlas.example")
    assert _common.default_base_url() == "https://my.atlas.example"


def test_default_base_url_falls_back_to_local_pocketbase():
    # No config.yaml at all: dev convenience — return localhost so a
    # bare `qatlas` against `pixi run server` works without setup.
    assert _common.default_base_url() == "http://127.0.0.1:8090"


def test_base_url_from_args_returns_yaml_value():
    _seed_config(server_url="https://yaml.example/")
    # args is accepted for back-compat; no field is read from it.
    assert _common.base_url_from_args(_ns()) == "https://yaml.example"


def test_request_verify_default_is_strict():
    # No config, no insecure: TLS verification ON (default secure).
    assert _common.request_verify(_ns()) is True


def test_request_verify_insecure_from_yaml(capsys):
    _seed_config(insecure=True)
    result = _common.request_verify(_ns())
    assert result is False
    err = capsys.readouterr().err
    assert "TLS certificate verification is disabled" in err


def test_request_verify_warning_one_shot_per_args(capsys):
    _seed_config(insecure=True)
    args = _ns()
    _common.request_verify(args)
    _ = capsys.readouterr()
    # Second call on the same args namespace must not warn again.
    _common.request_verify(args)
    assert capsys.readouterr().err == ""


def test_add_common_http_args_only_registers_request_timeout():
    """v0.17.0 removed --base-url / --token / --insecure.

    Argparse should still register --request-timeout (per-call
    ergonomic knob) but reject the now-deleted flags.
    """
    parser = argparse.ArgumentParser()
    _common.add_common_http_args(parser)
    parsed = parser.parse_args([])
    assert parsed.request_timeout == 120.0

    parsed = parser.parse_args(["--request-timeout", "30"])
    assert parsed.request_timeout == 30.0

    # Removed flags must be rejected by argparse (it exits with 2 on
    # unknown arg, which surfaces as SystemExit in tests).
    for removed_flag in ("--base-url", "--token", "--insecure"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed_flag, "value"] if removed_flag != "--insecure" else [removed_flag])


# ---------------------------------------------------------------------------
# Client/server version negotiation — contract (since 0.22.1): equal
# (major, minor) ⇒ compatible; patch drift is ignored. Mismatch warns in
# both directions; only write preflights against a NEWER server hard-fail (exit 4).
# ---------------------------------------------------------------------------

import requests  # noqa: E402

_CLIENT_UNDER_TEST = "0.22.1"


def _resp(server_version: str | None) -> requests.Response:
    """Build a bare Response carrying (or not) the server-version header."""
    r = requests.Response()
    if server_version is not None:
        r.headers["X-Qatlas-Server-Version"] = server_version
    return r


@pytest.fixture
def _pin_client_version(monkeypatch):
    """Pin the client version and reset the one-shot warning registry."""
    monkeypatch.setattr(_common, "_CLIENT_VERSION", _CLIENT_UNDER_TEST)
    _common._WARNED_VERSION_MISMATCH.clear()
    yield
    _common._WARNED_VERSION_MISMATCH.clear()


def test_version_check_same_xy_passes(_pin_client_version, capsys):
    # qatlasd 0.22.4 ↔ qatlas-cli 0.22.1: patch drift, fully compatible.
    _common.check_response_version(_resp("0.22.4"), write=False)
    _common.check_response_version(_resp("0.22.0"), write=True, request_sent=False)
    assert capsys.readouterr().err == ""


def test_version_check_server_newer_read_warns(_pin_client_version, capsys):
    _common.check_response_version(_resp("0.23.0"), write=False)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "0.23.0" in err and _CLIENT_UNDER_TEST in err
    assert "uv tool upgrade qatlas-cli" in err


def test_version_check_server_newer_read_warns_one_shot(_pin_client_version, capsys):
    _common.check_response_version(_resp("0.23.0"), write=False)
    _common.check_response_version(_resp("0.23.0"), write=False)
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1


def test_version_check_server_newer_write_preflight_exits_4(_pin_client_version, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _common.check_response_version(_resp("0.23.0"), write=True, request_sent=False)
    assert excinfo.value.code == 4
    err = capsys.readouterr().err
    assert "ERROR" in err and "uv tool upgrade qatlas-cli" in err
    assert "Write request was not sent" in err


def test_version_check_write_response_defaults_to_already_sent(_pin_client_version, capsys):
    # Existing/third-party response callers must not accidentally claim that
    # an already-sent write was refused if they omit the new keyword.
    _common.check_response_version(_resp("0.23.0"), write=True)
    err = capsys.readouterr().err
    assert "WARNING" in err and "already sent" in err
    assert "ERROR" not in err and "was not sent" not in err


def test_version_check_client_newer_read_warns(_pin_client_version, capsys):
    _common.check_response_version(_resp("0.21.5"), write=False)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "0.21.5" in err and _CLIENT_UNDER_TEST in err
    assert "0.22.x" in err  # suggests the server line to upgrade to


def test_version_check_client_newer_write_preflight_only_warns(_pin_client_version, capsys):
    # Client newer is NEVER a hard fail, even on writes — the operator
    # controls the server, and most old endpoints still work.
    _common.check_response_version(_resp("0.21.5"), write=True, request_sent=False)
    err = capsys.readouterr().err
    assert "WARNING" in err and "ERROR" not in err


def test_version_check_missing_header_skips(_pin_client_version, capsys):
    # Pre-v0.8.0 server: no header → silent skip.
    _common.check_response_version(_resp(None), write=True, request_sent=False)
    assert capsys.readouterr().err == ""


def test_version_check_unparseable_header_fails_open(_pin_client_version, capsys):
    _common.check_response_version(_resp("dev-build"), write=True, request_sent=False)
    assert capsys.readouterr().err == ""
