"""Shared HTTP layer for qatlas client plugin commands.

This is the public, semver-stable surface plugin packages should build on
(protocol v2): it applies the same conventions the built-in commands use, so
plugin traffic authenticates, negotiates versions and reports errors
consistently with the rest of the CLI.

Exit-code conventions for plugin commands (mirroring the core CLI):

* ``0`` success; non-zero failure;
* ``1`` transport / server error (requests exception, 5xx left to caller);
* ``2`` invalid input (bad arguments);
* ``4`` version-negotiation hard failure (write op against a newer server —
  raised as ``SystemExit(4)`` by :func:`server_request` via the shared
  ``check_response_version`` policy).
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

import requests

from qatlas.client.plugins.base import CliContext

_WARNED_INSECURE = False


def _verify_flag(ctx: CliContext) -> bool:
    global _WARNED_INSECURE
    if not ctx.insecure:
        return True
    if not _WARNED_INSECURE:
        requests.packages.urllib3.disable_warnings(  # type: ignore[attr-defined]
            category=requests.packages.urllib3.exceptions.InsecureRequestWarning
        )
        print("Warning: TLS certificate verification is disabled.", file=sys.stderr)
        _WARNED_INSECURE = True
    return False


def server_request(
    ctx: CliContext,
    method: str,
    path: str,
    *,
    json: Any | None = None,
    params: dict[str, Any] | None = None,
    write: bool = False,
    timeout: float | None = None,
) -> requests.Response:
    """Perform one authenticated request against the configured qatlasd.

    Applies, in order: the stored PAT as ``Authorization: Bearer`` (omitted
    when unset), the ``X-Qatlas-Client-Version`` negotiation header, the
    request timeout, and the TLS-verification override from the client
    config. On a ``(major, minor)`` version mismatch the shared policy in
    ``qatlas.client._common.check_response_version`` applies — write
    operations against a newer server hard-fail with ``SystemExit(4)``.

    ``path`` is joined onto ``ctx.server_base_url`` (leading slash optional).
    Raises ``ValueError`` when no server is configured.
    """
    from qatlas.client._common import check_response_version, client_version_headers

    base = (ctx.server_base_url or "").rstrip("/")
    if not base:
        raise ValueError(
            "no server configured — set one with `qatlas config set server_url URL`"
        )
    url = f"{base}/{path.lstrip('/')}"
    headers = {**ctx.auth_headers(), **client_version_headers()}
    response = requests.request(
        method,
        url,
        json=json,
        params=params,
        headers=headers,
        timeout=timeout or ctx.request_timeout,
        verify=_verify_flag(ctx),
    )
    check_response_version(response, write=write)
    return response


def format_api_error(response: requests.Response) -> str:
    """Render an HTTP error response as a one-line CLI message.

    Unwraps the server's ``{"detail": ...}`` JSON shape when present; falls
    back to a truncated body snippet. 401s additionally point at
    ``qatlas auth login``.
    """
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("message") or "")
        else:
            detail = str(body)
    except ValueError:
        detail = (response.text or "").strip()
    if len(detail) > 300:
        detail = detail[:300] + "…"
    message = f"HTTP {response.status_code}" + (f": {detail}" if detail else "")
    if response.status_code == 401:
        message += " (not authenticated — run `qatlas auth login`)"
    return message


def poll_lro(
    ctx: CliContext,
    path: str,
    *,
    done: Callable[[dict[str, Any]], bool],
    interval: float = 2.0,
    timeout: float | None = None,
    params: dict[str, Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Poll a status endpoint until ``done(payload)`` is true.

    Mirrors the LRO polling style of the built-in ``paper status`` command:
    GET ``path`` (optionally with ``params``), invoke the optional
    ``progress`` callback with each intermediate payload, sleep ``interval``
    seconds between polls. Returns the final payload. Raises
    ``TimeoutError`` when ``timeout`` seconds elapse first, and
    ``requests.HTTPError`` on any non-2xx poll.
    """
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        response = server_request(ctx, "GET", path, params=params)
        response.raise_for_status()
        payload = response.json()
        if done(payload):
            return payload
        if progress is not None:
            progress(payload)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"polling {path} exceeded the {timeout:.0f}s budget")
        time.sleep(interval)


__all__ = [
    "server_request",
    "format_api_error",
    "poll_lro",
    "CliContext",
]
