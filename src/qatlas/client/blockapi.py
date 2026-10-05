"""Low-level HTTP client for the block-comments API (plan §12.2).

This module is the single seam through which the ``qatlas paper
pdf|parse-*|block-*`` and ``qatlas comments …`` commands talk to the
server. It exists so that:

* the §12.2 wire contract (idempotency key, ``If-Match`` CAS, keyset
  cursor pagination, error envelope) is implemented **once**;
* every response passes through the shared version-negotiation /
  auth-header / error-rendering machinery the rest of the client uses;
* tests can mount a mock transport (a ``requests.Session`` whose
  adapter returns fixture responses — the ``requests`` equivalent of
  ``httpx.MockTransport``) on :func:`http_session` without patching
  N call sites.

Nothing here prints to stdout: CLI formatting lives in
:mod:`qatlas.client.blocks` / :mod:`qatlas.client.comments`. Errors are
raised as :class:`ApiError`, which the CLI wrappers translate into the
structured exit-code contract below (also documented in both command
helps):

===  ==========================================================
 0  success
 1  transport failure / server 5xx / invalid response content
 2  invalid input (client-side validation)
 3  not found (paper / source / revision / block / discussion)
 4  unauthorized (HTTP 401)
 5  forbidden (HTTP 403 — scope or ownership)
 6  conflict (HTTP 409 — idempotency mismatch / revision CAS)
 7  payload too large (HTTP 413)
 8  rate limited (HTTP 429)
 9  server does not support this capability (old qatlasd)
===  ==========================================================
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any, Callable
from urllib.parse import quote

import requests

from qatlas.client._common import (
    auth_headers,
    check_response_version,
    check_server_before_write,
    client_version_headers,
    request_verify,
)

# Mirrors the §12.2 general contract: body cap 20,000 Unicode chars
# (server config comments.max_body_chars; the CLI mirrors the default
# so obvious over-length bodies fail fast client-side without a
# round-trip).
MAX_BODY_CHARS = 20_000
# Request body cap from §12.2 (1 MiB). A legal body + envelope cannot
# approach this, but the check keeps a pathological --body-file honest.
MAX_REQUEST_BYTES = 1 << 20
# Pagination contract: per_page default 20, max 100.
PER_PAGE_DEFAULT = 20
PER_PAGE_MAX = 100

EXIT_OK = 0
EXIT_TRANSPORT = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_UNAUTHORIZED = 4
EXIT_FORBIDDEN = 5
EXIT_CONFLICT = 6
EXIT_TOO_LARGE = 7
EXIT_RATE_LIMITED = 8
EXIT_UNSUPPORTED = 9

_STATUS_EXIT = {
    401: EXIT_UNAUTHORIZED,
    403: EXIT_FORBIDDEN,
    409: EXIT_CONFLICT,
    413: EXIT_TOO_LARGE,
    429: EXIT_RATE_LIMITED,
}


class ApiError(Exception):
    """A structured error from (or about) the block-comments API.

    ``kind`` is one of ``not_found|unauthorized|forbidden|conflict|
    too_large|rate_limited|unsupported|server|bad_content|transport``;
    ``exit_code`` follows the table in the module docstring. ``body``
    carries the parsed server envelope (dict) or raw text, whatever
    was decipherable.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        exit_code: int,
        status: int | None = None,
        body: Any = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.exit_code = exit_code
        self.status = status
        self.body = body
        self.hint = hint

    def render(self) -> str:
        """Human-oriented multi-line rendering for stderr."""
        lines = [f"error[{self.kind}]: {self.message}"]
        if self.status is not None:
            lines[0] = f"HTTP {self.status}: {lines[0]}"
        if isinstance(self.body, dict) and self.body:
            try:
                lines.append(
                    json.dumps(self.body, ensure_ascii=False, indent=2, sort_keys=True)
                )
            except (TypeError, ValueError):
                pass
        elif isinstance(self.body, str) and self.body.strip():
            text = self.body.strip()
            if len(text) > 2000:
                text = text[:2000] + "…(truncated)"
            lines.append(text)
        if self.hint:
            lines.append(f"hint: {self.hint}")
        return "\n".join(lines)


