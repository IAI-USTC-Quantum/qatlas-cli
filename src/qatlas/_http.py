"""Transport diagnostics for one-shot writes; never retries or changes errors."""

from __future__ import annotations

import sys
from contextvars import ContextVar
from typing import Any, Callable

import requests

UNKNOWN_WRITE_HINT = (
    "Write result is UNKNOWN — the request may have reached the server even "
    "though its response was lost. No automatic retry was attempted. Check "
    "server state before resubmitting; non-idempotent writes may duplicate work."
)


# A watch loop may catch a transport exception and turn it into a return value.
# Track only an integer in this execution context so it cannot silently submit
# the same candidates on its next pass; no URLs, payloads or credentials persist.
_UNKNOWN_WRITE_COUNT: ContextVar[int] = ContextVar("qatlas_unknown_writes", default=0)


def unknown_write_count() -> int:
    return _UNKNOWN_WRITE_COUNT.get()


def write_request(
    request: Callable[..., requests.Response], *args: Any, **kwargs: Any
) -> requests.Response:
    """Call an actual write only, AFTER any preflight has succeeded.

    Emit the ambiguous-outcome diagnostic at the transport seam, including for
    library/plugin callers, and re-raise the identical requests exception. A
    note preserves the diagnostic in tracebacks without changing its type,
    message, response or the caller's existing exit-code contract. Do not wrap
    preflight, local validation or HTTP-response handling in this helper.
    """
    try:
        return request(*args, **kwargs)
    except requests.RequestException as exc:
        _UNKNOWN_WRITE_COUNT.set(_UNKNOWN_WRITE_COUNT.get() + 1)
        print(f"WARNING: {UNKNOWN_WRITE_HINT}", file=sys.stderr)
        exc.add_note(UNKNOWN_WRITE_HINT)
        raise
