"""Offline V1 producer tests: exact PDF, isolated credentials and full result ZIP."""
from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace
from urllib.parse import urlsplit
import zipfile

import pytest
import requests

from qatlas.client import mineru as cli
from qatlas.client.mineru_v1 import MinerUV1
from qatlas.config import ServerConfig
from qatlas.parser.mineru_client import MinerUFatalError
from tests.client.mocktransport import MockTransport, _MockAdapter, make_response

QA = "https://qa.test"
API = "https://mineru.test/api"
PDF = b"%PDF-1.7 exact frozen source"
SHA = hashlib.sha256(PDF).hexdigest()
SOURCE = "src-immutable"
PAPER = "qa_01h5e0aaaabbbbccccddddeeffff"
QTOKEN = "q-atlas-secret"
VTOKEN = "vendor-secret"


def archive_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("run/layout.json", b'{ "schema":"docvortex.middle", "schema_version":"2.0", "pages":[] }\n')
        archive.writestr("run/markdown.md", b"# exact original markdown\n")
        archive.writestr("run/structured_content.json", b'[ { "producer":"view" } ]\n')
        archive.writestr("unknown/name..json", b'{ "opaque":"retained byte-exactly" }\n')
    return output.getvalue()


ZIP = archive_bytes()


@pytest.fixture
def environment(monkeypatch):
    transport = MockTransport()
    native_session = requests.Session

    def session():
        value = native_session()
        value.trust_env = False
        adapter = _MockAdapter(transport)
        value.mount("http://", adapter)
        value.mount("https://", adapter)
        return value

    monkeypatch.setattr(requests, "Session", session)
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: session().get(url, **kwargs))
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: session().post(url, **kwargs))
    monkeypatch.setattr(requests, "delete", lambda url, **kwargs: session().delete(url, **kwargs))
    monkeypatch.setattr(cli, "check_response_version", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "check_server_before_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "client_version_headers", lambda: {})
    monkeypatch.setattr(cli, "_SHUTDOWN_REQUESTED", False)
    clock = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    released = []
    monkeypatch.setattr(cli, "_release_claim", lambda base, identity, claim_id, **kwargs: released.append((identity, claim_id)))
    claim = {"claim_id": "lease-1", "paper_id": PAPER, "source_id": SOURCE,
             "pdf_url": f"/api/papers/{PAPER}/pdf?source_id={SOURCE}",
             "pdf_requires_auth": True, "pdf_sha256": SHA}
    monkeypatch.setattr(cli, "_claim_one", lambda *args, **kwargs: (dict(claim), None))
    config = SimpleNamespace(mineru_api_protocol="v1", mineru_tier="standard", mineru_api_token=VTOKEN,
        mineru_api_tokens=[VTOKEN], mineru_api_base_url=API, mineru_timeout=30,
        mineru_poll_interval=1, mineru_is_ocr=False, mineru_model_version="legacy-model-not-a-tier")
    args = cli.build_parser().parse_args(["0811.3171v2"])
    return SimpleNamespace(transport=transport, session=session, claim=claim, config=config,
        args=args, released=released, clock=clock, posted=[], upload_count=0, poll_override=None)


def wire(env, *, pdf=PDF, result=ZIP, queued=True, redirects=False):
    transport = env.transport

    def source(req):
        assert urlsplit(req.url).netloc == "qa.test"
        assert req.headers.get("Authorization") == "Bearer " + QTOKEN
        assert VTOKEN not in str(req.headers)
        return make_response(200, content=pdf, headers={"Content-Type": "application/pdf",
            "X-QAtlas-Source-Id": SOURCE, "X-QAtlas-PDF-SHA256": SHA})

    transport.add("GET", f"/api/papers/{PAPER}/pdf", source)

    def create(req):
        assert urlsplit(req.url).netloc == "mineru.test"
        assert req.headers["Authorization"] == "Bearer " + VTOKEN
        assert "Cookie" not in req.headers
        body = json.loads(req.body)
        assert body["sha256sum"] == SHA and body["bytes"] == len(PDF)
        assert body["purpose"] == "parse" and "url" not in body
        env.upload_count += 1
        number = env.upload_count

        def put(req):
            assert urlsplit(req.url).netloc == "blob.test"
            assert "Authorization" not in req.headers and "Cookie" not in req.headers
            assert req.body.read() == PDF
            return make_response(200)

        transport.add("PUT", f"/put/{number}", put)
        transport.add("POST", f"/api/v1/uploads/u{number}/complete", lambda req: make_response(200, json_body={"file": {"id": f"f{number}"}}))
        return make_response(200, json_body={"id": f"u{number}", "status": "created",
            "upload_url": f"https://blob.test/put/{number}", "upload_headers": {"Content-Type": "application/pdf"}},
            headers={"Set-Cookie": "vendor_cookie=must_not_leak; Path=/"})

    transport.add("POST", "/api/v1/uploads", create)

    def complete_job(number):
        return {"job_id": f"j{number}", "status": "completed", "files": [{"status": "completed", "output_files": {"zip": {"file_id": f"z{number}"}}}]}

    def submit(req):
        assert req.headers["Authorization"] == "Bearer " + VTOKEN
        body = json.loads(req.body)
        assert body == {"files": [{"source": {"type": "file_id", "file_id": f"f{env.upload_count}"}}],
            "tier": "standard", "ocr_mode": "auto", "output_formats": ["zip"]}
        number = env.upload_count
        transport.add("GET", f"/api/v1/parse/jobs/j{number}", lambda req: make_response(200, json_body=env.poll_override or complete_job(number)))
        if redirects:
            transport.add("GET", f"/api/v1/files/z{number}/content", lambda req: make_response(302, headers={"Location": f"https://result.test/z{number}"}))

            def result_download(req):
                assert "Authorization" not in req.headers and "Cookie" not in req.headers
                return make_response(200, content=result, headers={"Content-Type": "application/zip"})

            transport.add("GET", f"/z{number}", result_download)
        else:
            transport.add("GET", f"/api/v1/files/z{number}/content", lambda req: make_response(200, content=result, headers={"Content-Type": "application/zip"}))
        return make_response(200, json_body={"job_id": f"j{number}", "status": "queued"} if queued else complete_job(number))

    transport.add("POST", "/api/v1/parse/jobs", submit)

    def push(req):
        assert urlsplit(req.url).netloc == "qa.test"
        assert req.headers["Authorization"] == "Bearer " + QTOKEN
        query = MockTransport.query_of(req)
        assert query["source_id"] == [SOURCE] and query["pdf_sha256"] == [SHA]
        assert query["tier"] == ["standard"] and query["expected_sha256"] == [hashlib.sha256(ZIP).hexdigest()]
        assert ZIP in req.body
        assert VTOKEN.encode() not in req.body
        env.posted.append(req)
        return make_response(201, json_body={"paper_id": PAPER, "source_id": SOURCE, "revision": "new-immutable-revision"})

    transport.add("POST", "/api/papers/0811.3171v2/upload-mineru", push)
    transport.add("POST", "/api/papers/0811.3171v3/upload-mineru", push)


def process(env):
    return cli._process_one(env.args, QA, env.config, "0811.3171v2", True, {"Authorization": "Bearer " + QTOKEN})


def test_config_defaults_explicit_v1_standard_not_legacy_model():
    assert ServerConfig.model_fields["mineru_api_protocol"].default == "v1"
    assert ServerConfig.model_fields["mineru_tier"].default == "standard"
    assert cli.build_parser().parse_args(["--tier", "advanced"]).tier == "advanced"


def test_single_auth_exact_pdf_v1_full_zip_no_credential_crossing(environment, capsys):
    wire(environment, redirects=True)
    assert process(environment) == 0
    assert len(environment.posted) == 1
    assert json.loads(capsys.readouterr().out)["revision"] == "new-immutable-revision"
    assert environment.released == [("0811.3171v2", "lease-1")]
    for req in environment.transport.requests_seen:
        host = urlsplit(req.url).netloc
        auth = req.headers.get("Authorization", "")
        assert (QTOKEN in auth) == (host == "qa.test")
        assert VTOKEN not in auth or host == "mineru.test"


def test_queue_uses_verified_sources_for_each_v1_job(environment):
    wire(environment)
    environment.transport.add("GET", "/api/papers/needs-mineru", lambda req: make_response(200, json_body={"papers": [{"arxiv_id": "0811.3171v2"}, {"arxiv_id": "0811.3171v3"}]}))
    outcome = cli._drain_queue_once(environment.args, QA, environment.config, True, {"Authorization": "Bearer " + QTOKEN})
    assert outcome.processed == 2 and outcome.failures == 0 and not outcome.daily_limit_hit
    assert len(environment.posted) == 2
    assert len(environment.released) == 2


@pytest.mark.parametrize("locator", ["https://attacker.test/api/papers/x/pdf", "//attacker.test/pdf", "https://user:password@qa.test/api/papers/x/pdf"])
def test_authenticated_locator_cannot_exfiltrate_qatlas_auth(environment, locator):
    environment.claim["pdf_url"] = locator
    assert process(environment) == 1
    assert not environment.transport.requests_seen
    assert environment.released


def test_pdf_hash_mismatch_never_calls_provider(environment, capsys):
    wire(environment, pdf=b"%PDF-tampered")
    assert process(environment) == 1
    assert all(urlsplit(req.url).netloc == "qa.test" for req in environment.transport.requests_seen)
    assert environment.released
    assert capsys.readouterr().out == ""


def test_source_sha_is_mandatory_no_legacy_unverified_default(environment):
    environment.claim["pdf_sha256"] = ""
    assert process(environment) == 1
    assert not environment.transport.requests_seen


def test_authenticated_source_redirect_is_not_followed(environment):
    environment.transport.add("GET", f"/api/papers/{PAPER}/pdf", lambda req: make_response(302, headers={"Location": "https://attacker.test/source.pdf"}))
    assert process(environment) == 1
    assert len(environment.transport.requests_seen) == 1


def test_no_push_retains_byte_exact_full_zip_all_json(environment, capsys):
    wire(environment)
    environment.args.no_push = True
    assert process(environment) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    path = captured.err.split("complete original ZIP retained at ", 1)[1].splitlines()[0]
    from pathlib import Path
    retained = Path(path)
    assert retained.read_bytes() == ZIP
    with zipfile.ZipFile(retained) as archive:
        assert archive.read("unknown/name..json") == b'{ "opaque":"retained byte-exactly" }\n'
    assert not environment.posted


def test_completed_without_zip_output_is_not_fake_success(environment, capsys):
    wire(environment)
    environment.poll_override = {"job_id": "j1", "status": "completed", "files": [{"status": "completed", "output_files": {"markdown": {"file_id": "md-only"}}}]}
    assert process(environment) == 1
    assert not environment.posted
    assert "full ZIP" in capsys.readouterr().err


def test_pending_job_wait_is_bounded_and_lease_released(environment):
    wire(environment)
    environment.args.max_wait = 2
    environment.poll_override = {"job_id": "j1", "status": "running"}
    assert process(environment) == 1
    assert environment.clock[0] <= 2
    assert environment.released and not environment.posted


def test_daily_limit_stops_single_and_releases_source(environment):
    wire(environment)
    environment.transport.add("POST", "/api/v1/parse/jobs", lambda req: make_response(429, json_body={"error": {"code": "daily_limit_exceeded", "message": "quota exhausted"}}))
    assert process(environment) == cli.EXIT_DAILY_LIMIT
    assert environment.released and not environment.posted


def test_unknown_submission_is_not_replayed(environment, capsys):
    wire(environment)

    def lost(req):
        raise requests.ConnectionError("committed job response lost")

    environment.transport.add("POST", "/api/v1/parse/jobs", lost)
    assert process(environment) == 1
    assert len([req for req in environment.transport.requests_seen if req.url.endswith("/parse/jobs")]) == 1
    assert "UNKNOWN" in capsys.readouterr().err


@pytest.mark.parametrize("headers", [{"Authorization": "Bearer " + VTOKEN}, {"Cookie": "secret=1"}, {"X-Api-Key": VTOKEN}])
def test_upload_ticket_cannot_forward_vendor_credentials(environment, tmp_path, headers):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(PDF)
    environment.transport.add("POST", "/api/v1/uploads", lambda req: make_response(200, json_body={"id": "u1", "status": "created", "upload_url": "https://blob.test/put/1", "upload_headers": headers}))
    client = MinerUV1(VTOKEN, base_url=API, timeout=5, deadline=30)
    with pytest.raises(MinerUFatalError):
        client.upload(pdf, SHA)
    assert len(environment.transport.requests_seen) == 1
    client.close()


def test_corrupt_zip_cannot_be_published(environment):
    wire(environment, result=b"PK-not-an-archive")
    assert process(environment) == 1
    assert not environment.posted
