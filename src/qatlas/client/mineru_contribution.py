"""Source-pinned contribution orchestration; all source and provider credentials isolated."""
from __future__ import annotations

import math
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from qatlas._http import unknown_write_count
from qatlas.client import blockapi, blockcache
from qatlas.client.mineru_v1 import MAX_PDF_BYTES, MinerUV1, origin
from qatlas.parser.keyring import AllKeysExhausted
from qatlas.parser.mineru_client import MinerUDailyLimitError, MinerUError, MinerUFatalError, MinerURetryableError


@dataclass
class Prepared:
    identity: str
    claim_id: str
    source_id: str | None
    sha256: str
    workdir: Path
    pdf: Path
    client: MinerUV1 | None = None
    slot: int = -1
    job: dict[str, Any] | None = None
    keep: bool = False


def deadline(args: Any, config: Any) -> float:
    limit = getattr(args, "max_wait", None)
    if limit is None:
        limit = config.mineru_timeout
    limit = float(limit)
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("MinerU wait budget must be finite and positive")
    request_limit = float(args.request_timeout)
    if not math.isfinite(request_limit) or request_limit <= 0:
        raise ValueError("--request-timeout must be finite and positive")
    ttl = getattr(args, "ttl_seconds", None) or 1800
    # Complete/release before the earliest claim can expire, not after.
    return time.monotonic() + min(limit, max(0.1, float(ttl) - 5))


