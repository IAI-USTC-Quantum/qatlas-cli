"""``qatlas paper`` — fetch paper assets from the server.

Subcommands::

    qatlas paper get markdown ID_OR_DOI [--output FILE | --to-stdout]
    qatlas paper get images   ID_OR_DOI [--output FILE | --to-stdout]
    qatlas paper get metadata ID_OR_DOI
    qatlas paper status       ID_OR_DOI [--kind markdown]
    qatlas paper list         [--has-md true] [--status …] [-q …] [--json]
    qatlas paper lookup       REF... [--json]
    qatlas paper fetch        ID|DOI|URL... [--file FILE] [--json]
    qatlas paper source-register URL --title TITLE --author NAME --year YYYY [--json]
    qatlas paper jobs         [--remote] [--watch] [--json]
    qatlas paper mineru-lease ID [--ttl-seconds N]

These wrap the server's paper-access endpoints (only registered when
``paper_access.enabled: true`` in the server's config.yaml):

    GET /api/papers/{id_or_doi}/markdown[/status]
    GET /api/papers/{id_or_doi}/images/zip

Exception: ``get metadata`` hits the paper-detail endpoint
``GET /api/papers/{id}``, which is plain registry metadata and always
available (no ``paper_access.enabled`` gate, no LRO, no side effects).
It prints the JSON response body verbatim; the registry does not store
abstracts, so none is included.

Use ``paper pdf`` for the authenticated, paper-access-gated
``GET /api/papers/{id}/pdf`` endpoint: source-pinned PDF bytes are streamed
and SHA-256-verified for canonical ids and aliases without enumerating sources.
Use ``paper read`` for an agent-friendly, resumable Middle-derived JSON view;
``paper parse-list`` / ``paper parse-json`` still expose immutable raw artifacts.
The markdown endpoint follows a long-running-operation contract: cache
miss returns 202 + ``Operation-Location``; we transparently poll until
``state == cached`` (or a terminal failure) then stream the bytes. The images/figures/image commands also handle new source-specific 202
operations with bounded same-origin polling and pinned return-GETs; old
servers' 404/no-conversion responses retain the legacy Markdown hint.

ID forms accepted (server-side auto-resolution):

* Canonical QAtlas paper id    ``qa_…`` (work identity, not a version pin)

* Versioned arxiv id          ``0811.3171v3`` / ``quant-ph/9508027v2``
* Bare arxiv id (no version)  ``0811.3171`` / ``quant-ph/9508027``
                              → server resolves to latest published vN
* Bare old-style (no category) ``9508027``
                              → server applies ``category=quant-ph`` default
* DOI                          ``10.1103/PhysRevLett.103.150502``
                              → server resolves the registered work and its assets

Whenever the server applies a default, this CLI prints a one-line
``Note:`` to stderr summarizing what it inferred (read from the
``X-QAtlas-Defaults-Applied`` response header). Use ``--quiet-notes``
to suppress.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests

from qatlas._http import write_request
from qatlas.client._common import (
    add_common_http_args,
    auth_headers,
    base_url_from_args,
    check_response_version,
    check_server_before_write,
    client_version_headers,
    print_json,
    request_verify,
    run_with_request_errors,
)
from qatlas.client.downloader import build_fetch_parser, build_jobs_parser


# Maximum total wall-time we spend polling the LRO status endpoint
# before giving up. Generous because MinerU conversions of dense
# papers can run several minutes; agents that want a tighter bound
# should pass --max-wait.
_DEFAULT_MAX_WAIT_S = 1800.0  # 30 minutes


# How long to back off when the server doesn't give us a Retry-After
# header on a 202 (rare; the server always sends 5 by default).
_DEFAULT_POLL_INTERVAL_S = 5.0


def _print_notes(response: requests.Response, *, quiet: bool) -> None:
    """Emit a stderr note when the server applied any inference defaults.

    Both byte-streaming GETs and JSON responses carry the same info
    via ``X-QAtlas-Defaults-Applied`` header; we read the header
    rather than the body so the same code works for 200 text/markdown
    and 200 application/zip.
    """
    if quiet:
        return
    defaults = response.headers.get("X-QAtlas-Defaults-Applied", "")
    requested = response.headers.get("X-QAtlas-Requested-Id", "")
    resolved = response.headers.get("X-QAtlas-Resolved-Id", "")
    if not defaults and not resolved:
        return
    bits = []
    if requested and resolved and requested != resolved:
        bits.append(f"{requested} → {resolved}")
    if defaults:
        # Legacy servers sent UTF-8 arrows in this header; requests decodes
        # header bytes as Latin-1. Repair only that known sequence, not the
        # whole field: genuine Latin-1 text must not be reinterpreted as UTF-8.
        bits.append(defaults.replace("\xe2\x86\x92", "→"))
    print(f"Note (server applied defaults): {'; '.join(bits)}", file=sys.stderr)


def _render_server_error(kind: str, resp: requests.Response) -> str:
    """Render the server's JSON error body verbatim for the operator.

    The paper-access endpoints return structured JSON on every 4xx/5xx
    (detail / kind / arxiv_id|doi|canonical / retry_after_iso /
    requested_id / resolved_id / defaults_applied). We dump the whole
    object — losing any of those fields swallows useful signal:

    * `kind` (fatal|retryable|daily_limit) tells the caller whether
      to retry at all.
    * `retry_after_iso` says when retry is sane (cooldown / quota
      reset).
    * `requested_id` / `resolved_id` / `defaults_applied` are echoed
      back even on failure so the caller can confirm "yes, you DID
      route my DOI to that arxiv id; the failure is downstream".

    Falls back to the raw body when JSON parsing fails so even an
    HTML error page from a misconfigured reverse proxy is visible.
    """
    parts: list[str] = []
    parts.append(f"{kind} fetch failed: HTTP {resp.status_code} {resp.reason}")
    try:
        body = resp.json()
    except json.JSONDecodeError:
        if resp.text:
            parts.append(resp.text.strip())
        return "\n".join(parts)
    if isinstance(body, dict):
        # Pretty-print so the operator's eye can find `detail` / `kind`
        # quickly; preserve every field for downstream tooling.
        parts.append(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        parts.append(json.dumps(body, ensure_ascii=False))
    return "\n".join(parts)


def _retry_after_seconds(response: requests.Response) -> float:
    """Parse the Retry-After header, falling back to default poll interval.

    RFC 7231 §7.1.3 lets it be either seconds or an HTTP-date; we
    only handle seconds because that's what qatlasd emits in
    practice. HTTP-date parsing is left for a follow-up if MinerU
    ever needs it.
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return _DEFAULT_POLL_INTERVAL_S
    try:
        seconds = float(raw)
        return max(1.0, seconds) if math.isfinite(seconds) else _DEFAULT_POLL_INTERVAL_S
    except ValueError:
        return _DEFAULT_POLL_INTERVAL_S


