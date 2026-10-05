"""Token-isolated MinerU V1 uploads/jobs and byte-exact result ZIP transport.

QAtlas credentials never enter this client. API bearer stays on the configured
origin; presigned PUTs and cross-origin result downloads carry no bearer/cookies.
"""
from __future__ import annotations

import hashlib
import json
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests

from qatlas._http import write_request
from qatlas.parser.mineru_client import (
    MinerUDailyLimitError, MinerUFatalError, MinerURetryableError,
    classify_mineru_error,
)

MAX_PDF_BYTES = 100 << 20
MAX_ZIP_BYTES = 128 << 20
MAX_JSON_BYTES = 1 << 20


def origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise MinerUFatalError("invalid HTTP origin")
    return parsed.scheme.lower(), parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def api_base(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    origin(value)
    if parsed.query or parsed.fragment:
        raise MinerUFatalError("MinerU API base cannot contain query/fragment")
    if parsed.hostname.lower() in {"mineru.net", "www.mineru.net"} and not parsed.path:
        value += "/api"
    return value


def classify_problem(body: dict[str, Any], status: int, *, token: str = "") -> Exception:
    problem = body.get("error")
    problem = problem if isinstance(problem, dict) else {}
    code = str(problem.get("code") or body.get("msgCode") or "")
    message = str(problem.get("message") or body.get("msg") or f"MinerU V1 HTTP {status}")
    if token:
        code, message = code.replace(token, "***"), message.replace(token, "***")
    if code in {"daily_limit_exceeded", "daily_quota_exceeded", "quota_exceeded", "insufficient_quota"}:
        return MinerUDailyLimitError(message, code=code, http_status=status)
    if code in {"invalid_api_key", "invalid_request", "invalid_parameter", "file_too_large", "too_many_pages", "unsupported_file_type"}:
        return MinerUFatalError(message, code=code, http_status=status)
    if code in {"rate_limit_exceeded", "queue_full", "service_unavailable", "timeout"}:
        return MinerURetryableError(message, code=code, http_status=status)
    return classify_mineru_error(code=code, msg=message, http_status=status)


class MinerUV1:
    def __init__(self, token: str, *, base_url: str, timeout: float, deadline: float) -> None:
        self.token = token
        self.base_url = api_base(base_url)
        self.timeout = timeout
        self.deadline = deadline
        self.session = requests.Session()
        # No ambient .netrc/cookies may add credentials to presigned hosts.
        self.session.trust_env = False

    def close(self) -> None:
        self.session.close()

    def request_timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MinerURetryableError("MinerU V1 bounded wait budget exhausted")
        return min(self.timeout, remaining)

    def _json(self, method: str, endpoint: str, body: Any = None) -> dict[str, Any]:
        url = self.base_url + endpoint
        if origin(url) != origin(self.base_url):
            raise MinerUFatalError("refusing cross-origin V1 API credential request")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        kwargs = dict(headers=headers, timeout=self.request_timeout(), allow_redirects=False, stream=True)
        if body is not None:
            kwargs["json"] = body
        def call():
            self.session.cookies.clear()
            return self.session.request(method, url, **kwargs)

        response = write_request(call) if method != "GET" else call()
        try:
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=1 << 16):
                if time.monotonic() >= self.deadline:
                    raise MinerURetryableError("MinerU V1 response exceeded wait budget")
                raw.extend(chunk)
                if len(raw) > MAX_JSON_BYTES:
                    raise MinerUFatalError("V1 JSON response exceeds 1MiB")
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise MinerUFatalError(f"V1 API returned non-JSON HTTP {response.status_code}") from exc
            if not isinstance(data, dict):
                raise MinerUFatalError("V1 API response must be an object")
            if data.get("error") is not None and not isinstance(data["error"], dict):
                raise MinerUFatalError("V1 API error field must be an object")
            if not 200 <= response.status_code < 300 or data.get("error") or str(data.get("msgCode", "0")) not in {"", "0"}:
                raise classify_problem(data, response.status_code, token=self.token)
            return data
        finally:
            response.close()

    def upload(self, pdf: Path, sha256: str) -> str:
        size = pdf.stat().st_size
        if not 0 < size <= MAX_PDF_BYTES:
            raise MinerUFatalError("exact source PDF exceeds upload size bounds")
        digest = hashlib.sha256()
        with pdf.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 16), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha256:
            raise MinerUFatalError("source PDF changed before V1 upload")
        ticket = self._json("POST", "/v1/uploads", {
            "filename": pdf.name, "bytes": size, "mime_type": "application/pdf",
            "purpose": "parse", "sha256sum": sha256,
        })
        if ticket.get("status") == "completed":
            uploaded = ticket.get("file")
            file_id = uploaded.get("id") if isinstance(uploaded, dict) else None
            if not isinstance(file_id, str) or not file_id:
                raise MinerUFatalError("V1 completed upload missing file.id")
            return file_id
        upload_id, target = ticket.get("id"), ticket.get("upload_url")
        if not isinstance(upload_id, str) or not upload_id or not isinstance(target, str) or not target:
            raise MinerUFatalError("V1 upload ticket missing id/upload_url")
        if target.startswith("/") and not target.startswith("//"):
            target = self.base_url + target
        target_origin = origin(target)
        if target_origin[0] != "https" and target_origin != origin(self.base_url):
            raise MinerUFatalError("cross-origin V1 upload must use HTTPS")
        headers = ticket.get("upload_headers") or {}
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            raise MinerUFatalError("invalid V1 upload headers")
        forbidden = {"authorization", "proxy-authorization", "cookie", "host"}
        if any(k.lower() in forbidden or (self.token and self.token in value) for k, value in headers.items()):
            raise MinerUFatalError("provider-supplied upload credentials/Host are not accepted")
        # Never attach API bearer/cookies to an upload presign, even same-origin PUTs.
        self.session.cookies.clear()
        with pdf.open("rb") as source:
            response = write_request(
                self.session.put, target, data=source, headers=headers,
                timeout=self.request_timeout(), allow_redirects=False,
            )
        try:
            if not 200 <= response.status_code < 300:
                raise MinerURetryableError(f"V1 PDF PUT HTTP {response.status_code}")
        finally:
            response.close()
        completed = self._json("POST", "/v1/uploads/" + quote(upload_id, safe="") + "/complete")
        uploaded = completed.get("file")
        file_id = uploaded.get("id") if isinstance(uploaded, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise MinerUFatalError("V1 upload completion missing file.id")
        return file_id

    def submit(self, file_id: str, *, tier: str, ocr: bool) -> dict[str, Any]:
        if tier not in {"flash", "basic", "standard", "advanced"}:
            raise MinerUFatalError("unsupported V1 tier (legacy model_version is not a tier)")
        job = self._json("POST", "/v1/parse/jobs", {
            "files": [{"source": {"type": "file_id", "file_id": file_id}}],
            "output_formats": ["zip"], "tier": tier, "ocr_mode": "ocr" if ocr else "auto",
        })
        if not isinstance(job.get("job_id"), str) or not job["job_id"]:
            raise MinerUFatalError("V1 parse response missing job_id")
        return job

    def poll(self, job_id: str) -> dict[str, Any]:
        job = self._json("GET", "/v1/parse/jobs/" + quote(job_id, safe=""))
        if job.get("job_id") and job["job_id"] != job_id:
            raise MinerUFatalError("V1 status substituted the submitted job")
        return job

    def output(self, job: dict[str, Any]) -> str | None:
        state = job.get("status")
        if state in {"queued", "running"}:
            return None
        files = job.get("files") or []
        if state == "completed":
            if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict) or files[0].get("status") != "completed" or files[0].get("error"):
                raise MinerUFatalError("V1 completed job must contain one successful file")
            outputs = files[0].get("output_files")
            result = outputs.get("zip") if isinstance(outputs, dict) else None
            file_id = result.get("file_id") if isinstance(result, dict) else None
            if not isinstance(file_id, str) or not file_id:
                raise MinerUFatalError("V1 completed job lacks the full ZIP output")
            return file_id
        if state in {"failed", "canceled", "partial"}:
            problem = job.get("error")
            if not problem and isinstance(files, list):
                problem = next((f.get("error") for f in files if isinstance(f, dict) and f.get("error")), None)
            raise classify_problem({"error": problem or {"code": "invalid_request", "message": f"V1 job ended {state}"}}, 0, token=self.token)
        raise MinerUFatalError(f"unknown V1 job state {state!r}")

    def download_zip(self, file_id: str, target: Path) -> None:
        url = self.base_url + "/v1/files/" + quote(file_id, safe="") + "/content"
        for _ in range(6):
            headers = {"Authorization": f"Bearer {self.token}"} if origin(url) == origin(self.base_url) else {}
            self.session.cookies.clear()
            response = self.session.get(url, headers=headers, timeout=self.request_timeout(), allow_redirects=False, stream=True)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise MinerUFatalError("V1 output redirect missing Location")
                next_url = urljoin(url, location)
                if origin(next_url)[0] != "https" and origin(next_url) != origin(self.base_url):
                    raise MinerUFatalError("V1 output redirect cannot downgrade transport")
                url = next_url
                self.session.cookies.clear()
                continue
            try:
                if response.status_code != 200:
                    raise MinerURetryableError(f"V1 full ZIP download HTTP {response.status_code}")
                content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
                if content_type not in {"application/zip", "application/x-zip-compressed", "application/octet-stream"}:
                    raise MinerUFatalError("V1 output is not ZIP/octet-stream")
                size, head = 0, bytearray()
                with target.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1 << 16):
                        if time.monotonic() >= self.deadline:
                            raise MinerURetryableError("V1 result transfer exceeded wait budget")
                        size += len(chunk)
                        if size > MAX_ZIP_BYTES:
                            raise MinerUFatalError("V1 result ZIP exceeds 128MiB")
                        if len(head) < 4:
                            head.extend(chunk[:4 - len(head)])
                        output.write(chunk)
                if size < 4 or bytes(head[:2]) != b"PK":
                    raise MinerUFatalError("V1 result is not a full ZIP archive")
                try:
                    with zipfile.ZipFile(target) as archive:
                        members = archive.infolist()
                        if len(members) > 10000 or sum(member.file_size for member in members) > 256 << 20 or any(member.file_size > MAX_ZIP_BYTES for member in members):
                            raise MinerUFatalError("V1 result archive exceeds expanded member bounds")
                        if archive.testzip() is not None:
                            raise MinerUFatalError("V1 full ZIP member CRC failed")
                except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                    raise MinerUFatalError("V1 result is a corrupt/unsupported ZIP archive") from exc
                return
            finally:
                response.close()
        raise MinerUFatalError("V1 output exceeded redirect limit")