def prepare(owner: Any, args: Any, base_url: str, identity: str, claim: dict[str, Any], verify: bool, headers: dict[str, str], end: float) -> Prepared:
    expected = str(claim.get("pdf_sha256") or "").lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise MinerUFatalError("claim must pin a complete exact PDF SHA-256; refusing unverified parsing")
    locator = claim.get("pdf_url")
    if not isinstance(locator, str) or not locator:
        raise MinerUFatalError("claim is missing the exact PDF locator")
    requires_auth = claim.get("pdf_requires_auth") is True
    target = urljoin(base_url.rstrip("/") + "/", locator)
    base_origin, target_origin = origin(base_url), origin(target)
    parsed = urlsplit(target)
    if parsed.fragment:
        raise MinerUFatalError("source PDF locator cannot contain a fragment")
    source_id = claim.get("source_id")
    if requires_auth:
        if target_origin != base_origin or not source_id or not parsed.path.startswith("/api/papers/") or not parsed.path.endswith("/pdf"):
            raise MinerUFatalError("authenticated PDF claim must pin a same-origin paper/source locator")
        query_source = parse_qs(parsed.query).get("source_id")
        if query_source is not None and query_source != [source_id]:
            raise MinerUFatalError("claim locator conflicts with its exact source_id")
    elif target_origin != base_origin and (parsed.scheme != "https" or parsed.hostname.lower() not in {"arxiv.org", "export.arxiv.org"}):
        raise MinerUFatalError("unauthenticated source claim is not a whitelisted arXiv locator")
    elif target_origin == base_origin:
        # An authenticated old absolute API locator can still be used, but must
        # remain a paper PDF path, never an arbitrary admin/API read.
        if not parsed.path.startswith("/api/papers/") or not parsed.path.endswith("/pdf"):
            raise MinerUFatalError("same-origin source locator is not a paper PDF endpoint")
        requires_auth = True
    workdir = Path(tempfile.mkdtemp(prefix="qatlas-v1-source-"))
    pdf = workdir / "source.pdf"
    session = requests.Session()
    session.trust_env = False
    try:
        for _ in range(6):
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise MinerURetryableError("exact PDF acquisition exceeded wait budget")
            # Only the QAtlas origin receives the user's QAtlas authorization.
            response = session.get(target, headers=headers if requires_auth else {}, verify=verify,
                                   timeout=min(args.request_timeout, remaining), stream=True, allow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise MinerUFatalError("source redirect missing Location")
                next_url = urljoin(target, location)
                next_origin = origin(next_url)
                next_parsed = urlsplit(next_url)
                if requires_auth and next_origin != base_origin:
                    raise MinerUFatalError("refusing cross-origin authenticated PDF redirect")
                if not requires_auth and (next_parsed.scheme != "https" or next_parsed.hostname.lower() not in {"arxiv.org", "export.arxiv.org"}):
                    raise MinerUFatalError("arXiv source redirect is outside the whitelist")
                target = next_url
                session.cookies.clear()
                continue
            try:
                owner.check_response_version(response, write=False)
                if response.status_code != 200:
                    raise MinerUFatalError(f"exact PDF download HTTP {response.status_code}")
                content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
                if content_type not in {"application/pdf", "application/octet-stream"}:
                    raise MinerUFatalError("source response is not PDF/octet-stream")
                if requires_auth:
                    if response.headers.get("X-QAtlas-Source-Id") != source_id:
                        raise MinerUFatalError("PDF response substituted/missed the claim source_id")
                    reported_sha = response.headers.get("X-QAtlas-PDF-SHA256") or response.headers.get("X-QAtlas-Sha256")
                    if reported_sha != expected:
                        raise MinerUFatalError("PDF response SHA disagrees with the source claim")
                original_iter = response.iter_content

                def chunks(chunk_size=1 << 16):
                    size = 0
                    for chunk in original_iter(chunk_size=chunk_size):
                        if time.monotonic() >= end:
                            raise MinerURetryableError("exact PDF transfer exceeded wait budget")
                        size += len(chunk)
                        if size > MAX_PDF_BYTES:
                            raise MinerUFatalError("source PDF exceeds 100MiB")
                        yield chunk

                response.iter_content = chunks
                try:
                    blockcache.verified_response_to_output(response, sha256=expected, output=str(pdf),
                        base_url=base_url, paper_id=None, use_cache=False, log=owner._print_err)
                except blockapi.ApiError as exc:
                    raise MinerUFatalError(exc.message) from exc
                with pdf.open("rb") as source:
                    if source.read(5) != b"%PDF-":
                        raise MinerUFatalError("verified source is not a PDF artifact")
                return Prepared(identity, str(claim["claim_id"]), source_id, expected, workdir, pdf)
            finally:
                response.close()
        raise MinerUFatalError("source exceeded redirect limit")
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    finally:
        session.close()


def submit(owner: Any, args: Any, config: Any, prepared: Prepared, key_ring: Any, end: float) -> None:
    while True:
        if owner._SHUTDOWN_REQUESTED:
            raise MinerURetryableError("shutdown requested before V1 submission")
        if key_ring is not None:
            legacy, slot = key_ring.acquire()
            token = legacy.token
        else:
            slot, token = -1, config.mineru_api_token
        client = MinerUV1(token, base_url=config.mineru_api_base_url, timeout=args.request_timeout, deadline=end)
        try:
            file_id = client.upload(prepared.pdf, prepared.sha256)
            tier = getattr(args, "tier", None) or config.mineru_tier
            job = client.submit(file_id, tier=tier, ocr=config.mineru_is_ocr)
            prepared.client, prepared.slot, prepared.job = client, slot, job
            owner._print_err(f"MinerU V1 job {job['job_id']} for exact source {prepared.source_id or prepared.sha256}")
            return
        except MinerUDailyLimitError:
            client.close()
            if key_ring is None or slot < 0:
                raise
            key_ring.mark_daily_limit(slot)
            if key_ring.available_slots() == 0:
                raise
            # Only confirmed quota errors rotate; unknown submissions are never
            # replayed, and file ids are never reused across account/token slots.
            owner._print_err(f"MinerU V1 key slot {slot} quota exhausted; rotating")
        except BaseException:
            client.close()
            raise


def finish(owner: Any, args: Any, base_url: str, prepared: Prepared, verify: bool, headers: dict[str, str], tier: str) -> bool:
    file_id = prepared.client.output(prepared.job)
    if file_id is None:
        return False
    archive = prepared.workdir / "mineru-result.zip"
    prepared.client.download_zip(file_id, archive)
    if args.no_push:
        prepared.keep = True
        owner._print_err(f"[no-push] complete original ZIP retained at {archive}")
        return True
    unknown_before = unknown_write_count()
    try:
        ok, payload = owner._upload_mineru_zip(base_url=base_url, arxiv_id=prepared.identity,
            zip_path=archive, overwrite=args.overwrite, request_timeout=args.request_timeout,
            verify=verify, headers=headers, pdf_sha256=prepared.sha256,
            source_id=prepared.source_id, tier=tier)
    except BaseException:
        prepared.keep = True
        owner._print_err(f"Upload failed/refused or result UNKNOWN; complete ZIP retained at {archive}; check server revisions before retry")
        raise
    if unknown_write_count() != unknown_before:
        prepared.keep = True
    if not ok:
        prepared.keep = True
        owner._print_err(f"{payload}\ncomplete ZIP retained at {archive}")
        raise MinerUFatalError("QAtlas rejected the complete source-bound result")
    if not isinstance(payload, dict) or (prepared.source_id and payload.get("source_id") != prepared.source_id) or (
        payload.get("source_sha256") and payload["source_sha256"] != prepared.sha256
    ):
        prepared.keep = True
        raise MinerUFatalError("QAtlas upload returned conflicting source identity; result may already exist, check server before retry")
    owner.print_json(payload)
    return True


def cleanup(owner: Any, args: Any, base_url: str, prepared: Prepared, verify: bool, headers: dict[str, str]) -> None:
    if prepared.client is not None:
        prepared.client.close()
    owner._release_claim(base_url, prepared.identity, prepared.claim_id, request_timeout=args.request_timeout, verify=verify, headers=headers)
    if not prepared.keep:
        shutil.rmtree(prepared.workdir, ignore_errors=True)


def process(owner: Any, args: Any, base_url: str, config: Any, identity: str, verify: bool, headers: dict[str, str], key_ring: Any) -> int:
    end = deadline(args, config)
    claim, skip = owner._claim_one(base_url, identity, request_timeout=args.request_timeout,
        verify=verify, headers=headers, ttl_seconds=args.ttl_seconds)
    if claim is None:
        owner._print_err(f"[skip] {identity}: {skip}")
        return 0 if skip and skip.startswith("skip") else 1
    prepared = None
    try:
        prepared = prepare(owner, args, base_url, identity, claim, verify, headers, end)
        submit(owner, args, config, prepared, key_ring, end)
        while time.monotonic() < end and not owner._SHUTDOWN_REQUESTED:
            if finish(owner, args, base_url, prepared, verify, headers, getattr(args, "tier", None) or config.mineru_tier):
                return 0
            time.sleep(min(max(float(config.mineru_poll_interval), 1), max(0, end - time.monotonic())))
            try:
                prepared.job = prepared.client.poll(prepared.job["job_id"])
            except MinerURetryableError as exc:
                owner._print_err(f"retryable V1 status: {exc}")
        raise MinerURetryableError("V1 wait expired or shutdown requested; provider job may still be running")
    except (MinerUDailyLimitError, AllKeysExhausted) as exc:
        owner._print_err(f"[daily-limit] {exc}")
        return owner.EXIT_DAILY_LIMIT
    except (MinerUError, requests.RequestException) as exc:
        owner._print_err(f"MinerU V1 contribution failed: {exc}")
        return 1
    finally:
        if prepared is not None:
            cleanup(owner, args, base_url, prepared, verify, headers)
        else:
            owner._release_claim(base_url, identity, str(claim["claim_id"]), request_timeout=args.request_timeout, verify=verify, headers=headers)


def drain(owner: Any, args: Any, base_url: str, config: Any, verify: bool, headers: dict[str, str], key_ring: Any) -> Any:
    end = deadline(args, config)
    response = owner.requests.get(f"{base_url}/api/papers/needs-mineru", params={"limit": max(1, min(int(args.batch_size), owner.MAX_BATCH_SIZE))},
        headers={**headers, **owner.client_version_headers()}, timeout=args.request_timeout, verify=verify)
    owner.check_response_version(response, write=False)
    if not response.ok:
        owner._print_err(owner._http_error(response, "needs-mineru list"))
        return owner._BatchOutcome(0, 1, False)
    candidates = response.json().get("papers") or []
    active: list[Prepared] = []
    processed = failures = 0
    daily = False
    unknown_before = unknown_write_count()
    try:
        for candidate in candidates:
            if owner._SHUTDOWN_REQUESTED or time.monotonic() >= end:
                break
            identity = candidate["arxiv_id"]
            claim, skip = owner._claim_one(base_url, identity, request_timeout=args.request_timeout, verify=verify,
                headers=headers, ttl_seconds=args.ttl_seconds)
            if claim is None:
                if skip and not skip.startswith("skip"):
                    failures += 1
                    processed += 1
                continue
            item = None
            try:
                item = prepare(owner, args, base_url, identity, claim, verify, headers, end)
                submit(owner, args, config, item, key_ring, end)
                active.append(item)
            except (MinerUDailyLimitError, AllKeysExhausted) as exc:
                daily = True
                owner._print_err(f"[daily-limit] {exc}")
                failures += 1
                processed += 1
                if item is not None:
                    cleanup(owner, args, base_url, item, verify, headers)
                else:
                    owner._release_claim(base_url, identity, str(claim["claim_id"]), request_timeout=args.request_timeout, verify=verify, headers=headers)
                break
            except (MinerUError, requests.RequestException) as exc:
                failures += 1
                processed += 1
                owner._print_err(f"V1 source/submission failed: {exc}")
                if item is not None:
                    cleanup(owner, args, base_url, item, verify, headers)
                else:
                    owner._release_claim(base_url, identity, str(claim["claim_id"]), request_timeout=args.request_timeout, verify=verify, headers=headers)
                if unknown_write_count() != unknown_before:
                    break
        while active and time.monotonic() < end and not owner._SHUTDOWN_REQUESTED:
            for item in list(active):
                try:
                    tier = getattr(args, "tier", None) or config.mineru_tier
                    if finish(owner, args, base_url, item, verify, headers, tier):
                        processed += 1
                        active.remove(item)
                        cleanup(owner, args, base_url, item, verify, headers)
                        continue
                    item.job = item.client.poll(item.job["job_id"])
                except MinerURetryableError as exc:
                    owner._print_err(f"retryable V1 status: {exc}")
                except (MinerUError, requests.RequestException) as exc:
                    daily = daily or isinstance(exc, MinerUDailyLimitError)
                    failures += 1
                    processed += 1
                    active.remove(item)
                    owner._print_err(f"V1 job/result failed: {exc}")
                    cleanup(owner, args, base_url, item, verify, headers)
                    if unknown_write_count() != unknown_before:
                        break
            if active:
                time.sleep(min(max(float(config.mineru_poll_interval), 1), max(0, end - time.monotonic())))
        if active:
            failures += len(active)
            processed += len(active)
            owner._print_err("V1 bounded wait ended; unfinished provider jobs may still run, not silently resubmitted")
        return owner._BatchOutcome(processed, failures, daily)
    finally:
        for item in active:
            cleanup(owner, args, base_url, item, verify, headers)
