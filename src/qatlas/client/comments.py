"""``qatlas comments`` — block-level discussion reads and writes
(plan §12.2 Q2 endpoints, §12.4 command surface).

Commands::

    qatlas comments list   PAPER [--scope …] [--type …] [--status …]
                                  [--page-idx N] [--block-index N]
                                  [--cursor C] [--per-page N] [--json]
    qatlas comments show   DISCUSSION_ID [--cursor C] [--per-page N] [--json]
    qatlas comments create PAPER REVISION PAGE_IDX BLOCK_INDEX
                                  [BODY | --body-file FILE] [--type T]
                                  [--scope public|lean] [--status S]
                                  [--model STR] [--json]
    qatlas comments reply  DISCUSSION_ID [BODY | --body-file FILE]
                                  [--model STR] [--json]
    qatlas comments status DISCUSSION_ID pending|confirmed|retracted
                                  --reason TEXT [--json]
    qatlas comments edit   discussion DISCUSSION_ID [BODY | --body-file FILE]
                                  [--if-match N] [--json]
    qatlas comments edit   reply DISCUSSION_ID REPLY_ID [BODY | --body-file FILE]
                                  [--if-match N] [--json]

Contract highlights (plan §12.2 / §9):

* Writes are idempotent: this CLI auto-sends ``Idempotency-Key`` =
  SHA-256(method|path|body), so a timeout can be retried with the same
  body and the server replays the original result instead of
  double-posting.
* ``edit`` sends ``If-Match: <revision>``; a stale revision is a 409
  (exit 6). Omit ``--if-match`` to auto-fetch the current revision
  first.
* Body budget: 20,000 Unicode characters (mirrored client-side); the
  server rejects more — split long posts instead of truncating.
* Types are bounded string labels: presets ``normal`` /
  ``transcription_error`` / ``typo_in_original`` plus custom slugs
  matching ``[a-z0-9_:-]+`` (≤64 chars). A discussion's status
  (``pending``/``confirmed``/``retracted``, optional) is orthogonal to
  its type (plan §12.1).
* System PATs are read-only for comments — writes answer 403 (exit 5).

Output: ``--json`` prints the complete machine response; the default
is a human-readable rendering. stdout is data only; notes/hints go to
stderr. Comments are mutable → never cached on disk.

Exit codes: 0 ok, 1 transport/5xx/bad content, 2 usage, 3 not found,
4 unauthorized, 5 forbidden, 6 conflict (409/CAS), 7 too large, 8 rate
limited, 9 unsupported server. See ``qatlas.client.blockapi``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from qatlas.client import blockapi
from qatlas.client._common import add_common_http_args, base_url_from_args

PRESET_TYPES = ("normal", "transcription_error", "typo_in_original")
STATUSES = ("pending", "confirmed", "retracted")
SCOPES = ("public", "lean")
_TYPE_RE = re.compile(r"^[a-z0-9_:-]{1,64}$")


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _json_print(body: dict[str, Any]) -> None:
    print(json.dumps(body, ensure_ascii=False, indent=2))


def _excerpt(text: Any, limit: int = 72) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_type(value: str) -> str:
    if not _TYPE_RE.match(value):
        raise blockapi.ApiError(
            f"invalid --type {value!r}: presets are {', '.join(PRESET_TYPES)}; "
            "custom types are slugs matching [a-z0-9_:-]+ (max 64 chars)",
            kind="usage",
            exit_code=blockapi.EXIT_USAGE,
        )
    return value


def _read_body(args: argparse.Namespace) -> str:
    """Resolve the body from the positional or --body-file (exactly one)."""
    positional = getattr(args, "body", None)
    body_file = getattr(args, "body_file", None)
    if positional is None and not body_file:
        raise blockapi.ApiError(
            "no body given — pass it as the positional argument or with "
            "--body-file FILE ('-' reads stdin)",
            kind="usage",
            exit_code=blockapi.EXIT_USAGE,
        )
    if positional is not None and body_file:
        raise blockapi.ApiError(
            "pass the body either as the positional argument or --body-file, "
            "not both",
            kind="usage",
            exit_code=blockapi.EXIT_USAGE,
        )
    if body_file:
        if body_file == "-":
            body = sys.stdin.read()
        else:
            with open(body_file, encoding="utf-8") as fh:
                body = fh.read()
    else:
        body = positional
    blockapi.check_body_text(body)
    return body


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    params: dict[str, Any] = {
        "per_page": blockapi.clamp_per_page(args.per_page),
    }
    if args.scope:
        params["scope"] = args.scope
    if args.type:
        params["type"] = _validate_type(args.type)
    if args.status:
        params["status"] = args.status
    if args.page_idx is not None:
        params["page_idx"] = args.page_idx
    if args.block_index is not None:
        params["block_index"] = args.block_index
    if args.cursor:
        params["cursor"] = args.cursor
    path = (
        f"/api/papers/{blockapi.encode_path_segment(args.paper_id)}/discussions"
    )
    body = blockapi.get_json(
        args, path, what="discussions list", params=params, base_url=base_url
    )
    if args.json:
        _json_print(body)
        return 0
    items = [it for it in (body.get("items") or body.get("discussions") or []) if isinstance(it, dict)]
    print(f"{len(items)} discussion(s):")
    for it in items:
        status = it.get("status") or "—"
        print(
            f"  [{status:<9}] {it.get('type', '?')}/{it.get('scope', '?')} "
            f"{it.get('discussion_id', '?')} by {it.get('created_by', '?')}: "
            f"{_excerpt(it.get('body'))}"
        )
    cursor = body.get("next_cursor")
    if cursor:
        print(f"next_cursor: {cursor}")
        _stderr("more results exist — pass --cursor")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    params: dict[str, Any] = {
        "per_page": blockapi.clamp_per_page(args.per_page),
    }
    if args.cursor:
        params["cursor"] = args.cursor
    path = f"/api/discussions/{blockapi.encode_path_segment(args.discussion_id)}"
    body = blockapi.get_json(
        args, path, what="discussion detail", params=params, base_url=base_url
    )
    if args.json:
        _json_print(body)
        return 0
    _render_discussion(body)
    return 0


def _render_discussion(body: dict[str, Any]) -> None:
    print(f"discussion {body.get('discussion_id', '?')}  "
          f"[{body.get('status') or '—'}]  {body.get('type', '?')}/"
          f"{body.get('scope', '?')}")
    print(
        f"  paper {body.get('paper_id', '?')}  revision "
        f"{body.get('parse_revision', '?')}  page_idx={body.get('page_idx', '?')} "
        f"block_index={body.get('block_index', '?')}"
    )
    model = f"  model={body['model']}" if body.get("model") else ""
    print(
        f"  by {body.get('created_by', '?')} at {body.get('created_at', '?')}"
        f"{model}  revision={body.get('revision', '?')}  "
        f"updated={body.get('updated_at', '?')}"
    )
    print("  body:")
    for line in str(body.get("body", "")).splitlines() or [""]:
        print(f"    {line}")
    replies = [r for r in (body.get("replies") or []) if isinstance(r, dict)]
    print(f"  replies: {len(replies)}")
    for r in replies:
        model_r = f" model={r['model']}" if r.get("model") else ""
        print(
            f"    {r.get('reply_id', '?')} by {r.get('created_by', '?')}{model_r} "
            f"rev={r.get('revision', '?')} at {r.get('created_at', '?')}: "
            f"{_excerpt(r.get('body'), 96)}"
        )
        if r.get("edited_at") or r.get("updated_at") != r.get("created_at"):
            print(f"      (edited: {r.get('edited_at') or r.get('updated_at')})")
    cursor = body.get("next_cursor")
    if cursor:
        print(f"  next_cursor: {cursor} (more replies — pass --cursor)")


def _build_create_payload(args: argparse.Namespace) -> dict[str, Any]:
    body = _read_body(args)
    payload: dict[str, Any] = {
        "parse_revision": args.revision,
        "page_idx": args.page_idx,
        "block_index": args.block_index,
        "body": body,
        "type": _validate_type(args.type),
        "scope": args.scope,
    }
    if args.status:
        payload["status"] = args.status
        # The server contract requires a reason whenever an initial
        # status is set (status events are append-only history).
        reason = getattr(args, "reason", None)
        if not reason:
            reason = "created with initial status via qatlas-cli"
        payload["reason"] = reason
    if args.model:
        payload["model"] = args.model
    return payload


def cmd_create(args: argparse.Namespace) -> int:
    """Create a root discussion anchored to (revision, page_idx, block_index)."""
    base_url = base_url_from_args(args)
    payload = _build_create_payload(args)
    path = (
        f"/api/papers/{blockapi.encode_path_segment(args.paper_id)}/discussions"
    )
    created = blockapi.post_json(
        args, path, what="create discussion", json_body=payload, base_url=base_url
    )
    if args.json:
        _json_print(created)
    else:
        _render_discussion(created)
    _stderr(
        "created (Idempotency-Key was auto-derived from this exact request; "
        "re-running the identical command replays this result, it does not "
        "double-post)"
    )
    return 0


def _render_reply(body: dict[str, Any]) -> None:
    model = f"  model={body['model']}" if body.get("model") else ""
    print(
        f"reply {body.get('reply_id', '?')} by {body.get('created_by', '?')}{model} "
        f"rev={body.get('revision', '?')} recorded"
    )
    print(f"  {_excerpt(body.get('body'), 120)}")


def cmd_reply(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    payload: dict[str, Any] = {"body": _read_body(args)}
    if args.model:
        payload["model"] = args.model
    path = (
        f"/api/discussions/{blockapi.encode_path_segment(args.discussion_id)}"
        "/replies"
    )
    created = blockapi.post_json(
        args, path, what="reply", json_body=payload, base_url=base_url
    )
    if args.json:
        _json_print(created)
    else:
        _render_reply(created)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Change a discussion's status (root author or admin only)."""
    base_url = base_url_from_args(args)
    reason = (args.reason or "").strip()
    if not reason:
        raise blockapi.ApiError(
            "--reason is required for every status change (kept in history)",
            kind="usage",
            exit_code=blockapi.EXIT_USAGE,
        )
    payload: dict[str, Any] = {"status": args.new_status, "reason": reason}
    path = (
        f"/api/discussions/{blockapi.encode_path_segment(args.discussion_id)}"
        "/status"
    )
    updated = blockapi.patch_json(
        args, path, what="status change", json_body=payload, if_match=None,
        base_url=base_url,
    )
    if args.json:
        _json_print(updated)
    else:
        _render_discussion(updated)
    return 0