def idempotency_key(method: str, path: str, body: str) -> str:
    """Compute the §12.2 ``Idempotency-Key`` header value.

    Deterministic: SHA-256 over ``METHOD + path + body``. Resubmitting the
    exact same logical request re-derives the same key. Comment endpoints
    replay the original result and reject a changed body under the same key
    with 409; other endpoints, including source registration, have their own
    idempotency contracts. This helper does not retry or guarantee replay.
    """
    digest = hashlib.sha256(f"{method.upper()}|{path}|{body}".encode("utf-8"))
    return digest.hexdigest()


def _session_factory() -> requests.Session:
    """Build the HTTP session used for every block-comments call.

    Tests monkeypatch this single seam with a session whose adapter
    serves fixture responses (mock-first; real-backend runs are Q5).
    """
    return requests.Session()


http_session: Callable[[], requests.Session] = _session_factory


def _looks_like_endpoint_missing(resp: requests.Response) -> bool:
    """Heuristic: did the route itself not exist (vs. a missing object)?

    qatlasd answers 404 with a JSON envelope when a *resource* is
    missing. A 404 whose body is not JSON (plain text, an HTML page
    from a reverse proxy, …) — or any 410 — means the *endpoint* is not
    implemented by this (older) server.
    """
    if resp.status_code == 410:
        return True
    if resp.status_code != 404:
        return False
    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        return True
    try:
        resp.json()
    except (ValueError, json.JSONDecodeError):
        return True
    return False


def _parse_error_body(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        text = resp.text or ""
        return text.strip() or None


def _error_from_response(
    resp: requests.Response,
    *,
    what: str,
    write: bool,
    url_path: str = "",
) -> ApiError:
    """Map an HTTP error response onto the structured ApiError table."""
    body = _parse_error_body(resp)
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("detail") or body.get("error") or "")
    status = resp.status_code
    source_registration = url_path == "/api/papers/source-register"

    if status in _STATUS_EXIT:
        kind = {
            401: "unauthorized",
            403: "forbidden",
            409: "conflict",
            413: "too_large",
            429: "rate_limited",
        }[status]
        hint = None
        if status == 401:
            hint = (
                "authentication required — run `qatlas auth login` or create a "
                "PAT at <server>/pat; cached files are NOT served on 401"
            )
        elif status == 403 and write:
            hint = (
                "source registration requires the papers:write scope"
                if source_registration else
                "writing comments requires a user PAT/session with the "
                "comments:write scope (system PATs are read-only)"
            )
        elif status == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                hint = f"Retry-After: {retry_after}s — wait before retrying"
        return ApiError(
            f"{what} failed: {detail or resp.reason or 'request rejected'}",
            kind=kind,
            exit_code=_STATUS_EXIT[status],
            status=status,
            body=body,
            hint=hint,
        )

    # Registration has no resource-id path segment; its 404 is a missing route,
    # including PocketBase's JSON 404 envelope (upstream PDF errors are 422).
    if _looks_like_endpoint_missing(resp) or (source_registration and status == 404):
        if url_path.startswith("/api/papers/") and url_path.endswith("/pdf") and "/sources/" not in url_path:
            return ApiError(
                f"{what} failed: paper access is disabled or this server does not "
                f"implement direct PDF delivery (HTTP {status})",
                kind="unsupported", exit_code=EXIT_UNSUPPORTED,
                status=status, body=body,
                hint="ask the server operator to enable paper_access or upgrade qatlasd",
            )
        if source_registration:
            return ApiError(
                f"{what} failed: the server does not implement external source "
                f"registration (HTTP {status})",
                kind="unsupported", exit_code=EXIT_UNSUPPORTED,
                status=status, body=body,
                hint="ask the server operator to upgrade qatlasd for source registration",
            )
        return ApiError(
            f"{what} failed: the server does not implement the "
            f"block-comments endpoints (HTTP {status})",
            kind="unsupported",
            exit_code=EXIT_UNSUPPORTED,
            status=status,
            body=body,
            hint=(
                "these commands need a qatlasd with block-level comments "
                "(Q1 originals/blocks + Q2 comments); ask the server operator "
                "to upgrade"
            ),
        )

    if status == 404:
        return ApiError(
            f"{what} failed: not found{f' — {detail}' if detail else ''}",
            kind="not_found",
            exit_code=EXIT_NOT_FOUND,
            status=status,
            body=body,
        )

    return ApiError(
        f"{what} failed: HTTP {status} {resp.reason or ''} {detail}".strip(),
        kind="server" if status >= 500 else "bad_content",
        exit_code=EXIT_TRANSPORT,
        status=status,
        body=body,
    )


