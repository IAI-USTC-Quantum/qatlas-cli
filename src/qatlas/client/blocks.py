"""``qatlas paper pdf|parse-list|parse-json|block-list|block-get|block-image``
— originals, parse revisions and block reads for block-level comments
(plan §12.2 Q1 endpoints, §12.4 command surface).

Commands::

    qatlas paper pdf         ID [--source S|--version vN] [-o FILE]
                                    [--no-cache] [--force-refresh]
    qatlas paper parse-list  ID [--json]
    qatlas paper parse-json  ID REVISION [-o FILE] [--no-cache] [--force-refresh]
    qatlas paper block-list  ID REVISION [--page-idx N] [--cursor C]
                                    [--per-page N] [--json]
    qatlas paper block-get   ID REVISION PAGE_IDX BLOCK_INDEX [--json]
    qatlas paper block-image ID REVISION PAGE_IDX BLOCK_INDEX [-o FILE]

Output discipline:

* byte commands (``pdf`` / ``parse-json`` / ``block-image``) emit the
  raw original bytes to stdout (or ``--output``) and nothing else;
  progress / cache notes go to stderr.
* ``--json`` prints the *complete* machine response (re-serialized
  verbatim, never a truncated reading window); the default is a
  compact human table/summary on stdout.

``ID`` is the canonical ``qa_`` paper id (aliases the server resolver
accepts also work, but cache reuse requires ``qa_``). ``REVISION`` is
a parse revision id from ``parse-list`` — never "latest": comments
pin their anchor to an immutable revision (plan §4.2), and so do we.

Exit codes: 0 ok, 1 transport/5xx/bad content, 2 usage, 3 not found,
4 unauthorized, 5 forbidden, 7 too large, 8 rate limited, 9 server
lacks block-comments support. See ``qatlas.client.blockapi``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from typing import Any

from qatlas.client import blockapi, blockcache
from qatlas.client._common import add_common_http_args, base_url_from_args

_QA_ID_RE = re.compile(r"^qa_[0-9a-z]+$")

# Preset note shown after a successful block-get (plan §7.2: gentle
# contribution *hint* on stderr, never inside the original bytes).
_CONTRIB_HINT = (
    "If you verified something against this block and hold write "
    "authorization (comments:write), you may leave your conclusion and "
    "evidence with `qatlas comments create` — prefer replying to an "
    "existing discussion over opening a new one."
)


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _id_arg(p: argparse.ArgumentParser, *, canonical_only: bool = False) -> None:
    help_text = (
        "canonical qa_ paper id (qa_ + 26-char ULID)"
        if canonical_only
        else "paper id — canonical qa_ form preferred (aliases the server "
        "resolver accepts also work; cache reuse requires qa_)"
    )
    p.add_argument("paper_id", help=help_text)


def _revision_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "revision",
        help="parse revision id from `qatlas paper parse-list` (immutable; "
        "comments anchor to it, never to 'latest')",
    )


def _anchor_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("page_idx", type=int, help="0-based page index (page_idx)")
    p.add_argument(
        "block_index",
        type=int,
        help="top-level block index within the page (block.index; NOT the "
        "array position or the visible markdown paragraph number)",
    )


def _output_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help='Write bytes to FILE. Use "-" or omit for stdout.',
    )


def _cache_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the local content-addressed cache (always download).",
    )
    p.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-download even if a verified cache entry exists.",
    )


# ---------------------------------------------------------------------------
# API helpers (§12.2 Q1)
# ---------------------------------------------------------------------------


def fetch_sources(args: argparse.Namespace, base_url: str) -> dict[str, Any]:
    path = f"/api/papers/{blockapi.encode_path_segment(args.paper_id)}/sources"
    return blockapi.get_json(args, path, what="sources list", base_url=base_url)


def fetch_parses(args: argparse.Namespace, base_url: str) -> dict[str, Any]:
    path = f"/api/papers/{blockapi.encode_path_segment(args.paper_id)}/parses"
    return blockapi.get_json(args, path, what="parse list", base_url=base_url)


def _source_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    items = body.get("items")
    if items is None and isinstance(body.get("sources"), list):
        items = body["sources"]
    if not isinstance(items, list):
        raise blockapi.ApiError(
            "sources list: unexpected response shape (no items array)",
            kind="bad_content",
            exit_code=blockapi.EXIT_TRANSPORT,
            body=body,
        )
    return [it for it in items if isinstance(it, dict)]


def _pick_source(
    body: dict[str, Any],
    *,
    source_id: str | None,
    version: str | None,
) -> dict[str, Any]:
    """Select one source per §12.2 rules — explicit pin never falls back."""
    items = _source_items(body)
    if not items:
        raise blockapi.ApiError(
            "no source PDF assets recorded for this paper",
            kind="not_found",
            exit_code=blockapi.EXIT_NOT_FOUND,
        )
    if source_id is not None:
        for it in items:
            if str(it.get("source_id")) == source_id:
                return it
        raise blockapi.ApiError(
            f"source {source_id!r} not found; available: "
            + ", ".join(str(i.get("source_id")) for i in items),
            kind="not_found",
            exit_code=blockapi.EXIT_NOT_FOUND,
        )
    if version is not None:
        v = version.lstrip("vV")
        matches = [
            it
            for it in items
            if str(it.get("origin", "")).rstrip().endswith(f":v{v}")
            or str(it.get("origin", "")) == f"arxiv:v{v}"
        ]
        if not matches:
            raise blockapi.ApiError(
                f"no source with origin version v{v}; refusing to fall back to "
                "another version. Available origins: "
                + ", ".join(str(i.get("origin")) for i in items),
                kind="not_found",
                exit_code=blockapi.EXIT_NOT_FOUND,
            )
        if len(matches) > 1:
            raise blockapi.ApiError(
                f"multiple sources claim version v{v} — disambiguate with "
                "--source: "
                + ", ".join(str(i.get("source_id")) for i in matches),
                kind="usage",
                exit_code=blockapi.EXIT_USAGE,
            )
        return matches[0]
    # Default: the current pointer (item-level is_current, else the
    # response's current_source_id field).
    currents = [it for it in items if it.get("is_current")]
    if len(currents) == 1:
        return currents[0]
    top = body.get("current_source_id")
    if top:
        for it in items:
            if str(it.get("source_id")) == str(top):
                return it
    if len(items) == 1:
        return items[0]
    raise blockapi.ApiError(
        "multiple sources and no unambiguous current pointer — pick one with "
        "`--source SOURCE_ID` (see `qatlas paper sources` output); available: "
        + ", ".join(
            f"{i.get('source_id')} (origin={i.get('origin')})" for i in items
        ),
        kind="usage",
        exit_code=blockapi.EXIT_USAGE,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_pdf(args: argparse.Namespace) -> int:
    """Download the original PDF bytes (hash-verified, cached)."""
    base_url = base_url_from_args(args)
    sources = fetch_sources(args, base_url)
    src = _pick_source(
        sources, source_id=args.source, version=args.version
    )
    sha256 = str(src.get("sha256") or "")
    source_id = str(src.get("source_id") or "")
    if not sha256 or not source_id:
        raise blockapi.ApiError(
            f"sources entry missing sha256/source_id: {src!r}",
            kind="bad_content",
            exit_code=blockapi.EXIT_TRANSPORT,
        )
    path = (
        f"/api/papers/{blockapi.encode_path_segment(args.paper_id)}/sources/"
        f"{blockapi.encode_path_segment(source_id)}/pdf"
    )

    cacheable = _QA_ID_RE.match(args.paper_id) is not None
    if not cacheable:
        _stderr(
            "Note: paper id is not canonical qa_ — download proceeds but is "
            "not cached (resolve the qa_ id for cache reuse)."
        )
        data, _ = blockapi.download_bytes(
            args,
            path,
            what="pdf",
            expected_content_prefixes=("application/pdf", "application/octet-stream"),
            base_url=base_url,
        )
    else:
        data, hit = blockcache.cached_download(
            args,
            base_url=base_url,
            paper_id=args.paper_id,
            kind="pdf",
            sha256=sha256,
            url_path=path,
            what="pdf",
            expected_content_prefixes=("application/pdf", "application/octet-stream"),
            force_refresh=args.force_refresh,
            use_cache=not args.no_cache,
            log=_stderr,
        )
        if not hit:
            _stderr(f"downloaded {len(data)} bytes (sha256 verified: {sha256})")
    blockcache.stream_out(data, args.output)
    return 0


def cmd_parse_list(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    body = fetch_parses(args, base_url)
    if args.json:
        blockapi_json_print(body)
        return 0
    items = [
        it
        for it in (body.get("items") or [])
        if isinstance(it, dict)
    ]
    if not items:
        _stderr("no parse revisions recorded for this paper")
        return 0
    current = body.get("current_revision_id")
    print(f"{len(items)} parse revision(s):")
    for it in items:
        rev = str(it.get("revision_id") or it.get("revision") or "?")
        mark = "*"
        if it.get("is_current") or (current and str(current) == rev):
            mark = "→"  # current pointer
        schema = f"{it.get('schema', '?')}@{it.get('schema_version', '?')}"
        src = str(it.get("source_id") or "?")
        print(
            f"  {mark} {rev}  schema={schema}  source={src}  "
            f"artifact_sha256={str(it.get('artifact_sha256', '?'))[:12]}…  "
            f"created={it.get('created_at', '?')}"
        )
    _stderr("(→ = current pointer; comments anchor to the revision id, not 'current')")
    return 0


def blockapi_json_print(body: dict[str, Any]) -> None:
    """Print the full machine JSON (never a truncated reading window)."""
    print(json.dumps(body, ensure_ascii=False, indent=2))


def cmd_parse_json(args: argparse.Namespace) -> int:
    """Download the original parse JSON bytes (hash-verified, cached)."""
    base_url = base_url_from_args(args)
    parses = fetch_parses(args, base_url)
    items = [it for it in (parses.get("items") or []) if isinstance(it, dict)]
    match = None
    for it in items:
        if str(it.get("revision_id") or it.get("revision")) == args.revision:
            match = it
            break
    if match is None:
        raise blockapi.ApiError(
            f"parse revision {args.revision!r} not found for this paper; "
            "list them with `qatlas paper parse-list`",
            kind="not_found",
            exit_code=blockapi.EXIT_NOT_FOUND,
        )
    sha256 = str(match.get("artifact_sha256") or "")
    if not sha256:
        raise blockapi.ApiError(
            f"parse revision {args.revision!r} has no artifact_sha256 — "
            "cannot verify download integrity",
            kind="bad_content",
            exit_code=blockapi.EXIT_TRANSPORT,
        )
    path = (
        f"/api/papers/{blockapi.encode_path_segment(args.paper_id)}/parses/"
        f"{blockapi.encode_path_segment(args.revision)}/json"
    )
    cacheable = _QA_ID_RE.match(args.paper_id) is not None
    if cacheable:
        data, hit = blockcache.cached_download(
            args,
            base_url=base_url,
            paper_id=args.paper_id,
            kind="parse-json",
            sha256=sha256,
            url_path=path,
            what="parse json",
            expected_content_prefixes=("application/json", "application/octet-stream"),
            force_refresh=args.force_refresh,
            use_cache=not args.no_cache,
            log=_stderr,
        )
        if not hit:
            _stderr(f"downloaded {len(data)} bytes (sha256 verified: {sha256})")
    else:
        data, _ = blockapi.download_bytes(
            args,
            path,
            what="parse json",
            expected_content_prefixes=("application/json", "application/octet-stream"),
            base_url=base_url,
        )
    blockcache.stream_out(data, args.output)
    return 0


def cmd_block_list(args: argparse.Namespace) -> int:
    base_url = base_url_from_args(args)
    per_page = blockapi.clamp_per_page(args.per_page)
    params: dict[str, Any] = {"per_page": per_page}
    if args.page_idx is not None:
        params["page_idx"] = args.page_idx
    if args.cursor:
        params["cursor"] = args.cursor
    path = (
        f"/api/papers/{blockapi.encode_path_segment(args.paper_id)}/parses/"
        f"{blockapi.encode_path_segment(args.revision)}/blocks"
    )
    body = blockapi.get_json(
        args, path, what="block list", params=params, base_url=base_url
    )
    if args.json:
        blockapi_json_print(body)
        return 0
    items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
    print(f"{len(items)} block(s) on this page of results:")
    for it in items:
        idx = it.get("index", it.get("block_index", "?"))
        page = it.get("page_idx", "?")
        btype = it.get("type", "?")
        excerpt = _excerpt(it)
        print(f"  page={page} block={idx} type={btype:<18} {excerpt}")
    cursor = body.get("next_cursor")
    if cursor:
        print(f"next_cursor: {cursor}")
        _stderr(
            "more results exist — pass --cursor (machine: read next_cursor "
            "from --json output)"
        )
    return 0


def _excerpt(it: dict[str, Any]) -> str:
    for key in ("content", "text", "html"):
        val = it.get(key)
        if isinstance(val, str) and val.strip():
            flat = " ".join(val.split())
            return flat[:72] + ("…" if len(flat) > 72 else "")
    return "(no text preview)"


def cmd_block_get(args: argparse.Namespace) -> int:
    """Combined single-block read: source + anchor + content + discussions."""
    base_url = base_url_from_args(args)
    path = (
        f"/api/papers/{blockapi.encode_path_segment(args.paper_id)}/parses/"
        f"{blockapi.encode_path_segment(args.revision)}/blocks/"
        f"{args.page_idx}/{args.block_index}"
    )
    body = blockapi.get_json(args, path, what="block get", base_url=base_url)
    if args.json:
        blockapi_json_print(body)
    else:
        _render_block(body)
    if not args.quiet_notes:
        _stderr(f"hint: {_CONTRIB_HINT}")
    return 0


def _render_block(body: dict[str, Any]) -> None:
    source = body.get("source") or {}
    anchor = body.get("anchor") or {}
    content = body.get("content") or {}
    discussions = body.get("discussions") or []
    print(
        f"paper {source.get('paper_id', '?')}  schema "
        f"{source.get('schema', '?')}@{source.get('schema_version', '?')}  "
        f"pdf_sha256={str(source.get('pdf_sha256', '?'))[:12]}…"
    )
    print(
        f"anchor page_idx={anchor.get('page_idx', '?')} "
        f"block_index={anchor.get('block_index', '?')}"
    )
    text = content.get("content") or content.get("text") or ""
    flat = " ".join(str(text).split())
    print(f"content type={content.get('type', '?')}:")
    print(f"  {flat[:500]}{'…' if len(flat) > 500 else ''}")
    print(f"discussions: {len(discussions)}")
    for d in discussions:
        if isinstance(d, dict):
            print(
                f"  [{d.get('status') or 'none'}] {d.get('type', '?')}/"
                f"{d.get('scope', '?')} by {d.get('created_by', '?')}: "
                f"{_excerpt(d)}"
            )
    cursor = body.get("next_cursor")
    if cursor:
        print(f"next_cursor: {cursor} (more discussions — see `qatlas comments list`)")


def cmd_block_image(args: argparse.Namespace) -> int:
    """Original-image crop of one block (rendered from the source PDF,
    never a re-drawn transcription). No known hash → not cached."""
    base_url = base_url_from_args(args)
    path = (
        f"/api/papers/{blockapi.encode_path_segment(args.paper_id)}/parses/"
        f"{blockapi.encode_path_segment(args.revision)}/blocks/"
        f"{args.page_idx}/{args.block_index}/image"
    )
    data, resp = blockapi.download_bytes(
        args,
        path,
        what="block image",
        expected_content_prefixes=("image/",),
        base_url=base_url,
    )
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0]
    _stderr(f"{len(data)} bytes ({ctype})")
    blockcache.stream_out(data, args.output)
    return 0


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def build_pdf_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper pdf",
        description=(
            "Download a paper's original PDF bytes. The source is pinned: "
            "--source SOURCE_ID or --version vN never fall back to another "
            "version; default is the current source pointer. Downloads are "
            "sha256-verified against the sources listing and published "
            "atomically to the content-addressed cache (config: cache_dir)."
        ),
    )
    _id_arg(p)
    p.add_argument("--source", default=None, help="exact source_id from the sources listing")
    p.add_argument(
        "--version",
        default=None,
        metavar="vN",
        help="pin the arXiv origin version, e.g. v2 (matches origin 'arxiv:v2'); "
        "fails rather than substituting the latest version",
    )
    _output_arg(p)
    _cache_args(p)
    add_common_http_args(p)
    p.set_defaults(func=cmd_pdf)
    return p


def build_parse_list_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper parse-list",
        description=(
            "List a paper's immutable parse revisions (schema, "
            "schema_version, artifact_sha256, source, current pointer). "
            "Use a revision id with block-* commands and comments anchors."
        ),
    )
    _id_arg(p)
    p.add_argument("--json", action="store_true", help="print the raw server response")
    add_common_http_args(p)
    p.set_defaults(func=cmd_parse_list)
    return p


def build_parse_json_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper parse-json",
        description=(
            "Download the original MinerU middle-JSON bytes for one parse "
            "revision (the immutable artifact — not a re-serialization). "
            "Hash-verified against artifact_sha256 and cached like the PDF."
        ),
    )
    _id_arg(p)
    _revision_arg(p)
    _output_arg(p)
    _cache_args(p)
    add_common_http_args(p)
    p.set_defaults(func=cmd_parse_json)
    return p


def build_block_list_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper block-list",
        description=(
            "Page through the top-level blocks of a parse revision "
            "(keyset cursor pagination: per_page default 20, max 100). "
            "Block identity is the original page_idx + block index — not "
            "an array offset."
        ),
    )
    _id_arg(p)
    _revision_arg(p)
    p.add_argument("--page-idx", type=int, default=None, help="filter to one page (0-based)")
    p.add_argument("--cursor", default=None, help="cursor from a previous page (next_cursor)")
    p.add_argument("--per-page", type=int, default=None, help=f"page size 1..100 (default {blockapi.PER_PAGE_DEFAULT})")
    p.add_argument("--json", action="store_true", help="print the raw server response")
    add_common_http_args(p)
    p.set_defaults(func=cmd_block_list)
    return p


def build_block_get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper block-get",
        description=(
            "Combined single-block read: source identity + anchor + block "
            "content + the discussions anchored to this block. --json gives "
            "the complete machine response."
        ),
    )
    _id_arg(p)
    _revision_arg(p)
    _anchor_args(p)
    p.add_argument("--json", action="store_true", help="print the raw server response")
    p.add_argument(
        "--quiet-notes",
        action="store_true",
        help="suppress the stderr contribution hint",
    )
    add_common_http_args(p)
    p.set_defaults(func=cmd_block_get)
    return p


def build_block_image_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qatlas paper block-image",
        description=(
            "Download the original-image crop of one block (rendered from "
            "the source PDF and cropped — not a re-drawn transcription). "
            "A missing bbox or missing source is reported by the server as "
            "an error; this CLI never fabricates an image."
        ),
    )
    _id_arg(p)
    _revision_arg(p)
    _anchor_args(p)
    _output_arg(p)
    add_common_http_args(p)
    p.set_defaults(func=cmd_block_image)
    return p


_PARSERS: dict[str, Callable[[], argparse.ArgumentParser]] = {
    "pdf": build_pdf_parser,
    "parse-list": build_parse_list_parser,
    "parse-json": build_parse_json_parser,
    "block-list": build_block_list_parser,
    "block-get": build_block_get_parser,
    "block-image": build_block_image_parser,
}


_BLOCK_HELP = """\
qatlas paper <pdf|parse-list|parse-json|block-list|block-get|block-image> —
originals, parse revisions and block reads for block-level comments.