def _current_discussion_revision(
    args: argparse.Namespace, base_url: str, discussion_id: str
) -> str:
    path = f"/api/discussions/{blockapi.encode_path_segment(discussion_id)}"
    body = blockapi.get_json(
        args, path, what="fetch current revision", base_url=base_url
    )
    rev = body.get("revision") or body.get("etag")
    if rev is None:
        raise blockapi.ApiError(
            "server did not report the discussion's current revision — "
            "pass --if-match explicitly",
            kind="bad_content",
            exit_code=blockapi.EXIT_TRANSPORT,
            body=body,
        )
    return str(rev)


def _current_reply_revision(
    args: argparse.Namespace, base_url: str, discussion_id: str, reply_id: str
) -> str:
    path = f"/api/discussions/{blockapi.encode_path_segment(discussion_id)}"
    body = blockapi.get_json(
        args, path, what="fetch current revision",
        params={"per_page": blockapi.PER_PAGE_MAX}, base_url=base_url,
    )
    for r in body.get("replies") or []:
        if isinstance(r, dict) and str(r.get("reply_id")) == reply_id:
            rev = r.get("revision") or r.get("etag")
            if rev is not None:
                return str(rev)
    raise blockapi.ApiError(
        f"reply {reply_id!r} not found in the first {blockapi.PER_PAGE_MAX} "
        "replies — pass --if-match with its current revision",
        kind="not_found",
        exit_code=blockapi.EXIT_NOT_FOUND,
    )


