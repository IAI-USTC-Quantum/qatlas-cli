"""A ``requests``-native mock transport — the moral equivalent of
``httpx.MockTransport`` — for the block-comments CLI tests.

Routes are registered as ``(method, path) → handler(prepared_request) →
requests.Response``; every request that matches no route fails the test
loudly, and every dispatched request is recorded (``requests_seen``)
so tests can assert on headers (Idempotency-Key, If-Match,
Authorization), query params and JSON bodies.

Mount with :func:`mount` by monkeypatching ``blockapi.http_session``.
"""

from __future__ import annotations

import json as _json
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter

Handler = Callable[[requests.PreparedRequest], requests.Response]


def make_response(
    status: int,
    *,
    json_body: Any = None,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
    url: str = "http://server.test/",
) -> requests.Response:
    """Build a real ``requests.Response`` with the given payload."""
    resp = requests.Response()
    resp.status_code = status
    resp.url = url
    resp.headers.update(headers or {})
    if json_body is not None:
        content = _json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        resp.headers.setdefault("Content-Type", "application/json")
    elif not content and status >= 400:
        # Error bodies default to a JSON-ish envelope shape.
        content = b'{"detail": "error"}'
        resp.headers.setdefault("Content-Type", "application/json")
    resp._content = content
    resp._content_consumed = True
    return resp


class _MockAdapter(HTTPAdapter):
    def __init__(self, transport: "MockTransport") -> None:
        super().__init__()
        self._transport = transport

    # pylint: disable=arguments-differ
    def send(self, request: requests.PreparedRequest, **kwargs: Any):
        return self._transport._dispatch(request)


class MockTransport:
    """Route table + recorded requests for offline HTTP fixtures."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}
        self.requests_seen: list[requests.PreparedRequest] = []

    def route(self, method: str, path: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self._routes[(method.upper(), path)] = handler
            return handler

        return register

    def add(self, method: str, path: str, handler: Handler) -> None:
        self._routes[(method.upper(), path)] = handler

    def _dispatch(self, request: requests.PreparedRequest) -> requests.Response:
        self.requests_seen.append(request)
        parsed = urlparse(request.url)
        key = (request.method.upper(), parsed.path)
        handler = self._routes.get(key)
        if handler is None:
            raise AssertionError(
                f"unexpected HTTP request {key[0]} {parsed.path}?{parsed.query} — "
                f"registered routes: {sorted(self._routes)}"
            )
        return handler(request)

    # ── inspection helpers ────────────────────────────────────────

    @staticmethod
    def query_of(request: requests.PreparedRequest) -> dict[str, list[str]]:
        return parse_qs(urlparse(request.url).query)

    @staticmethod
    def json_body_of(request: requests.PreparedRequest) -> Any:
        return _json.loads(request.body.decode("utf-8")) if request.body else None


def mount(monkeypatch, transport: MockTransport) -> None:
    """Point ``blockapi.http_session`` at a session wired to ``transport``."""

    def factory() -> requests.Session:
        session = requests.Session()
        adapter = _MockAdapter(transport)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    from qatlas.client import blockapi

    monkeypatch.setattr(blockapi, "http_session", factory)


def stub_common(monkeypatch, *, token: str = "test-token") -> None:
    """Stub config-derived helpers the block commands pull in.

    Mirrors the pattern in ``test_paper_cli.py``: pin the base URL and
    auth so nothing reads ``~/.config/qatlas``, and neutralize the
    write preflight probe (which uses module-level ``requests.get``).
    """
    from qatlas.client import blockapi, blocks, comments

    monkeypatch.setattr(blocks, "base_url_from_args", lambda args: "http://server.test")
    monkeypatch.setattr(comments, "base_url_from_args", lambda args: "http://server.test")
    monkeypatch.setattr(blockapi, "auth_headers", lambda args: {"Authorization": f"Bearer {token}"})
    monkeypatch.setattr(blockapi, "client_version_headers", lambda: {})
    monkeypatch.setattr(blockapi, "request_verify", lambda args: True)
    monkeypatch.setattr(blockapi, "check_response_version", lambda resp, **kw: None)
    monkeypatch.setattr(
        blockapi, "check_server_before_write", lambda *a, **kw: None
    )