Usage:
  qatlas paper pdf         ID [--source S|--version vN] [-o FILE]
                                  [--no-cache] [--force-refresh]
  qatlas paper parse-list  ID [--json]
  qatlas paper parse-json  ID REVISION [-o FILE] [--no-cache] [--force-refresh]
  qatlas paper block-list  ID REVISION [--page-idx N] [--cursor C]
                                  [--per-page N] [--json]
  qatlas paper block-get   ID REVISION PAGE_IDX BLOCK_INDEX [--json]
  qatlas paper block-image ID REVISION PAGE_IDX BLOCK_INDEX [-o FILE]

ID is the canonical qa_ paper id; REVISION is a parse revision id from
`parse-list` (immutable — comments anchor to it, never to 'latest').

Exit codes: 0 ok, 1 transport/5xx/bad content, 2 usage, 3 not found,
4 unauthorized, 5 forbidden, 7 too large, 8 rate limited, 9 unsupported
server. Use 'qatlas paper <subcommand> --help' for details.
"""


def main(argv: list[str] | None = None) -> int:
    """Dispatch one of the block-originals paper subcommands.

    Called from :mod:`qatlas.client.paper` — not a top-level entry.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(_BLOCK_HELP, end="")
        return 0
    sub = argv.pop(0)
    builder = _PARSERS.get(sub)
    if builder is None:
        print(
            f"unknown subcommand {sub!r} (valid: {', '.join(sorted(_PARSERS))})",
            file=sys.stderr,
        )
        return 2
    parser = builder()
    args = parser.parse_args(argv)
    return blockapi.run_cli(args.func, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