def cmd_edit_discussion(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    payload = {"body": _read_body(args)}
    if_match = args.if_match
    if if_match is None:
        if_match = _current_discussion_revision(args, base_url, args.discussion_id)
        _stderr(f"auto-fetched current revision {if_match} (pass --if-match to pin)")
    path = (
        f"/api/discussions/{blockapi.encode_path_segment(args.discussion_id)}"
        "/body"
    )
    updated = blockapi.patch_json(
        args, path, what="edit discussion body", json_body=payload,
        if_match=if_match, base_url=base_url,
    )
    if args.json:
        _json_print(updated)
    else:
        _render_discussion(updated)
    return 0


def cmd_edit_reply(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    payload = {"body": _read_body(args)}
    if_match = args.if_match
    if if_match is None:
        if_match = _current_reply_revision(
            args, base_url, args.discussion_id, args.reply_id
        )
        _stderr(f"auto-fetched current revision {if_match} (pass --if-match to pin)")
    path = (
        f"/api/discussions/{blockapi.encode_path_segment(args.discussion_id)}"
        f"/replies/{blockapi.encode_path_segment(args.reply_id)}/body"
    )
    updated = blockapi.patch_json(
        args, path, what="edit reply body", json_body=payload,
        if_match=if_match, base_url=base_url,
    )
    if args.json:
        _json_print(updated)
    else:
        _render_reply(updated)
    return 0


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _body_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "body",
        nargs="?",
        default=None,
        help="discussion/reply body text (≤20,000 chars). Either this or "
        "--body-file, not both.",
    )
    p.add_argument(
        "--body-file",
        default=None,
        help="read the body from FILE ('-' = stdin; UTF-8)",
    )


def _model_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--model",
        default=None,
        help="declared model identity (advisory string, recorded as-is; "
        "humans may omit)",
    )


