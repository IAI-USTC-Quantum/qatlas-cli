"""Shared helpers for QuantumAtlas client-side CLIs.

Configuration source (v0.17.0+): every value comes from
``~/.config/qatlas/config.yaml`` via ``qatlas.config.ServerConfig``.
No CLI flag, no OS env, no ``QATLAS_DOTENV``. ``qatlas auth login``
still maintains a separate per-host token file
(``~/.config/qatlas/hosts.yml``) used as the last-resort token
fallback when ``config.yaml`` has no ``token:``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

import requests

from qatlas import __version__ as _CLIENT_VERSION
from qatlas.config import ServerConfig


def _client_config() -> ServerConfig:
    """Build a fresh ServerConfig view of the current YAML.

    Cheap (~ms), and re-reading per call means a `qatlas config set`
    in between two `qatlas papers ...` invocations is picked up
    immediately without process state.
    """
    return ServerConfig.from_env()


def default_base_url() -> str:
    """Resolve the server base URL from ``config.yaml`` ``server_url``.

    Falls back to ``http://127.0.0.1:8090`` (PocketBase default) when
    unset so ``qatlas`` doesn't fatal on first run without a configured
    server — useful for local dev.
    """
    cfg = _client_config()
    server_url = cfg.get_server_url()
    if server_url:
        return server_url
    return "http://127.0.0.1:8090"


def base_url_from_args(args: argparse.Namespace) -> str:
    """Return the config-file server_url.

    ``args`` is accepted for back-compat with the v0.16 signature; no
    field is read from it anymore. Subcommands that need a different
    server should set up a separate ``XDG_CONFIG_HOME``-isolated
    config.
    """
    return default_base_url()


def request_verify(args: argparse.Namespace) -> bool:
    """Honor ``insecure: true`` in config.yaml to disable TLS verification.

    Same one-shot warning behaviour as before. ``args`` kept for
    signature back-compat.
    """
    cfg = _client_config()
    if not cfg.insecure:
        return True
    if not getattr(args, "_insecure_warning_shown", False):
        requests.packages.urllib3.disable_warnings(  # type: ignore[attr-defined]
            category=requests.packages.urllib3.exceptions.InsecureRequestWarning
        )
        print("Warning: TLS certificate verification is disabled.", file=sys.stderr)
        args._insecure_warning_shown = True
    return False


def resolve_token(args: argparse.Namespace) -> str:
    """Resolve the bearer credential from ``~/.config/qatlas/hosts.yml``.

    The store is populated by ``qatlas auth login`` (browser OAuth /
    ``--with-token`` from stdin). ``server_url:`` in config.yaml /
    ``--server-url`` CLI flag picks WHICH host's token to use.

    ``args`` kept for signature back-compat (used to honour
    ``--token`` CLI flag, removed in v0.17.0; config.yaml ``token:``
    field removed in v0.19.0 — it silently shadowed all per-host
    tokens in hosts.yml).

    An empty return value means no Authorization header will be set;
    the server then either serves open reads or replies 401 for write
    endpoints. The 401 body always points the user at ``/pat``
    (top-level redirect to ``/<lang>/pat``, defined in
    ``web/src/routes/pat.tsx``) regardless of language.
    """
    try:
        from qatlas.client.auth import get_stored_token  # local import to avoid cycle

        return get_stored_token(default_base_url())
    except Exception:
        # Defensive: never let a config-file glitch break unrelated commands.
        return ""


def auth_headers(args: argparse.Namespace) -> dict[str, str]:
    """Build the Authorization header for a CLI request.

    Returns an empty dict when no token is configured so callers can
    safely splat ``{**auth_headers(args), ...other...}``.
    """
    token = resolve_token(args)
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def add_common_http_args(parser: argparse.ArgumentParser) -> None:
    """Register the shared ``--request-timeout`` flag.

    v0.17.0 removed ``--base-url`` / ``--token`` / ``--insecure``
    flags — those fields now live exclusively in
    ``~/.config/qatlas/config.yaml``. ``--request-timeout`` is kept
    because it's an in-call ergonomic knob (raise the timeout for a
    slow MinerU poll), not a persistent config concern.
    """
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=120.0,
        help="HTTP request timeout in seconds (per-call override).",
    )


def run_with_request_errors(func, *args, **kwargs) -> int:
    """Convert ValueError / RequestException into standard CLI exit codes."""
    try:
        return func(*args, **kwargs)
    except ValueError as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Client/server version negotiation (since v0.8.0)
# ---------------------------------------------------------------------------
#
# Contract (since qatlas-cli 0.22.1 / qatlasd 0.22.0): client and server are
# compatible iff their (major, minor) versions are EQUAL. Patch-level
# differences are always fine — compatibility fixes only ever bump the patch
# component, so qatlasd 0.22.4 ↔ qatlas-cli 0.22.3 is a supported pairing.
# A (major, minor) MISMATCH in either direction is suspect:
#
#   * server newer — the server's wire contract may have moved (new endpoints
#     / fields this client doesn't know). Write ops hard-fail (SystemExit 4)
#     because silent breakage is the worst failure mode; read ops get a
#     one-shot stderr warning and keep going.
#   * client newer — the client may call endpoints the server doesn't have
#     yet. Always a one-shot stderr warning (never a hard fail): the operator
#     controls the server, not us, and most old endpoints still work.
#
# Mechanism:
#   1. Every request adds  `X-Qatlas-Client-Version: <version>`  (headers
#      injected by client_version_headers()). The server logs / future
#      rate-limit policies can use it.
#   2. Every response (when from a v0.8.0+ server) includes
#      `X-Qatlas-Server-Version: <version>`. The client compares major+minor
#      against its own __version__.
#   3. If the header is absent (older server) the client treats the
#      server as "unknown version" and silently skips negotiation —
#      forward-compatible with pre-v0.8.0 deployments.

_VERSION_TUPLE_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:[.+-].*)?$")


def _parse_semver(v: str) -> tuple[int, int] | None:
    """Return (major, minor) for a semver string, or None if unparseable."""
    if not v:
        return None
    m = _VERSION_TUPLE_RE.match(v.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def client_version_headers() -> dict[str, str]:
    """Headers every outgoing request should include for version negotiation."""
    return {"X-Qatlas-Client-Version": _CLIENT_VERSION}


_WARNED_VERSION_MISMATCH: set[str] = set()


def check_response_version(response: requests.Response, *, write: bool) -> None:
    """Compare X-Qatlas-Server-Version against this client's version.

    Compatibility contract: equal (major, minor) ⇒ compatible; patch drift
    is ignored on purpose. On a (major, minor) mismatch:

    * `write=True` callers (POST/PUT/PATCH/DELETE) hard-fail (SystemExit 4)
      only when the server is NEWER than the client — the server's wire
      contract may have moved and silent breakage is the worst failure
      mode.
    * Every other mismatch (reads against a newer server, or any call
      against an older server) emits a one-shot stderr warning naming both
      versions and the suggested action, then lets the call through.

    Older servers (pre-v0.8.0) don't send the header — we treat the
    absence as "unknown" and do nothing, preserving the new client's
    ability to talk to legacy deployments.
    """
    server_version = response.headers.get("X-Qatlas-Server-Version", "").strip()
    if not server_version:
        return  # pre-v0.8.0 server, skip negotiation
    server = _parse_semver(server_version)
    client = _parse_semver(_CLIENT_VERSION)
    if server is None or client is None:
        return  # unparseable on either side — fail open, don't block calls
    if server == client:
        return  # (major, minor) match — compatible, patch drift is fine
    if server > client:
        msg = (
            f"server version {server_version} is newer than client {_CLIENT_VERSION}.\n"
            f"This client may not understand new endpoints/fields. Upgrade with:\n"
            f"  uv tool upgrade qatlas-cli"
        )
        if write:
            print(f"ERROR: {msg}", file=sys.stderr)
            raise SystemExit(4)
        warn_key = f"newer-server:{server_version}"
    else:
        msg = (
            f"server version {server_version} is older than client {_CLIENT_VERSION}.\n"
            f"The server may lack endpoints/fields this client expects. "
            f"Ask the server operator to upgrade qatlasd to the "
            f"{client[0]}.{client[1]}.x line."
        )
        warn_key = f"older-server:{server_version}"
    if warn_key not in _WARNED_VERSION_MISMATCH:
        print(f"WARNING: {msg}", file=sys.stderr)
        _WARNED_VERSION_MISMATCH.add(warn_key)