def _format_eta(body: dict[str, Any]) -> str:
    """Render a compact progress line from a JSON status body."""
    state = body.get("state", "?")
    phase = body.get("phase", "")
    parts = [state]
    if phase:
        parts.append(phase)
    if (fetch := body.get("fetch")):
        if (total := fetch.get("bytes_total")) and (got := fetch.get("bytes_received")):
            parts.append(f"fetch={got}/{total}B")
        elif (got := fetch.get("bytes_received")):
            parts.append(f"fetch={got}B")
    if (conv := body.get("convert")):
        if (stage := conv.get("stage")):
            parts.append(f"convert.{stage}")
        if (polls := conv.get("polled_count")):
            parts.append(f"polls={polls}")
    if (queue := body.get("queue")):
        if (pos := queue.get("position")):
            ahead = queue.get("ahead_of_me", 0)
            running = queue.get("running_count", 0)
            mc = queue.get("max_concurrent", 0)
            parts.append(f"queue=#{pos}({ahead}ahead,{running}/{mc}slot)")
        if (eta := queue.get("eta_seconds")):
            parts.append(f"eta={int(eta)}s")
    return " ".join(parts)


def _poll_until_cached(
    args: argparse.Namespace,
    base_url: str,
    status_url: str,
    *,
    cached_predicate,
    initial_response: requests.Response | None = None,
) -> tuple[int, dict[str, Any]]:
    """Poll the status endpoint until cache-hit or terminal failure.

    Returns (exit_code, last_body). exit_code == 0 means the asset is
    ready to GET; non-zero means we gave up (terminal failure, timeout).

    cached_predicate(body) returns True when the asset we want
    (markdown vs pdf) is ready. Lets us re-use this loop for both.
    """
    deadline = time.monotonic() + args.max_wait
    interval = _DEFAULT_POLL_INTERVAL_S
    last: dict[str, Any] = {}
    if initial_response is not None:
        delay = _retry_after_seconds(initial_response)
        remaining = deadline - time.monotonic()
        if remaining < delay:
            print(f"timed out after {args.max_wait}s before the next allowed status poll", file=sys.stderr)
            return 1, last
        time.sleep(delay)
    while True:
        if initial_response is not None and time.monotonic() >= deadline:
            print(f"timed out after {args.max_wait}s; last state={last.get('state', '?')}", file=sys.stderr)
            return 1, last
        try:
            resp = requests.get(
                status_url,
                headers={**auth_headers(args), **client_version_headers()},
                verify=request_verify(args),
                timeout=args.request_timeout,
            )
        except requests.RequestException as exc:
            print(f"poll request failed: {exc}", file=sys.stderr)
            return 1, last
        check_response_version(resp, write=False)
        if resp.status_code == 404:
            print(_render_server_error("status poll", resp), file=sys.stderr)
            return 1, last
        if not resp.ok:
            print(_render_server_error("status poll", resp), file=sys.stderr)
            return 1, last
        try:
            last = resp.json()
        except json.JSONDecodeError:
            print(f"status poll: non-JSON response\nHTTP {resp.status_code} {resp.reason}\n{resp.text}", file=sys.stderr)
            return 1, last
        if not isinstance(last, dict):
            print("status poll: expected a JSON object", file=sys.stderr)
            return 1, {}
        # Honor server-side Retry-After even on 200 status responses.
        interval = _retry_after_seconds(resp)
        if cached_predicate(last):
            return 0, last
        state = last.get("state", "")
        if state in {"failed", "cooldown"}:
            # Body-level terminal state (HTTP itself was 200). Render
            # the JSON body verbatim so kind / detail / retry_after_iso
            # / phase / requested_id / resolved_id / defaults_applied
            # all surface together — picking out fields individually
            # would lose signal the server worked hard to expose.
            print(
                f"job ended in terminal state ({state}):\n"
                + json.dumps(last, ensure_ascii=False, indent=2, sort_keys=True),
                file=sys.stderr,
            )
            return 1, last
        if state == "unavailable":
            print(
                "server-side fetch/convert unavailable:\n"
                + json.dumps(last, ensure_ascii=False, indent=2, sort_keys=True),
                file=sys.stderr,
            )
            return 1, last
        # In-flight: print compact progress line then sleep.
        if not args.quiet_progress:
            print(f"... waiting: {_format_eta(last)}", file=sys.stderr)
        if time.monotonic() > deadline:
            print(
                f"timed out after {args.max_wait}s; last state={state}",
                file=sys.stderr,
            )
            return 1, last
        # Sleep, but never longer than the remaining deadline.
        sleep_for = max(0.1, min(interval, deadline - time.monotonic()))
        time.sleep(sleep_for)


def _stream_to_output(response: requests.Response, output: str | None) -> int:
    """Write a successful 200 body to --output FILE or stdout."""
    if output is None or output == "-":
        out = sys.stdout.buffer
    else:
        out = open(output, "wb")  # noqa: SIM115 — closed below in finally
    try:
        for chunk in response.iter_content(chunk_size=1 << 16):
            if chunk:
                out.write(chunk)
    finally:
        if out is not sys.stdout.buffer:
            out.close()
    return 0


