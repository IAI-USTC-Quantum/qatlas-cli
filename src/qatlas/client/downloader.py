"""Robust Downloader CLI: batch fetch submissions and job progress.

Backs ``qatlas paper fetch`` and ``qatlas paper jobs`` (mounted by the
``qatlas.client.paper`` dispatcher). Server endpoints:

* ``POST /api/downloader/fetch`` — submit up to 50 identifiers (DOI /
  arXiv id / paper URL) into the multi-strategy download ladder
  (``papers:write``);
* ``GET /api/downloader/jobs`` — in-process local job snapshot with
  counters (``papers:read``);
* ``GET /api/downloader/remote-jobs`` — PostgreSQL-persisted outbound
  fleet snapshot, at most the 500 most recently updated tasks
  (``papers:read``).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import requests

from qatlas.client._common import (
    add_common_http_args,
    auth_headers,
    base_url_from_args,
    check_response_version,
    client_version_headers,
    print_json,
    request_verify,
    run_with_request_errors,
)

#: Server-side cap on identifiers per fetch call.
MAX_FETCH_ITEMS = 50

#: Remote-fleet states that still owe work; everything else is terminal.
_REMOTE_ACTIVE_STATES = frozenset({"queued", "running", "staged"})


def _render_error(kind: str, resp: requests.Response) -> str:
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("message") or "")
    except ValueError:
        detail = (resp.text or "").strip()
    if len(detail) > 300:
        detail = detail[:300] + "…"
    return f"{kind} failed: HTTP {resp.status_code}" + (f": {detail}" if detail else "")


def _collect_items(args: argparse.Namespace) -> list[str]:
    """Merge positional identifiers with ``--file`` lines, de-duplicated."""
    items: list[str] = [i.strip() for i in args.items or []]
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            items.extend(line.strip() for line in fh)
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and not item.startswith("#") and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def cmd_fetch(args: argparse.Namespace) -> int:
    items = _collect_items(args)
    if not items:
        print("no items to fetch — pass identifiers or --file FILE", file=sys.stderr)
        return 2
    if len(items) > MAX_FETCH_ITEMS:
        print(
            f"{len(items)} items exceeds the server limit of {MAX_FETCH_ITEMS} per call",
            file=sys.stderr,
        )
        return 2
    resp = requests.post(
        f"{base_url_from_args(args)}/api/downloader/fetch",
        json={"items": items},
        headers={**auth_headers(args), **client_version_headers()},
        verify=request_verify(args),
        timeout=args.request_timeout,
    )
    check_response_version(resp, write=True)
    if not resp.ok:
        print(_render_error("fetch", resp), file=sys.stderr)
        return 1
    try:
        body = resp.json()
    except json.JSONDecodeError:
        print(f"non-JSON fetch response:\nHTTP {resp.status_code}\n{resp.text}", file=sys.stderr)
        return 1
    if args.json:
        print_json(body)
    else:
        for item in body.get("items", []):
            if item.get("error"):
                print(f"  failed   {item.get('input')}: {item['error']}")
            else:
                suffix = " (new)" if item.get("created") else ""
                print(
                    f"  enqueued {item.get('input')} -> {item.get('paper_id')} "
                    f"[{item.get('kind')}]{suffix}"
                )
        print(f"enqueued {body.get('enqueued', 0)}/{len(items)}")
    return 0 if body.get("enqueued") else 1


def _snapshot(args: argparse.Namespace, remote: bool) -> requests.Response:
    path = "/api/downloader/remote-jobs" if remote else "/api/downloader/jobs"
    return requests.get(
        f"{base_url_from_args(args)}{path}",
        headers={**auth_headers(args), **client_version_headers()},
        verify=request_verify(args),
        timeout=args.request_timeout,
    )


def _active_count(body: dict[str, Any], remote: bool) -> int:
    if remote:
        return sum(1 for j in body.get("jobs", []) if j.get("state") in _REMOTE_ACTIVE_STATES)
    counters = body.get("counters") or {}
    return int(counters.get("queued", 0)) + int(counters.get("in_flight", 0))


def _print_snapshot(body: dict[str, Any], remote: bool) -> None:
    if remote:
        print(f"fleet enabled: {body.get('enabled')}")
        for job in body.get("jobs", []):
            error = f" error={job['error']}" if job.get("error") else ""
            print(
                f"  {job.get('id')} worker={job.get('worker_id')} "
                f"state={job.get('state')} id={job.get('identifier')}{error}"
            )
        return
    counters = body.get("counters") or {}
    print("counters: " + " ".join(f"{k}={v}" for k, v in sorted(counters.items())))
    for job in body.get("jobs", []):
        error = f" error={job['error']}" if job.get("error") else ""
        print(
            f"  {job.get('paper_id')} state={job.get('state')} "
            f"phase={job.get('phase') or '-'} strategy={job.get('strategy') or '-'}{error}"
        )


def cmd_jobs(args: argparse.Namespace) -> int:
    remote = bool(args.remote)
    while True:
        resp = _snapshot(args, remote)
        check_response_version(resp, write=False)
        if not resp.ok:
            print(_render_error("jobs", resp), file=sys.stderr)
            return 1
        try:
            body = resp.json()
        except json.JSONDecodeError:
            print(f"non-JSON jobs response:\nHTTP {resp.status_code}\n{resp.text}", file=sys.stderr)
            return 1
        if args.json:
            if args.watch:
                # JSON-lines stream: one compact object per poll.
                print(json.dumps(body, ensure_ascii=False))
            else:
                print_json(body)
        else:
            _print_snapshot(body, remote)
        if not args.watch or _active_count(body, remote) == 0:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 130


def build_fetch_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper fetch",
        description=(
            "Submit a batch of paper identifiers (DOI / arXiv id / paper URL) to the "
            f"server's Robust Downloader. At most {MAX_FETCH_ITEMS} identifiers per call. "
            "Enqueueing does not mean the PDF is archived yet — track with `qatlas paper jobs`."
        ),
    )
    p.add_argument(
        "items",
        nargs="*",
        help="paper identifiers (DOI, arXiv id, or paper page URL)",
    )
    p.add_argument(
        "--file",
        help="read identifiers from FILE, one per line ('#' comments and blank lines ignored)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="print the raw server response as JSON",
    )
    add_common_http_args(p)
    p.set_defaults(func=cmd_fetch)
    return p


def build_jobs_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper jobs",
        description=(
            "Show downloader job progress: the local in-process snapshot by default, "
            "or the persisted outbound-fleet snapshot with --remote. --watch polls "
            "until no active jobs remain (Ctrl-C stops early, exit code 130)."
        ),
    )
    p.add_argument(
        "--remote",
        action="store_true",
        help="query the persisted remote-fleet snapshot (/api/downloader/remote-jobs) instead of local jobs",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="keep polling until the queue is idle",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="poll interval in seconds for --watch (default: 2)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="machine-readable output (one JSON object per poll in --watch mode)",
    )
    add_common_http_args(p)
    p.set_defaults(func=cmd_jobs)
    return p


def main(argv: list[str] | None = None) -> int:
    """Standalone entry for ``python -m qatlas.client.downloader`` (rare)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "fetch":
        parser = build_fetch_parser()
    elif argv[0] == "jobs":
        parser = build_jobs_parser()
        argv = argv[1:]
    else:
        print(f"unknown downloader subcommand: {argv[0]!r}", file=sys.stderr)
        return 2
    args = parser.parse_args(argv)
    return run_with_request_errors(args.func, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