def build_list_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas comments list",
        description=(
            "Issue-style listing of a paper's discussions. Filters compose; "
            "--scope lean includes public+lean (Q0 §12.1). Keyset cursor "
            "pagination (per_page default 20, max 100)."
        ),
    )
    p.add_argument("paper_id", help="canonical qa_ paper id")
    p.add_argument("--scope", choices=SCOPES, default=None)
    p.add_argument("--type", default=None, help="preset or custom type slug")
    p.add_argument("--status", choices=STATUSES, default=None)
    p.add_argument("--page-idx", type=int, default=None, help="filter: anchor page")
    p.add_argument("--block-index", type=int, default=None, help="filter: anchor block")
    p.add_argument("--cursor", default=None, help="cursor from a previous page")
    p.add_argument("--per-page", type=int, default=None)
    p.add_argument("--json", action="store_true", help="print the raw server response")
    add_common_http_args(p)
    p.set_defaults(func=cmd_list)
    return p


def build_show_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas comments show",
        description="Discussion detail incl. paginated replies and revisions.",
    )
    p.add_argument("discussion_id")
    p.add_argument("--cursor", default=None, help="replies cursor from a previous page")
    p.add_argument("--per-page", type=int, default=None)
    p.add_argument("--json", action="store_true", help="print the raw server response")
    add_common_http_args(p)
    p.set_defaults(func=cmd_show)
    return p


def build_create_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas comments create",
        description=(
            "Open a root discussion anchored to one block of an immutable "
            "parse revision. Prefer replying to an existing discussion when "
            "one already covers the block."
        ),
    )
    p.add_argument("paper_id", help="canonical qa_ paper id")
    p.add_argument(
        "revision",
        help="parse revision id from `qatlas paper parse-list` (the anchor is "
        "permanent: re-parses create new revisions, they never move this "
        "discussion)",
    )
    p.add_argument("page_idx", type=int, help="0-based page index")
    p.add_argument("block_index", type=int, help="top-level block index")
    _body_args(p)
    p.add_argument(
        "--type",
        default="normal",
        help=f"discussion type; presets: {', '.join(PRESET_TYPES)}; custom "
        "slugs [a-z0-9_:-]+ (default: normal)",
    )
    p.add_argument("--scope", choices=SCOPES, default="public")
    p.add_argument(
        "--status",
        choices=STATUSES,
        default=None,
        help="optional initial status (omit for a plain note with no status)",
    )
    p.add_argument(
        "--reason",
        default=None,
        help="reason recorded with an initial status (server requires one; "
        "defaults to a generic CLI note)",
    )
    _model_arg(p)
    p.add_argument("--json", action="store_true", help="print the raw server response")
    add_common_http_args(p)
    p.set_defaults(func=cmd_create)
    return p


def build_reply_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas comments reply",
        description="Reply to a discussion (evidence, verification results…).",
    )
    p.add_argument("discussion_id")
    _body_args(p)
    _model_arg(p)
    p.add_argument("--json", action="store_true", help="print the raw server response")
    add_common_http_args(p)
    p.set_defaults(func=cmd_reply)
    return p