def _do_get(args: argparse.Namespace, kind: str) -> int:
    """Retrieve original Markdown with legacy and new readiness support."""
    if kind != "markdown":
        raise ValueError(f"unknown kind: {kind!r}")
    base_url = base_url_from_args(args)
    ident = args.id_or_doi.strip().lstrip("/")
    response, code = _get_with_content_wait(
        args, f"{base_url}/api/papers/{ident}/markdown", kind=kind, stream=True,
    )
    if response is None:
        return code
    try:
        _print_notes(response, quiet=args.quiet_notes)
        if response.status_code != 200:
            print(_render_server_error(kind, response), file=sys.stderr)
            return 1
        return _stream_to_output(response, args.output)
    finally:
        response.close()


def cmd_get_markdown(args: argparse.Namespace) -> int:
    return _do_get(args, "markdown")


def _read_json(response: requests.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        print(f"non-JSON read response:\nHTTP {response.status_code} {response.reason}\n{response.text}", file=sys.stderr)
        return None
    if not isinstance(body, dict):
        print("read: expected a JSON object", file=sys.stderr)
        return None
    return body


def cmd_read(args: argparse.Namespace) -> int:
    """Emit a resumable Middle-derived JSON view, never a raw parse artifact."""
    if args.block is not None and args.page is None:
        print("read: --block requires --page (both are 1-based)", file=sys.stderr)
        return 2
    if not math.isfinite(args.max_wait) or args.max_wait < 0:
        print("read: --max-wait must be finite and non-negative", file=sys.stderr)
        return 2
    base_url = base_url_from_args(args).rstrip("/")
    ident = args.id_or_doi.strip().lstrip("/")
    asset_url = f"{base_url}/api/papers/{quote(ident, safe='')}/read"
    params = {
        name: getattr(args, name)
        for name in ("source_id", "revision", "page", "block", "cursor", "limit")
        if getattr(args, name) is not None
    }

    def get_view() -> requests.Response:
        return requests.get(
            asset_url, params=params or None,
            headers={**auth_headers(args), **client_version_headers()},
            verify=request_verify(args), timeout=args.request_timeout,
        )

    resp = get_view()
    try:
        check_response_version(resp, write=False)
        _print_notes(resp, quiet=args.quiet_notes)
        if resp.status_code not in {200, 202}:
            print(_render_server_error("read", resp), file=sys.stderr)
            return 1
        body = _read_json(resp)
        if body is None:
            return 1
        if resp.status_code == 202:
            operation = body.get("operation")
            status_path = resp.headers.get("Operation-Location")
            if not status_path and isinstance(operation, dict):
                status_path = operation.get("status_url")
            if not isinstance(status_path, str) or not status_path:
                print("read: async response missing Operation-Location/status_url", file=sys.stderr)
                return 1
            status_url = urljoin(base_url + "/", status_path)
            target = urlsplit(status_url)
            origin = urlsplit(base_url)
            if (target.scheme, target.netloc) != (origin.scheme, origin.netloc) or target.username or target.password:
                print("read: refusing cross-origin operation URL (authentication must stay on the configured server)", file=sys.stderr)
                return 1
            if args.no_wait:
                # Include the header-only status locator in machine-readable async
                # output; do not make consumers scrape progress from stderr.
                body = dict(body)
                body["operation"] = {**(operation if isinstance(operation, dict) else {}), "status_url": status_path}
                body["retry_after"] = _retry_after_seconds(resp)
            else:
                if not args.quiet_progress:
                    print(f"async read started: {_format_eta(body)}", file=sys.stderr)
                # Hold source identity stable across a changing current pointer.
                if not params.get("source_id") and body.get("source_id"):
                    params["source_id"] = body["source_id"]
                initial = resp
                resp.close()
                code, last = _poll_until_cached(
                    args, base_url, status_url,
                    cached_predicate=lambda state: state.get("state") in {"ready", "cached", "done"},
                    initial_response=initial,
                )
                if code:
                    return code
                if not params.get("revision") and last.get("revision"):
                    params["revision"] = last["revision"]
                if not params.get("source_id") and last.get("source_id"):
                    params["source_id"] = last["source_id"]
                resp = get_view()
                check_response_version(resp, write=False)
                if resp.status_code != 200:
                    print(_render_server_error("read (post-parse GET)", resp), file=sys.stderr)
                    return 1
                body = _read_json(resp)
                if body is None:
                    return 1
        if resp.status_code == 200:
            # A proxy/misrouted raw Middle object must not masquerade as a
            # reading window. Preserve every field, but require the view's
            # identity, renderer and continuation envelope.
            strings = ("paper_id", "source_id", "revision", "renderer", "format", "content")
            if any(not isinstance(body.get(key), str) for key in strings) or any(
                not body.get(key) for key in strings if key != "content"
            ) or not isinstance(body.get("truncated"), bool) or "next_request" not in body:
                print("read: invalid derived-view envelope (not a source/revision-pinned reading view)", file=sys.stderr)
                return 1
            continuation = body["next_request"]
            if body["truncated"] and (
                not isinstance(continuation, dict) or not isinstance(continuation.get("cursor"), str)
                or not continuation["cursor"]
            ):
                print("read: truncated view is missing next_request.cursor", file=sys.stderr)
                return 1
            if ident.startswith("qa_") and body["paper_id"] != ident:
                print("read: server substituted the requested canonical paper identity", file=sys.stderr)
                return 1
            for pin in ("source_id", "revision"):
                if params.get(pin) and body.get(pin) != params[pin]:
                    print(f"read: server substituted {pin}: requested {params[pin]!r}, got {body.get(pin)!r}", file=sys.stderr)
                    return 1
        if args.output is None or args.output == "-":
            print_json(body)
        else:
            dest = Path(args.output).expanduser()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    finally:
        resp.close()


def _get_with_content_wait(
    args: argparse.Namespace, asset_url: str, *, kind: str, stream: bool = False,
) -> tuple[requests.Response | None, int]:
    """Preserve asset commands while source-specific content is prepared."""
    max_wait = getattr(args, "max_wait", _DEFAULT_MAX_WAIT_S)
    if not math.isfinite(max_wait) or max_wait < 0:
        print(f"{kind}: --max-wait must be finite and non-negative", file=sys.stderr)
        return None, 2
    base_url = base_url_from_args(args).rstrip("/")
    params: dict[str, Any] = {}

    def get() -> requests.Response:
        response = requests.get(
            asset_url, params=dict(params) or None,
            headers={**auth_headers(args), **client_version_headers()},
            verify=request_verify(args), timeout=args.request_timeout,
            stream=stream, allow_redirects=True,
        )
        check_response_version(response, write=False)
        return response

    response = get()
    if response.status_code != 202:
        return response, 0
    body = _read_json(response)
    if body is None:
        response.close()
        return None, 1
    _print_notes(response, quiet=args.quiet_notes)
    operation = body.get("operation")
    status_path = response.headers.get("Operation-Location") or (
        operation.get("status_url") if isinstance(operation, dict) else None
    ) or (asset_url + "/status" if kind == "markdown" else None)
    if not isinstance(status_path, str) or not status_path:
        print(f"{kind}: async response missing Operation-Location/status_url", file=sys.stderr)
        response.close()
        return None, 1
    status_url = urljoin(base_url + "/", status_path)
    target, origin = urlsplit(status_url), urlsplit(base_url)
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc) or target.username or target.password:
        print(f"{kind}: refusing cross-origin operation URL", file=sys.stderr)
        response.close()
        return None, 1
    if getattr(args, "no_wait", False):
        body = dict(body)
        body["operation"] = {**(operation if isinstance(operation, dict) else {}), "status_url": status_path}
        body["retry_after"] = _retry_after_seconds(response)
        print_json(body)
        response.close()
        return None, 0
    for pin in ("source_id", "revision"):
        if body.get(pin):
            params[pin] = body[pin]
    if not getattr(args, "quiet_progress", False):
        print(f"async {kind} started: {_format_eta(body)}", file=sys.stderr)
    initial = response
    response.close()
    code, last = _poll_until_cached(
        args, base_url, status_url,
        cached_predicate=lambda status: (
            status.get("state") in {"ready", "cached", "done"}
            and (status.get("ready", False) or status.get("md_ready", False))
        ),
        initial_response=initial,
    )
    if code:
        return None, code
    for pin in ("source_id", "revision"):
        if last.get(pin):
            if params.get(pin) and params[pin] != last[pin]:
                print(f"{kind}: status substituted {pin}", file=sys.stderr)
                return None, 1
            params[pin] = last[pin]
    response = get()
    if response.status_code != 200:
        print(_render_server_error(f"{kind} (post-parse GET)", response), file=sys.stderr)
        response.close()
        return None, 1
    for pin, header in (("source_id", "X-QAtlas-Source-Id"), ("revision", "X-QAtlas-Parse-Revision")):
        reported = response.headers.get(header)
        if reported and params.get(pin) and reported != params[pin]:
            print(f"{kind}: response substituted {pin}", file=sys.stderr)
            response.close()
            return None, 1
    return response, 0


