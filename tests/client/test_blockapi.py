"""Unit tests for the block-comments API client layer (plan §12.2)."""

from __future__ import annotations

import argparse

import pytest

from qatlas.client import blockapi
from tests.client.mocktransport import make_response


def _args() -> argparse.Namespace:
    return argparse.Namespace(request_timeout=5.0)


# ---------------------------------------------------------------------------
# idempotency_key — deterministic SHA-256(method|path|body)
# ---------------------------------------------------------------------------


def test_idempotency_key_deterministic_and_body_sensitive():
    a = blockapi.idempotency_key("POST", "/api/papers/qa_x/discussions", '{"body":"hi"}')
    b = blockapi.idempotency_key("post", "/api/papers/qa_x/discussions", '{"body":"hi"}')
    c = blockapi.idempotency_key("POST", "/api/papers/qa_x/discussions", '{"body":"ho"}')
    assert a == b  # retry of the same logical request re-derives the key
    assert a != c  # different body → different key (server 409 territory)
    assert len(a) == 64


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def test_check_body_text_accepts_limit_and_rejects_over():
    ok = "x" * blockapi.MAX_BODY_CHARS
    blockapi.check_body_text(ok)  # exactly at the limit is fine
    with pytest.raises(blockapi.ApiError) as excinfo:
        blockapi.check_body_text(ok + "x")
    assert excinfo.value.exit_code == blockapi.EXIT_USAGE
    assert "split" in excinfo.value.message


def test_clamp_per_page_bounds():
    assert blockapi.clamp_per_page(None) == blockapi.PER_PAGE_DEFAULT
    assert blockapi.clamp_per_page(1) == 1
    assert blockapi.clamp_per_page(100) == 100
    for bad in (0, -1, 101):
        with pytest.raises(blockapi.ApiError):
            blockapi.clamp_per_page(bad)


# ---------------------------------------------------------------------------
# error mapping (§9 shared error contract)
# ---------------------------------------------------------------------------


def _err_for_status(status: int, *, text: str = "", json_body=None, headers=None):
    kwargs: dict = {"headers": headers}
    if json_body is not None:
        kwargs["json_body"] = json_body
    else:
        kwargs["content"] = text.encode() or b"plain"
    resp = make_response(status, **kwargs)
    return blockapi._error_from_response(resp, what="op", write=False)


@pytest.mark.parametrize(
    "status,kind,exit_code",
    [
        (401, "unauthorized", blockapi.EXIT_UNAUTHORIZED),
        (403, "forbidden", blockapi.EXIT_FORBIDDEN),
        (409, "conflict", blockapi.EXIT_CONFLICT),
        (413, "too_large", blockapi.EXIT_TOO_LARGE),
        (429, "rate_limited", blockapi.EXIT_RATE_LIMITED),
    ],
)
def test_status_to_structured_error(status, kind, exit_code):
    err = _err_for_status(status, json_body={"detail": "boom"})
    assert err.kind == kind
    assert err.exit_code == exit_code


def test_429_hint_includes_retry_after():
    err = _err_for_status(
        429, json_body={"detail": "slow down"}, headers={"Retry-After": "30"}
    )
    assert err.exit_code == blockapi.EXIT_RATE_LIMITED
    assert "Retry-After: 30" in (err.hint or "")


def test_401_hint_mentions_pat_and_cache_policy():
    err = _err_for_status(401, json_body={"detail": "no"})
    assert "auth login" in (err.hint or "")
    assert "NOT served" in (err.hint or "")


def test_json_404_is_not_found_not_unsupported():
    err = _err_for_status(404, json_body={"detail": "paper not found"})
    assert err.kind == "not_found"
    assert err.exit_code == blockapi.EXIT_NOT_FOUND


def test_html_404_is_unsupported_capability():
    err = _err_for_status(404, text="<html>404 page not found</html>")
    assert err.kind == "unsupported"
    assert err.exit_code == blockapi.EXIT_UNSUPPORTED
    assert "upgrade" in (err.hint or "")


def test_410_is_unsupported_capability():
    err = _err_for_status(410, json_body={"detail": "gone"})
    assert err.kind == "unsupported"
    assert err.exit_code == blockapi.EXIT_UNSUPPORTED


def test_500_is_transport_family():
    err = _err_for_status(500, json_body={"detail": "oops"})
    assert err.kind == "server"
    assert err.exit_code == blockapi.EXIT_TRANSPORT


def test_error_render_includes_status_detail_and_body():
    err = _err_for_status(403, json_body={"detail": "no scope"})
    text = err.render()
    assert "HTTP 403" in text
    assert "no scope" in text
    assert '"detail"' in text  # full envelope preserved for tooling


# ---------------------------------------------------------------------------
# run_cli translation + write timeout semantics
# ---------------------------------------------------------------------------


def test_run_cli_maps_api_error_to_exit_code(capsys):
    def boom(args):
        raise blockapi.ApiError("nope", kind="conflict", exit_code=blockapi.EXIT_CONFLICT)

    assert blockapi.run_cli(boom, _args()) == blockapi.EXIT_CONFLICT
    err = capsys.readouterr().err
    assert "nope" in err


def test_transport_error_on_write_mentions_unknown_result():
    import requests as _requests

    def boom(args):
        raise _requests.ConnectionError("reset")

    # _request wraps RequestException into ApiError with the write hint.
    args = _args()
    args.request_timeout = 1.0
    caught = None
    try:
        blockapi._request(
            args,
            "POST",
            "/api/papers/qa_x/discussions",
            what="create",
            json_body={"body": "hi"},
            write=True,
            base_url="http://127.0.0.1:1",  # nothing listens; loopback-safe
        )
    except blockapi.ApiError as exc:
        caught = exc
    assert caught is not None
    assert caught.kind == "transport"
    assert "UNKNOWN" in (caught.hint or "")
    assert "Idempotency-Key" in (caught.hint or "")


def test_get_json_rejects_non_json_2xx():
    # A 200 whose body is HTML (e.g. proxy error page) must not pass as
    # data — requests' own .json() raises, and the guard maps it to a
    # structured bad_content error.
    resp = make_response(200, content=b"<html/>", headers={"Content-Type": "text/html"})

    class _Session:
        def request(self, *a, **kw):
            return resp

    from qatlas.client import blockapi as api

    orig = api.http_session
    api.http_session = lambda: _Session()
    try:
        with pytest.raises(blockapi.ApiError) as excinfo:
            api.get_json(_args(), "/x", what="op", base_url="http://server.test")
    finally:
        api.http_session = orig
    assert excinfo.value.kind == "bad_content"