def build_status_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas comments status",
        description=(
            "Set a discussion's status (pending/confirmed/retracted). Root "
            "author or admin only; the reason is recorded in the status "
            "history and re-opening later is allowed."
        ),
    )
    p.add_argument("discussion_id")
    p.add_argument("new_status", choices=STATUSES, metavar="status")
    p.add_argument(
        "--reason",
        required=True,
        help="why the status changes (required; cite verification replies)",
    )
    p.add_argument("--json", action="store_true", help="print the raw server response")
    add_common_http_args(p)
    p.set_defaults(func=cmd_status)
    return p


def build_edit_discussion_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas comments edit discussion",
        description=(
            "Edit your own root discussion body (author-only per §12.1). "
            "CAS: --if-match <revision> (409 on mismatch); omitted → the "
            "current revision is fetched first. The server keeps revision "
            "history with the actual editor recorded."
        ),
    )
    p.add_argument("discussion_id")
    _body_args(p)
    p.add_argument("--if-match", type=int, default=None, help="expected revision")
    p.add_argument("--json", action="store_true", help="print the raw server response")
    add_common_http_args(p)
    p.set_defaults(func=cmd_edit_discussion)
    return p


def build_edit_reply_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas comments edit reply",
        description=(
            "Edit your own reply body. CAS: --if-match <revision> (409 on "
            "mismatch); omitted → fetched from the discussion detail."
        ),
    )
    p.add_argument("discussion_id")
    p.add_argument("reply_id")
    _body_args(p)
    p.add_argument("--if-match", type=int, default=None, help="expected revision")
    p.add_argument("--json", action="store_true", help="print the raw server response")
    add_common_http_args(p)
    p.set_defaults(func=cmd_edit_reply)
    return p


_HELP = """\
qatlas comments — block-level discussions (read, reply, status)

Usage:
  qatlas comments list   PAPER [--scope public|lean] [--type T]
                                [--status pending|confirmed|retracted]
                                [--page-idx N] [--block-index N]
                                [--cursor C] [--per-page N] [--json]
  qatlas comments show   DISCUSSION_ID [--cursor C] [--per-page N] [--json]
  qatlas comments create PAPER REVISION PAGE_IDX BLOCK_INDEX
                                [BODY | --body-file FILE] [--type T]
                                [--scope public|lean] [--status S]
                                [--model STR] [--json]
  qatlas comments reply  DISCUSSION_ID [BODY | --body-file FILE]
                                [--model STR] [--json]
  qatlas comments status DISCUSSION_ID pending|confirmed|retracted
                                --reason TEXT [--json]
  qatlas comments edit   discussion DISCUSSION_ID [BODY | --body-file FILE]
                                [--if-match N] [--json]
  qatlas comments edit   reply DISCUSSION_ID REPLY_ID [BODY | --body-file FILE]
                                [--if-match N] [--json]

Body limit 20,000 chars; writes auto-send a deterministic Idempotency-Key
(safe retry after timeout). Writing needs a user PAT/session with
comments:write (system PATs: read-only).

Exit codes: 0 ok, 1 transport/5xx, 2 usage, 3 not found, 4 unauthorized,
5 forbidden, 6 conflict/CAS, 7 too large, 8 rate limited, 9 unsupported.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(_HELP, end="")
        return 0
    sub = argv.pop(0)
    if sub == "list":
        parser = build_list_parser()
    elif sub == "show":
        parser = build_show_parser()
    elif sub == "create":
        parser = build_create_parser()
    elif sub == "reply":
        parser = build_reply_parser()
    elif sub == "status":
        parser = build_status_parser()
    elif sub == "edit":
        if not argv or argv[0] in {"-h", "--help"}:
            print(_HELP, end="")
            return 0
        target = argv.pop(0)
        if target == "discussion":
            parser = build_edit_discussion_parser()
        elif target == "reply":
            parser = build_edit_reply_parser()
        else:
            print(
                f"unknown 'comments edit' target {target!r} "
                "(valid: discussion, reply)",
                file=sys.stderr,
            )
            return 2
    else:
        print(
            f"unknown subcommand {sub!r} "
            "(valid: list, show, create, reply, status, edit)",
            file=sys.stderr,
        )
        return 2
    args = parser.parse_args(argv)
    return blockapi.run_cli(args.func, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