def cmd_get_images(args: argparse.Namespace) -> int:
    """Fetch the images zip for a paper.

    Unlike markdown there is no LRO here: the zip is a byproduct of the
    MinerU conversion, so the endpoint is a plain read — 200 streams
    bytes, 404 means "not converted yet (or no images)". On 404 we
    probe the side-effect-free markdown status and, when markdown isn't
    ready either, tell the user to trigger the conversion first.
    """
    base_url = base_url_from_args(args)
    id_or_doi = args.id_or_doi.strip().lstrip("/")
    resp, code = _get_with_content_wait(
        args, f"{base_url}/api/papers/{id_or_doi}/images/zip", kind="images", stream=True,
    )
    if resp is None:
        return code
    if resp.status_code == 200:
        _print_notes(resp, quiet=args.quiet_notes)
        return _stream_to_output(resp, args.output)
    _print_notes(resp, quiet=args.quiet_notes)
    print(_render_server_error("images", resp), file=sys.stderr)
    if resp.status_code == 404 and not _markdown_ready(args, base_url, id_or_doi):
        print(
            f"hint: run `qatlas paper get markdown {id_or_doi}` first — the "
            "MinerU conversion it triggers is what produces the images zip.",
            file=sys.stderr,
        )
    return 1


def cmd_get_figures(args: argparse.Namespace) -> int:
    """Fetch a paper's figure/caption index as JSON.

    Plain read of ``GET /api/papers/{id}/figures`` — figure groups from
    the MinerU markdown with captions, per-image sizes and per-image
    download URLs. No LRO; answers 200 with ``markdown_ready: false``
    when the paper has no converted markdown yet.
    """
    base_url = base_url_from_args(args)
    id_or_doi = args.id_or_doi.strip().lstrip("/")
    resp, code = _get_with_content_wait(
        args, f"{base_url}/api/papers/{id_or_doi}/figures", kind="figures",
    )
    if resp is None:
        return code
    _print_notes(resp, quiet=args.quiet_notes)
    if not resp.ok:
        print(_render_server_error("figures", resp), file=sys.stderr)
        return 1
    try:
        print_json(resp.json())
    except json.JSONDecodeError:
        print(f"non-JSON figures response:\nHTTP {resp.status_code} {resp.reason}\n{resp.text}", file=sys.stderr)
        return 1
    return 0