def _request(
    args: Any,
    method: str,
    url_path: str,
    *,
    what: str,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    extra_headers: dict[str, str] | None = None,
    write: bool = False,
    stream: bool = False,
    base_url: str,
) -> requests.Response:
    """Perform one HTTP request through the shared session + policies.

    Applies auth headers, client version negotiation, the write
    preflight guard, and (for writes) automatic ``Idempotency-Key``.
    Raises :class:`ApiError` for any non-2xx response or transport
    failure; never prints.
    """
    headers = {**auth_headers(args), **client_version_headers(), **(extra_headers or {})}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(json_body, ensure_ascii=False)
        if write:
            # Deterministic key for the exact request. Replay guarantees
            # belong to each server endpoint; never automatically retry.
            headers["Idempotency-Key"] = idempotency_key(method, url_path, payload)
    else:
        payload = None

    verify = request_verify(args)
    url = f"{base_url.rstrip('/')}{url_path}"
    session = http_session()
    request_started = False
    try:
        if write:
            check_server_before_write(
                base_url, headers=headers, timeout=args.request_timeout, verify=verify
            )
        # Only failures after this point can have an ambiguous write outcome.
        request_started = True
        resp = session.request(
            method,
            url,
            params=params,
            data=payload.encode("utf-8") if payload is not None else None,
            headers=headers,
            timeout=args.request_timeout,
            verify=verify,
            stream=stream,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        hint = None
        if write:
            if not request_started:
                hint = "Write request was not sent; resolve the preflight failure first."
            else:
                hint = (
                    "Write result is UNKNOWN — the request may have reached the "
                    "server. No automatic retry was attempted. The same method, "
                    "path and body re-derive the same Idempotency-Key; check server "
                    "state and the endpoint's idempotency contract before resubmitting. "
                    "Do not assume failure and double-submit different content."
                )
        raise ApiError(
            f"{what} failed: {exc}",
            kind="transport",
            exit_code=EXIT_TRANSPORT,
            hint=hint,
        ) from exc
    check_response_version(resp, write=write, request_sent=True)
    if not 200 <= resp.status_code < 300:
        raise _error_from_response(resp, what=what, write=write, url_path=url_path)
    return resp


def get_json(
    args: Any,
    url_path: str,
    *,
    what: str,
    params: dict[str, Any] | None = None,
    base_url: str,
) -> dict[str, Any]:
    """GET a JSON document; raise ``bad_content`` on non-JSON 2xx."""
    resp = _request(args, "GET", url_path, what=what, params=params, base_url=base_url)
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApiError(
            f"{what}: server returned non-JSON body "
            f"(Content-Type: {resp.headers.get('Content-Type', '?')})",
            kind="bad_content",
            exit_code=EXIT_TRANSPORT,
            status=resp.status_code,
            body=resp.text[:2000],
        ) from exc
    if not isinstance(body, dict):
        raise ApiError(
            f"{what}: unexpected JSON shape (expected object, got "
            f"{type(body).__name__})",
            kind="bad_content",
            exit_code=EXIT_TRANSPORT,
            status=resp.status_code,
            body=body,
        )
    return body


def post_json(
    args: Any,
    url_path: str,
    *,
    what: str,
    json_body: dict[str, Any],
    base_url: str,
    expect_status: tuple[int, ...] = (200, 201),
) -> dict[str, Any]:
    """POST/PUT a JSON write; returns the parsed response body.

    2xx statuses outside ``expect_status`` are treated as bad content
    (the contract for these endpoints is precise).
    """
    resp = _request(
        args, "POST", url_path, what=what, json_body=json_body, write=True,
        base_url=base_url,
    )
    if resp.status_code not in expect_status:
        raise ApiError(
            f"{what}: unexpected HTTP {resp.status_code} (expected "
            f"{' or '.join(str(s) for s in expect_status)})",
            kind="bad_content",
            exit_code=EXIT_TRANSPORT,
            status=resp.status_code,
            body=_parse_error_body(resp),
        )
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApiError(
            f"{what}: server returned non-JSON body",
            kind="bad_content",
            exit_code=EXIT_TRANSPORT,
            status=resp.status_code,
        ) from exc


def patch_json(
    args: Any,
    url_path: str,
    *,
    what: str,
    json_body: dict[str, Any],
    if_match: str | int | None,
    base_url: str,
) -> dict[str, Any]:
    """PATCH a JSON write with the §12.2 ``If-Match`` CAS header.

    ``If-Match`` carries the current revision (ETag = revision integer,
    per §12.2). A stale revision surfaces as 409 → exit 6.
    """
    extra: dict[str, str] = {}
    if if_match is not None:
        extra["If-Match"] = str(if_match)
    resp = _request(
        args, "PATCH", url_path, what=what, json_body=json_body,
        extra_headers=extra, write=True, base_url=base_url,
    )
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApiError(
            f"{what}: server returned non-JSON body",
            kind="bad_content",
            exit_code=EXIT_TRANSPORT,
            status=resp.status_code,
        ) from exc


def iter_stream(resp: requests.Response) -> bytes:
    """Drain a streamed response into bytes (for hashable downloads)."""
    chunks: list[bytes] = []
    for chunk in resp.iter_content(chunk_size=1 << 16):
        if chunk:
            chunks.append(chunk)
    return b"".join(chunks)


def download_bytes(
    args: Any,
    url_path: str,
    *,
    what: str,
    expected_content_prefixes: tuple[str, ...],
    base_url: str,
    params: dict[str, Any] | None = None,
) -> tuple[bytes, requests.Response]:
    """GET binary content and guard against HTML/JSON masquerading.

    ``expected_content_prefixes`` are accepted ``Content-Type``
    prefixes (e.g. ``("application/pdf", "application/octet-stream")``).
    A 2xx with the wrong type — e.g. an HTML error page from a proxy —
    raises ``bad_content`` instead of writing garbage to stdout.
    Returns ``(data, response)`` so callers can read extra headers.
    """
    resp = _request(
        args, "GET", url_path, what=what, params=params, stream=True,
        base_url=base_url,
    )
    data = iter_stream(resp)
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if expected_content_prefixes and not any(
        content_type.startswith(p.lower()) for p in expected_content_prefixes
    ):
        preview = data[:400].decode("utf-8", errors="replace")
        raise ApiError(
            f"{what}: server returned Content-Type {content_type!r}, expected one "
            f"of {list(expected_content_prefixes)} — refusing to emit it as bytes",
            kind="bad_content",
            exit_code=EXIT_TRANSPORT,
            status=resp.status_code,
            body=preview,
        )
    return data, resp


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_path_segment(value: str) -> str:
    """Percent-encode one path segment (paper ids / revisions / ids)."""
    return quote(str(value), safe="")


def check_body_text(body: str, *, flag: str = "--body-file") -> None:
    """Client-side mirror of the §12.2 body budget (20k Unicode chars)."""
    if len(body) > MAX_BODY_CHARS:
        raise ApiError(
            f"body is {len(body)} characters, over the {MAX_BODY_CHARS}-character "
            f"limit; split the discussion into smaller posts",
            kind="usage",
            exit_code=EXIT_USAGE,
            hint=f"pass a file with {flag} and keep it under the limit",
        )
    if len(body.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ApiError(
            "body exceeds the 1 MiB request budget",
            kind="usage",
            exit_code=EXIT_USAGE,
        )


def clamp_per_page(per_page: int | None) -> int:
    """Validate per_page against the §12.2 pagination contract."""
    if per_page is None:
        return PER_PAGE_DEFAULT
    if not 1 <= per_page <= PER_PAGE_MAX:
        raise ApiError(
            f"--per-page must be between 1 and {PER_PAGE_MAX} (got {per_page})",
            kind="usage",
            exit_code=EXIT_USAGE,
        )
    return per_page


def run_cli(func: Callable[[Any], int], args: Any) -> int:
    """CLI wrapper: ApiError → structured stderr + mapped exit code.

    Complements ``run_with_request_errors`` (which keeps ValueError→2,
    RequestException→1) with the block-comments structured table.
    """
    try:
        return func(args)
    except ApiError as exc:
        print(exc.render(), file=sys.stderr)
        return exc.exit_code
    except ValueError as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT
