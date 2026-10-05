"""Register verified external PDF sources without inventing DOI identities."""

from __future__ import annotations

import argparse
import sys

from qatlas.client import blockapi
from qatlas.client._common import add_common_http_args, base_url_from_args, print_json

REGISTER_PATH = "/api/papers/source-register"


def cmd_source_register(args: argparse.Namespace) -> int:
    """Mirror the server's required metadata budget before ANY HTTP call."""
    source_url = args.source_url.strip()
    title = args.title.strip()
    authors = [author.strip() for author in args.authors]
    if not source_url:
        raise ValueError("source URL must not be empty")
    if not title or len(title.encode("utf-8")) > 2000:
        raise ValueError("--title must be nonempty and at most 2000 UTF-8 bytes")
    if not 1 <= len(authors) <= 100:
        raise ValueError("provide 1..100 authors using repeatable --author NAME")
    if any(not author or len(author.encode("utf-8")) > 500 for author in authors):
        raise ValueError("each --author must be nonempty and at most 500 UTF-8 bytes")
    if not 1 <= args.year <= 9999:
        raise ValueError("--year must be between 1 and 9999")
    body = blockapi.post_json(
        args, REGISTER_PATH, what="source registration",
        json_body={"source_url": source_url, "title": title,
                   "authors": authors, "year": args.year},
        base_url=base_url_from_args(args), expect_status=(200,),
    )
    if not isinstance(body, dict) or not isinstance(body.get("source"), dict):
        raise blockapi.ApiError(
            "source registration: expected a JSON object with a source object",
            kind="bad_content", exit_code=blockapi.EXIT_TRANSPORT, body=body,
        )
    if args.json:
        # No client projection, metadata enrichment or external identity rewrite.
        print_json(body)
    else:
        source = body["source"]
        state = "created" if body.get("created") else "registered"
        print(f"{state} {body.get('paper_id')} source={source.get('source_id')}")
        print(f"  sha256: {source.get('sha256')}")
        print(f"  source_url: {body.get('source_url')}")
        if body.get("external_id"):
            print(f"  external_id: {body['external_id']}")
    return 0


def build_source_register_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qatlas paper source-register",
        description=(
            "Register an external HTTPS PDF source with required title, authors and year "
            "(papers:write). The server verifies the source and assigns paper/source "
            "identity; this is not downloader enqueue and does not invent a DOI."
        ),
        epilog=(
            "Writes use version preflight and a deterministic Idempotency-Key, never "
            "automatic retries. A lost response means UNKNOWN: inspect server state "
            "before resubmitting. Exit codes follow paper originals: "
            "0 success, 1 transport/server/bad content, 2 usage, 3 not found, "
            "4 unauthorized/version refusal, 5 forbidden, 6 conflict, 7 too large, "
            "8 rate limited, 9 unsupported."
        ),
    )
    parser.add_argument("source_url", metavar="URL", help="external HTTPS PDF or supported landing URL")
    parser.add_argument("--title", required=True, help="paper title (nonempty, max 2000 UTF-8 bytes)")
    parser.add_argument("--author", dest="authors", required=True, action="append", metavar="NAME",
                        help="author name; repeat once per author (1..100, max 500 UTF-8 bytes each)")
    parser.add_argument("--year", required=True, type=int, metavar="YYYY", help="paper year (1..9999)")
    parser.add_argument("--json", action="store_true", help="print the complete server response as JSON")
    add_common_http_args(parser)
    parser.set_defaults(func=cmd_source_register)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_source_register_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return blockapi.run_cli(args.func, args)