def cmd_get_image(args: argparse.Namespace) -> int:
    """Download one image file from a paper's images bundle.

    ``name`` is the sha256-hex filename reported by
    ``qatlas paper get figures`` (its ``images[].name`` / ``url`` fields).
    Plain byte read of ``GET /api/papers/{id}/images/{name}``.
    """
    base_url = base_url_from_args(args)
    id_or_doi = args.id_or_doi.strip().lstrip("/")
    name = args.name.strip().strip("/")
    resp, code = _get_with_content_wait(
        args, f"{base_url}/api/papers/{id_or_doi}/images/{name}", kind="image", stream=True,
    )
    if resp is None:
        return code
    if resp.status_code == 200:
        _print_notes(resp, quiet=args.quiet_notes)
        return _stream_to_output(resp, args.output)
    _print_notes(resp, quiet=args.quiet_notes)
    print(_render_server_error("image", resp), file=sys.stderr)
    return 1


def _markdown_ready(args: argparse.Namespace, base_url: str, id_or_doi: str) -> bool:
    """Best-effort side-effect-free markdown readiness probe."""
    try:
        resp = requests.get(
            f"{base_url}/api/papers/{id_or_doi}/markdown/status",
            headers={**auth_headers(args), **client_version_headers()},
            verify=request_verify(args),
            timeout=args.request_timeout,
        )
        if not resp.ok:
            return False
        body = resp.json()
    except (requests.RequestException, json.JSONDecodeError):
        return False
    return isinstance(body, dict) and bool(body.get("md_ready") or body.get("ready"))


def cmd_get_metadata(args: argparse.Namespace) -> int:
    """Fetch a paper's registry metadata as JSON.

    Plain read of ``GET /api/papers/{id}`` — no LRO polling, no side
    effects. The id may be a ``qa_`` paper id, an arxiv id (new-style
    or old-style, with or without version) or a DOI. JSON-only: the
    whole response body goes to stdout via ``print_json``, mirroring
    the ``paper status`` output convention. No abstract — the registry
    does not store one.
    """
    base_url = base_url_from_args(args)
    id_or_doi = args.id_or_doi.strip().lstrip("/")
    resp = requests.get(
        f"{base_url}/api/papers/{id_or_doi}",
        headers={**auth_headers(args), **client_version_headers()},
        verify=request_verify(args),
        timeout=args.request_timeout,
    )
    check_response_version(resp, write=False)
    _print_notes(resp, quiet=args.quiet_notes)
    if not resp.ok:
        print(_render_server_error("metadata", resp), file=sys.stderr)
        return 1
    try:
        body = resp.json()
    except json.JSONDecodeError:
        print(
            f"non-JSON metadata response:\nHTTP {resp.status_code} {resp.reason}\n{resp.text}",
            file=sys.stderr,
        )
        return 1
    print_json(body)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Paginated registry listing — GET /api/papers with filters."""
    params: dict[str, str] = {}
    if args.has_md is not None:
        params["has_md"] = str(args.has_md).lower()
    if args.status:
        params["status"] = args.status
    if args.q:
        params["q"] = args.q
    if args.arxiv_id:
        params["arxiv_id"] = args.arxiv_id
    if args.doi:
        params["doi"] = args.doi
    if args.paper_id:
        params["paper_id"] = args.paper_id
    if args.page:
        params["page"] = str(args.page)
    if args.per_page:
        params["per_page"] = str(args.per_page)
    if args.sort:
        params["sort"] = args.sort
    resp = requests.get(
        f"{base_url_from_args(args)}/api/papers",
        params=params or None,
        headers={**auth_headers(args), **client_version_headers()},
        verify=request_verify(args),
        timeout=args.request_timeout,
    )
    check_response_version(resp, write=False)
    if not resp.ok:
        print(_render_server_error("list", resp), file=sys.stderr)
        return 1
    try:
        body = resp.json()
    except json.JSONDecodeError:
        print(f"non-JSON list response:\nHTTP {resp.status_code} {resp.reason}\n{resp.text}", file=sys.stderr)
        return 1
    if args.json:
        print_json(body)
        return 0
    items = body.get("items", [])
    print(
        f"{body.get('total', len(items))} paper(s) "
        f"(page {body.get('page', 1)}, {body.get('per_page', len(items))} per page)"
    )
    for it in items:
        title = (it.get("title") or "").replace("\n", " ")
        if len(title) > 64:
            title = title[:64] + "…"
        md = "md:yes" if it.get("has_md") else "md:no "
        print(f"  {it.get('paper_id')} {md} {it.get('status', '-'):<7} {title}")
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    """Batch reference resolution — GET /api/papers/lookup?ids=…."""
    refs = [r.strip() for r in args.refs if r and r.strip()]
    if not refs:
        print("no references given — pass kind:id refs (arxiv:… / doi:… / openalex:…)", file=sys.stderr)
        return 2
    if len(refs) > 200:
        print(f"{len(refs)} refs exceeds the server limit of 200 per call", file=sys.stderr)
        return 2
    resp = requests.get(
        f"{base_url_from_args(args)}/api/papers/lookup",
        params={"ids": ",".join(refs)},
        headers={**auth_headers(args), **client_version_headers()},
        verify=request_verify(args),
        timeout=args.request_timeout,
    )
    check_response_version(resp, write=False)
    if not resp.ok:
        print(_render_server_error("lookup", resp), file=sys.stderr)
        return 1
    try:
        body = resp.json()
    except json.JSONDecodeError:
        print(f"non-JSON lookup response:\nHTTP {resp.status_code} {resp.reason}\n{resp.text}", file=sys.stderr)
        return 1
    if args.json:
        print_json(body)
        return 0
    if not body.get("corpus_available", True):
        print(
            "note: OpenAlex corpus unavailable on the server — resolution may be limited",
            file=sys.stderr,
        )
    for r in body.get("results", []):
        if not r.get("resolved"):
            print(f"  {r.get('ref')}: unresolved")
            continue
        flags = ["hosted" if r.get("hosted") else "not hosted"]
        if r.get("has_md"):
            flags.append("markdown ready")
        year = f" ({r['year']})" if r.get("year") else ""
        print(f"  {r.get('ref')}: {', '.join(flags)}{year} — {r.get('title') or '(no title)'}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    raw_ids = args.id_or_doi
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    ids = [i.strip().lstrip("/") for i in raw_ids if i and i.strip()]
    kind = args.kind

    if len(ids) > 1:
        # Batch: one round-trip via the server's status/batch endpoint.
        if kind != "markdown":
            print("--kind is only valid with a single id (the batch endpoint reports all kinds at once)", file=sys.stderr)
            return 2
        resp = requests.get(
            f"{base_url}/api/papers/status/batch",
            params={"ids": ",".join(ids)},
            headers={**auth_headers(args), **client_version_headers()},
            verify=request_verify(args),
            timeout=args.request_timeout,
        )
        check_response_version(resp, write=False)
        _print_notes(resp, quiet=args.quiet_notes)
        if not resp.ok:
            print(_render_server_error("status batch", resp), file=sys.stderr)
            return 1
        try:
            print_json(resp.json())
        except json.JSONDecodeError:
            print(f"non-JSON status response:\nHTTP {resp.status_code} {resp.reason}\n{resp.text}", file=sys.stderr)
            return 1
        return 0

    id_or_doi = ids[0]
    url = f"{base_url}/api/papers/{id_or_doi}/{kind}/status"
    resp = requests.get(
        url,
        headers={**auth_headers(args), **client_version_headers()},
        verify=request_verify(args),
        timeout=args.request_timeout,
    )
    check_response_version(resp, write=False)
    _print_notes(resp, quiet=args.quiet_notes)
    if resp.status_code == 404:
        print(_render_server_error("status", resp), file=sys.stderr)
        return 1
    if not resp.ok:
        print(_render_server_error("status", resp), file=sys.stderr)
        return 1
    try:
        body = resp.json()
    except json.JSONDecodeError:
        print(f"non-JSON status response:\nHTTP {resp.status_code} {resp.reason}\n{resp.text}", file=sys.stderr)
        return 1
    print_json(body)
    return 0


def cmd_mineru_lease(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    arxiv_id = args.id_or_doi.strip().lstrip("/")
    params: dict[str, Any] = {}
    if args.ttl_seconds is not None:
        params["ttl_seconds"] = args.ttl_seconds
    headers = {**auth_headers(args), **client_version_headers()}
    verify = request_verify(args)
    check_server_before_write(
        base_url, headers=headers, timeout=args.request_timeout, verify=verify
    )
    resp = write_request(
        requests.post,
        f"{base_url}/api/v1/papers/{arxiv_id}/mineru-lease",
        params=params or None,
        headers=headers,
        verify=verify,
        timeout=args.request_timeout,
    )
    check_response_version(resp, write=True, request_sent=True)
    if resp.status_code != 201:
        print(_render_server_error("mineru lease", resp), file=sys.stderr)
        return 1
    try:
        print_json(resp.json())
    except json.JSONDecodeError:
        print(resp.text)
    return 0


def cmd_release_mineru_lease(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    arxiv_id = args.id_or_doi.strip().lstrip("/")
    claim_id = args.claim_id.strip()
    headers = {**auth_headers(args), **client_version_headers()}
    verify = request_verify(args)
    check_server_before_write(
        base_url, headers=headers, timeout=args.request_timeout, verify=verify
    )
    resp = write_request(
        requests.delete,
        f"{base_url}/api/v1/papers/{arxiv_id}/mineru-lease/{claim_id}",
        headers=headers,
        verify=verify,
        timeout=args.request_timeout,
    )
    check_response_version(resp, write=True, request_sent=True)
    if resp.status_code != 204:
        print(_render_server_error("mineru lease release", resp), file=sys.stderr)
        return 1
    return 0


def _add_id_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "id_or_doi",
        help=(
            "canonical qa_ paper id OR arxiv id (versioned or bare; new-style or old-style) OR a DOI. "
            "Server auto-fills missing version (latest) and missing category (quant-ph). "
            "Examples: 0811.3171v3, 0811.3171, quant-ph/9508027v2, 9508027, "
            "10.1103/PhysRevLett.103.150502"
        ),
    )


def _add_wait_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="On cache miss return the initial 202 JSON without polling (for scripted async workflows).",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=_DEFAULT_MAX_WAIT_S,
        help=f"Maximum seconds to wait for an in-flight conversion. Default: {int(_DEFAULT_MAX_WAIT_S)}s.",
    )
    parser.add_argument(
        "--quiet-progress",
        action="store_true",
        help="Suppress the per-poll '...waiting: state ...' progress lines on stderr.",
    )


def _add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output", "-o", default=None,
        help='Write bytes to FILE. Use "-" or omit for stdout.',
    )
    _add_wait_args(parser)
    parser.add_argument(
        "--quiet-notes",
        action="store_true",
        help="Suppress the 'Note (server applied defaults): ...' line on stderr.",
    )


def build_get_markdown_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper get markdown",
        description="Fetch a paper's MinerU markdown bytes from the server (triggers silent fetch+convert when missing).",
    )
    _add_id_arg(p)
    _add_output_args(p)
    add_common_http_args(p)
    p.set_defaults(func=cmd_get_markdown)
    return p


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _read_limit(value: str) -> int:
    number = _positive_int(value)
    if number > 100_000:
        raise argparse.ArgumentTypeError("limit must be 1..100000 Unicode characters")
    return number


def build_read_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper read",
        description=(
            "Read an agent-friendly, resumable JSON view derived from MinerU Middle. "
            "Returns source/revision pins, content ranges, truncated and next_request. "
            "This is NOT the original artifact; use parse-json for immutable raw bytes. "
            "Missing parses are prepared asynchronously by the server."
        ),
    )
    _add_id_arg(p)
    p.add_argument("--source-id", "--source", dest="source_id", help="pin an exact PDF source")
    p.add_argument("--revision", help="pin an immutable parse revision")
    p.add_argument("--page", type=_positive_int, help="select a 1-based page")
    p.add_argument("--block", type=_positive_int, help="select a 1-based block within --page")
    p.add_argument("--cursor", help="resume using next_request.cursor; preserves the server-pinned selection")
    p.add_argument("--limit", type=_read_limit, help="content budget: 1..100000 Unicode characters (server default 30000)")
    p.add_argument("--json", action="store_true", help="JSON is always emitted; accepted for scripting consistency")
    _add_output_args(p)
    add_common_http_args(p)
    p.set_defaults(func=cmd_read)
    return p


def build_get_images_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper get images",
        description=(
            "Download a paper's images zip (a byproduct of the MinerU conversion). "
            "No long-running operation: the server answers 404 until the conversion has run — "
            "use `qatlas paper get markdown` first to trigger it."
        ),
    )
    _add_id_arg(p)
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help='Write the zip to FILE. Use "-" or omit for stdout.',
    )
    p.add_argument(
        "--quiet-notes",
        action="store_true",
        help="Suppress the 'Note (server applied defaults): ...' line on stderr.",
    )
    _add_wait_args(p)
    add_common_http_args(p)
    p.set_defaults(func=cmd_get_images)
    return p


def build_get_metadata_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper get metadata",
        description=(
            "Fetch a paper's registry metadata (paper_id, arxiv/doi/openalex ids, title, "
            "authors, assets, acquisition) as JSON. Accepts a qa_ paper id, arxiv id "
            "(versioned or bare) or DOI. No abstract — the registry does not store one."
        ),
    )
    _add_id_arg(p)
    p.add_argument(
        "--quiet-notes",
        action="store_true",
        help="Suppress the 'Note (server applied defaults): ...' line on stderr.",
    )
    add_common_http_args(p)
    p.set_defaults(func=cmd_get_metadata)
    return p


def build_list_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper list",
        description="Paginated listing of registry papers with optional filters.",
    )
    p.add_argument(
        "--has-md",
        dest="has_md",
        type=lambda v: v.lower() in {"1", "true", "yes"},
        default=None,
        help="filter on converted markdown present (true/false)",
    )
    p.add_argument(
        "--status",
        choices=["pending", "ready", "failed"],
        default=None,
        help="filter by lifecycle status",
    )
    p.add_argument("-q", help="case-insensitive title substring filter")
    p.add_argument("--arxiv-id", help="exact identity filter (version suffix tolerated)")
    p.add_argument("--doi", help="exact identity filter (URL prefix tolerated)")
    p.add_argument("--paper-id", help="exact identity filter (qa_ id)")
    p.add_argument("--page", type=int, default=None, help="1-based page number")
    p.add_argument("--per-page", type=int, default=None, help="page size (server caps it)")
    p.add_argument(
        "--sort",
        choices=["created_at", "updated_at"],
        default=None,
        help="sort column (descending)",
    )
    p.add_argument("--json", action="store_true", help="print the raw server response as JSON")
    add_common_http_args(p)
    p.set_defaults(func=cmd_list)
    return p


def build_lookup_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper lookup",
        description=(
            "Resolve up to 200 namespaced references (arxiv:… / doi:… / openalex:…) "
            "against the registry + OpenAlex corpus; reports hosted / markdown state "
            "for each."
        ),
    )
    p.add_argument("refs", nargs="+", help="kind:id references (comma-free; one per argument)")
    p.add_argument("--json", action="store_true", help="print the raw server response as JSON")
    add_common_http_args(p)
    p.set_defaults(func=cmd_lookup)
    return p


def build_status_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper status",
        description=(
            "Query the side-effect-free status endpoint for one paper, or the batch "
            "endpoint (md/pdf readiness, image count, phase) for several at once."
        ),
    )
    p.add_argument(
        "id_or_doi",
        nargs="+",
        help=(
            "arxiv id (versioned or bare) OR DOI OR qa_ paper id. One id queries the "
            "per-paper status endpoint; two or more ids switch to the batch endpoint."
        ),
    )
    p.add_argument(
        "--kind",
        choices=["markdown"],
        default="markdown",
        help='Which status endpoint to hit. Only "markdown" remains in this legacy status API; use paper pdf for immutable source PDFs.',
    )
    p.add_argument(
        "--quiet-notes",
        action="store_true",
        help="Suppress the 'Note (server applied defaults): ...' line on stderr.",
    )
    add_common_http_args(p)
    p.set_defaults(func=cmd_status)
    return p


def build_get_figures_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper get figures",
        description=(
            "Fetch a paper's figure/caption index as JSON: figure groups with "
            "captions (when the markdown has them), per-image sizes and single-image "
            "download URLs. Requires the MinerU markdown to exist."
        ),
    )
    _add_id_arg(p)
    p.add_argument(
        "--quiet-notes",
        action="store_true",
        help="Suppress the 'Note (server applied defaults): ...' line on stderr.",
    )
    _add_wait_args(p)
    add_common_http_args(p)
    p.set_defaults(func=cmd_get_figures)
    return p


def build_get_image_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper get image",
        description=(
            "Download a single image file from a paper's images bundle. NAME is the "
            "sha256-hex filename from `qatlas paper get figures`."
        ),
    )
    _add_id_arg(p)
    p.add_argument(
        "name",
        help="image file name, e.g. 3afe9563bed1...e965e5.jpg (see `paper get figures`)",
    )
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help='Write the image to FILE. Use "-" or omit for stdout.',
    )
    p.add_argument(
        "--quiet-notes",
        action="store_true",
        help="Suppress the 'Note (server applied defaults): ...' line on stderr.",
    )
    _add_wait_args(p)
    add_common_http_args(p)
    p.set_defaults(func=cmd_get_image)
    return p


def build_mineru_lease_parser(*, prog: str = "qatlas paper mineru-lease") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog,
        description="Acquire a MinerU processing lease for a paper.",
    )
    _add_id_arg(p)
    p.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help="Lease TTL in seconds. Server default is 1800; server clamps to its allowed range.",
    )
    add_common_http_args(p)
    p.set_defaults(func=cmd_mineru_lease)
    return p


def build_release_mineru_lease_parser(*, prog: str = "qatlas paper mineru-lease release") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog,
        description="Release a MinerU processing lease by claim_id.",
    )
    _add_id_arg(p)
    p.add_argument("claim_id", help="claim_id returned by mineru-lease")
    add_common_http_args(p)
    p.set_defaults(func=cmd_release_mineru_lease)
    return p


def _print_top_help() -> None:
    print(
        """qatlas paper — fetch paper assets from the server

Usage:
  qatlas paper get markdown ID_OR_DOI [--output FILE] [--no-wait]
  qatlas paper get images   ID_OR_DOI [--output FILE]
  qatlas paper get figures  ID_OR_DOI
  qatlas paper get image    ID_OR_DOI NAME [-o FILE]
  qatlas paper get metadata ID_OR_DOI
  qatlas paper status       ID_OR_DOI [ID_OR_DOI ...] [--kind markdown]
  qatlas paper list         [--has-md true] [--status …] [-q …] [--json]
  qatlas paper lookup       arxiv:ID | doi:DOI | openalex:ID ... [--json]
  qatlas paper fetch        ID|DOI|URL... [--file FILE] [--json]
  qatlas paper source-register URL --title TITLE --author NAME [--author NAME ...] --year YYYY [--json]
  qatlas paper jobs         [--remote] [--watch] [--json]
  qatlas paper mineru-lease ID_OR_DOI [--ttl-seconds N]
  qatlas paper mineru-lease release ID_OR_DOI CLAIM_ID

  Source PDFs and resumable reading (paper_access.enabled required):
  qatlas paper pdf         ID [--source-id S|--version vN] [-o FILE]
  qatlas paper read        ID [--source-id S] [--revision R] [--page N] [--block N]
                             [--cursor C] [--limit N] [-o FILE] [--no-wait]
  Immutable parse artifacts and block-level comments (server Q1/Q2):
  qatlas paper source-list ID [--json]
  qatlas paper parse-list  ID [--json]
  qatlas paper parse-json  ID REVISION [-o FILE]
  qatlas paper block-list  ID REVISION [--page-idx N] [--cursor C] [--json]
  qatlas paper block-get   ID REVISION PAGE_IDX BLOCK_INDEX [--json]
  qatlas paper block-image ID REVISION PAGE_IDX BLOCK_INDEX [-o FILE]
  (see `qatlas paper pdf --help` etc.; discussions live in `qatlas comments`)

ID forms accepted:
  - Versioned arxiv id          0811.3171v3 / quant-ph/9508027v2
  - Bare arxiv id (no version)  0811.3171  (server adds latest vN)
  - Bare old-style (no category) 9508027   (server adds quant-ph/)
  - DOI                          10.1103/PhysRevLett.103.150502
  - Canonical qa_ paper id       qa_… (immutable work identity; PDF aliases
                                 also cache under the resolved qa_ identity)

Markdown/images deliver via ``paper get`` above; ``paper pdf`` directly
streams authenticated source-pinned PDF bytes (sha256-verified before output,
content-addressed cache under cache_dir). ``paper read`` emits derived JSON,
including locators and a pinned next_request.cursor; it is not raw parse-json.

PDF/read and legacy delivery endpoints must be enabled via
``paper_access.enabled: true`` in the server's config.yaml. Defaults applied by the server
are surfaced on stderr (use --quiet-notes to suppress).

Use 'qatlas paper <subcommand> --help' for full options.
"""
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        _print_top_help()
        return 0
    subcommand = argv.pop(0)
    if subcommand == "get":
        if not argv or argv[0] in {"-h", "--help"}:
            _print_top_help()
            return 0
        kind = argv.pop(0)
        if kind == "markdown":
            parser = build_get_markdown_parser()
        elif kind == "images":
            parser = build_get_images_parser()
        elif kind == "figures":
            parser = build_get_figures_parser()
        elif kind == "image":
            parser = build_get_image_parser()
        elif kind == "metadata":
            parser = build_get_metadata_parser()
        else:
            print(f"unknown 'paper get' subcommand: {kind!r}", file=sys.stderr)
            _print_top_help()
            return 2
    elif subcommand == "read":
        parser = build_read_parser()
    elif subcommand == "status":
        parser = build_status_parser()
    elif subcommand == "list":
        parser = build_list_parser()
    elif subcommand == "lookup":
        parser = build_lookup_parser()
    elif subcommand == "fetch":
        parser = build_fetch_parser()
    elif subcommand == "jobs":
        parser = build_jobs_parser()
    elif subcommand == "source-register":
        from qatlas.client import external_sources

        return external_sources.main(argv)
    elif subcommand in {"mineru-lease", "claim"}:
        prog_base = "qatlas paper mineru-lease" if subcommand == "mineru-lease" else "qatlas paper claim"
        if argv and argv[0] == "release":
            argv.pop(0)
            parser = build_release_mineru_lease_parser(prog=f"{prog_base} release")
        else:
            parser = build_mineru_lease_parser(prog=prog_base)
    elif subcommand in {
        "pdf",
        "source-list",
        "parse-list",
        "parse-json",
        "block-list",
        "block-get",
        "block-image",
    }:
        # Block-comments originals (plan §12.4): implemented in a sibling
        # module to keep this one focused on the legacy paper-access API.
        from qatlas.client import blocks as _blocks

        return _blocks.main([subcommand, *argv])
    else:
        print(f"unknown paper subcommand: {subcommand!r}", file=sys.stderr)
        _print_top_help()
        return 2
    args = parser.parse_args(argv)
    return run_with_request_errors(args.func, args)


if __name__ == "__main__":
    raise SystemExit(main())
