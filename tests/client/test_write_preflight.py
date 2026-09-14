"""Offline regression tests: version refusal must precede every business write."""

from __future__ import annotations

import argparse
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from qatlas import cli
from qatlas.client import _common, downloader, mineru, paper, pluginsupport, upload
from qatlas.client.plugins.base import CliContext

BASE = "https://server.test/prefix"
CLIENT = "0.34.1rc1"
NEWER = "0.35.0-rc.1"
HEADERS = {"Authorization": "Bearer test-only", "X-Qatlas-Client-Version": CLIENT}
TIMEOUT = 17.0


def response(status=200, version="0.34.0", body=None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body if body is not None else {
        "claim_id": "cid", "enqueued": 1, "items": [],
    }).encode()
    if version is not None:
        resp.headers["X-Qatlas-Server-Version"] = version
    return resp


@pytest.fixture(autouse=True)
def isolated_client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    (config / "qatlas").mkdir(parents=True)
    (config / "qatlas" / "config.yaml").write_text(
        f"server_url: {BASE}\ninsecure: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(_common, "_CLIENT_VERSION", CLIENT)
    monkeypatch.setattr(_common, "_WARNED_VERSION_MISMATCH", set())
    monkeypatch.setattr(_common, "resolve_token", lambda args: "test-only")
    monkeypatch.setattr(pluginsupport, "_WARNED_INSECURE", False)


@pytest.fixture(params=[
    "plugin-post", "plugin-put", "plugin-patch", "plugin-delete",
    "paper-lease", "paper-release", "pdf-upload", "zip-upload",
    "mineru-claim", "mineru-upload", "fetch",
])
def write_case(request, tmp_path):
    """Real entry points, never mocked version guards or business wrappers."""
    kind = request.param
    case = SimpleNamespace(kind=kind, status=200, method="POST", kwargs={})
    args = argparse.Namespace(
        id_or_doi="2501.00010v1", arxiv_id="10.1/a?b#c", claim_id="cid",
        ttl_seconds=600, request_timeout=TIMEOUT, overwrite=True,
        verify="strict", source="test", items=["10.1/a"], file=None, json=True,
    )
    mineru_options = dict(request_timeout=TIMEOUT, verify=False, headers=HEADERS)
    if kind.startswith("plugin-"):
        case.method = kind.removeprefix("plugin-").upper()
        ctx = CliContext(server_base_url=BASE + "/", token="test-only",
                         request_timeout=99, insecure=True)
        case.path = "/api/example"
        case.kwargs = {"json": {"secret": "payload"}, "params": {"query": "data"}}
        case.call = lambda: pluginsupport.server_request(
            ctx, case.method, case.path, write=True, timeout=TIMEOUT, **case.kwargs
        )
    elif kind == "paper-lease":
        case.status = 201
        case.path = "/api/v1/papers/2501.00010v1/mineru-lease"
        case.kwargs = {"params": {"ttl_seconds": 600}}
        case.call = lambda: paper.cmd_mineru_lease(args)
    elif kind == "paper-release":
        case.method, case.status = "DELETE", 204
        case.path = "/api/v1/papers/2501.00010v1/mineru-lease/cid"
        case.call = lambda: paper.cmd_release_mineru_lease(args)
    elif kind in {"pdf-upload", "zip-upload"}:
        is_pdf = kind == "pdf-upload"
        content = b"%PDF-test" if is_pdf else b"PK-test"
        path = tmp_path / ("paper.pdf" if is_pdf else "paper.zip")
        path.write_bytes(content)
        args.pdf = args.zip = str(path)
        case.path = "/api/papers/10.1%2Fa%3Fb%23c/upload-" + ("pdf" if is_pdf else "mineru")
        params = {"expected_sha256": hashlib.sha256(content).hexdigest(),
                  "overwrite": "true", "verify": "strict"}
        if not is_pdf:
            params["source"] = "test"
        case.kwargs = {"params": params}
        case.file_content = content
        case.file_field = "pdf" if is_pdf else "mineru_zip"
        case.call = lambda: (upload.cmd_upload_pdf(args) if is_pdf
                             else upload.cmd_upload_mineru(args))
    elif kind == "mineru-claim":
        case.status = 201
        case.path = "/api/v1/papers/2501.00010v1/mineru-lease"
        case.kwargs = {"params": {"ttl_seconds": 600}}
        case.call = lambda: mineru._claim_one(
            BASE, "2501.00010v1", ttl_seconds=600, **mineru_options
        )
    elif kind == "mineru-upload":
        path = tmp_path / "mineru.zip"
        path.write_bytes(b"PK-mineru")
        case.path = "/api/papers/2501.00010v1/upload-mineru"
        case.kwargs = {"params": {"source": "mineru", "overwrite": "true",
                                  "pdf_sha256": "abc123"}}
        case.file_content, case.file_field = b"PK-mineru", "mineru_zip"
        case.call = lambda: mineru._upload_mineru_zip(
            base_url=BASE, arxiv_id="2501.00010v1", zip_path=path,
            overwrite=True, pdf_sha256="abc123", **mineru_options
        )
    else:
        case.path = "/api/downloader/fetch"
        case.kwargs = {"json": {"items": ["10.1/a"]}}
        case.call = lambda: downloader.cmd_fetch(args)
    return case


def mock_http(monkeypatch, case, probe_response=None, probe_error=None,
              write_version="0.34.0"):
    events = []
    handles = []
    result_response = response(case.status, write_version)

    def probe(url, **kwargs):
        events.append("probe")
        assert url == BASE + "/api/server/info"
        assert kwargs == {"headers": HEADERS, "timeout": TIMEOUT,
                          "verify": False, "allow_redirects": False}
        if probe_error is not None:
            raise probe_error
        return probe_response if probe_response is not None else response()

    def mutate(*args, **kwargs):
        events.append("mutation")
        assert events[-2] == "probe"
        if case.kind.startswith("plugin-"):
            method, url = args
            assert method == case.method
        else:
            (url,) = args
        assert url == BASE + case.path
        assert kwargs["headers"] == HEADERS
        assert kwargs["timeout"] == TIMEOUT
        assert kwargs["verify"] is False
        for name, value in case.kwargs.items():
            assert kwargs[name] == value
        if hasattr(case, "file_content"):
            (_, fh, _) = kwargs["files"][case.file_field]
            assert fh.read() == case.file_content
            handles.append(fh)
        return result_response

    get = Mock(side_effect=probe)
    writes = {name: Mock(side_effect=mutate) for name in ("post", "delete", "request")}
    monkeypatch.setattr(requests, "get", get)
    for name, mock in writes.items():
        monkeypatch.setattr(requests, name, mock)
    return SimpleNamespace(get=get, writes=writes, events=events, handles=handles,
                           response=result_response)


def assert_success(case, result, http):
    expected_method = "request" if case.kind.startswith("plugin-") else case.method.lower()
    http.writes[expected_method].assert_called_once()
    for name, mock in http.writes.items():
        if name != expected_method:
            mock.assert_not_called()
    if case.kind.startswith("plugin-"):
        assert result is http.response
    elif case.kind == "mineru-claim":
        assert result == (http.response.json(), None)
    elif case.kind == "mineru-upload":
        assert result == (True, http.response.json())
    else:
        assert result == 0
    assert all(fh.closed for fh in http.handles)


def test_newer_rc_server_never_receives_business_write(write_case, monkeypatch, capsys):
    http = mock_http(monkeypatch, write_case, response(version=NEWER))
    with pytest.raises(SystemExit) as exc:
        write_case.call()
    assert exc.value.code == 4
    assert http.events == ["probe"]
    http.get.assert_called_once()
    for mock in http.writes.values():
        mock.assert_not_called()
    err = capsys.readouterr().err
    assert NEWER in err and CLIENT in err and "Write request was not sent" in err


@pytest.mark.parametrize("status,version", [
    (200, "0.34.9"), (200, "0.33.0"), (200, None), (200, "dev-build"), (404, None),
])
def test_compatible_and_legacy_writes_keep_order_and_parameters(
    write_case, monkeypatch, status, version, capsys,
):
    http = mock_http(monkeypatch, write_case, response(status, version))
    assert_success(write_case, write_case.call(), http)
    assert http.events == ["probe", "mutation"]
    assert sum(mock.call_count for mock in http.writes.values()) == 1
    err = capsys.readouterr().err
    assert ("older than client" in err) == (version == "0.33.0")


@pytest.mark.parametrize("failure", [401, 403, 429, 500, 503, 302, "timeout", "network"])
def test_probe_failure_never_sends_write(write_case, monkeypatch, failure, capsys):
    error = {"timeout": requests.Timeout("timed out"),
             "network": requests.ConnectionError("offline")}.get(failure)
    probe = response(failure) if isinstance(failure, int) else None
    http = mock_http(monkeypatch, write_case, probe, error)
    result = _common.run_with_request_errors(write_case.call)
    if write_case.kind == "mineru-claim":
        assert result[0] is None and "write request was not sent" in result[1]
    else:
        assert result == 1
        assert "write request was not sent" in capsys.readouterr().err
    assert http.events == ["probe"]
    for mock in http.writes.values():
        mock.assert_not_called()


def test_write_response_version_drift_warns_without_refusal_or_retry(
    write_case, monkeypatch, capsys,
):
    # A prior read warning must not suppress the distinct already-sent warning.
    _common.check_response_version(response(version=NEWER), write=False)
    capsys.readouterr()
    http = mock_http(monkeypatch, write_case, write_version=NEWER)
    assert_success(write_case, write_case.call(), http)
    assert http.events == ["probe", "mutation"]
    err = capsys.readouterr().err
    assert "WARNING" in err and "already sent" in err
    assert "ERROR" not in err and "was not sent" not in err


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_read_operations_do_not_probe_even_when_using_post(monkeypatch, method, capsys):
    get = Mock(side_effect=AssertionError("read must not probe"))
    request = Mock(return_value=response(version=NEWER))
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "request", request)
    ctx = CliContext(server_base_url=BASE, request_timeout=TIMEOUT)
    assert pluginsupport.server_request(ctx, method, "/api/search", json={"q": "x"}).ok
    get.assert_not_called()
    request.assert_called_once()
    assert "WARNING" in capsys.readouterr().err


def test_probe_uses_context_defaults_without_token(monkeypatch):
    get = Mock(return_value=response())
    mutation = Mock(return_value=response())
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "request", mutation)
    ctx = CliContext(server_base_url=BASE, request_timeout=31.0)
    pluginsupport.server_request(ctx, "POST", "/x", write=True)
    options = {"headers": {"X-Qatlas-Client-Version": CLIENT},
               "timeout": 31.0, "verify": True}
    get.assert_called_once_with(BASE + "/api/server/info", **options, allow_redirects=False)
    mutation.assert_called_once_with("POST", BASE + "/x", json=None, params=None, **options)


def test_no_probe_cache_between_writes(monkeypatch):
    get = Mock(side_effect=[response(), response(version=NEWER)])
    mutation = Mock(return_value=response())
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "request", mutation)
    ctx = CliContext(server_base_url=BASE)
    assert pluginsupport.server_request(ctx, "POST", "/api/x", write=True).ok
    with pytest.raises(SystemExit) as exc:
        pluginsupport.server_request(ctx, "POST", "/api/x", write=True)
    assert exc.value.code == 4 and get.call_count == 2
    mutation.assert_called_once()


def test_probe_404_with_known_newer_version_still_refuses(monkeypatch):
    monkeypatch.setattr(requests, "get", Mock(return_value=response(404, NEWER)))
    mutation = Mock()
    monkeypatch.setattr(requests, "request", mutation)
    with pytest.raises(SystemExit) as exc:
        pluginsupport.server_request(CliContext(server_base_url=BASE), "DELETE", "/x", write=True)
    assert exc.value.code == 4
    mutation.assert_not_called()


def test_http_error_after_write_is_returned_not_version_refused(monkeypatch, capsys):
    monkeypatch.setattr(requests, "get", Mock(return_value=response()))
    failure = response(503, NEWER, {"detail": "deployment changed"})
    mutation = Mock(return_value=failure)
    monkeypatch.setattr(requests, "request", mutation)
    assert pluginsupport.server_request(
        CliContext(server_base_url=BASE), "POST", "/x", write=True
    ) is failure
    mutation.assert_called_once()
    assert "already sent" in capsys.readouterr().err


def test_mutation_transport_failure_is_not_retried(monkeypatch):
    get = Mock(return_value=response())
    timeout = requests.Timeout("write response lost")
    mutation = Mock(side_effect=timeout)
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "request", mutation)
    with pytest.raises(requests.Timeout) as exc:
        pluginsupport.server_request(CliContext(server_base_url=BASE), "POST", "/x", write=True)
    assert exc.value is timeout
    get.assert_called_once()
    mutation.assert_called_once()


def test_claim_legacy_route_fallback_only_probes_once(monkeypatch, capsys):
    get = Mock(return_value=response())
    post = Mock(side_effect=[response(404, NEWER), response(201, NEWER)])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    claim, skip = mineru._claim_one(BASE, "2501.00010v1", request_timeout=TIMEOUT,
                                   verify=False, headers=HEADERS, ttl_seconds=600)
    assert claim["claim_id"] == "cid" and skip is None
    get.assert_called_once()
    assert [call.args[0] for call in post.call_args_list] == [
        BASE + "/api/v1/papers/2501.00010v1/mineru-lease",
        BASE + "/api/papers/2501.00010v1/mineru-claim",
    ]
    assert "already sent" in capsys.readouterr().err


def test_cleanup_release_never_gains_preflight_dependency(monkeypatch, capsys):
    get = Mock(side_effect=requests.ConnectionError("probe is unavailable"))
    delete = Mock(side_effect=[response(404, NEWER), response(204, NEWER)])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "delete", delete)
    original = ValueError("original operation failure")
    with pytest.raises(ValueError) as exc:
        try:
            raise original
        finally:
            mineru._release_claim(BASE, "2501.00010v1", "cid",
                                  request_timeout=TIMEOUT, verify=False, headers=HEADERS)
    assert exc.value is original
    get.assert_not_called()
    assert [call.args[0] for call in delete.call_args_list] == [
        BASE + "/api/v1/papers/2501.00010v1/mineru-lease/cid",
        BASE + "/api/papers/2501.00010v1/mineru-claim/cid",
    ]
    assert "already sent" in capsys.readouterr().err


def test_upload_preflight_refusal_releases_acquired_lease(monkeypatch, tmp_path):
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"PK-test")
    args = argparse.Namespace(request_timeout=TIMEOUT, ttl_seconds=600,
                              no_cache=False, no_push=False, overwrite=False)
    get = Mock(side_effect=[response(), response(version=NEWER)])
    post = Mock(return_value=response(201, body={
        "claim_id": "cid", "pdf_url": "https://arxiv.org/pdf/2501.00010v1",
    }))
    delete = Mock(return_value=response(204, NEWER))
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "delete", delete)
    monkeypatch.setattr(mineru, "_run_mineru_to_zip", lambda **kwargs: zip_path)
    refused = []

    def track_refusal(*args, **kwargs):
        try:
            return _common.check_server_before_write(*args, **kwargs)
        except SystemExit as exc:
            refused.append(exc)
            raise

    monkeypatch.setattr(mineru, "check_server_before_write", track_refusal)
    with pytest.raises(SystemExit) as exc:
        mineru._process_one(args, BASE, None, "2501.00010v1", False, HEADERS)
    assert exc.value.code == 4 and exc.value is refused[0]
    assert get.call_count == 2
    # The only POST was the lease acquisition, never the upload.
    post.assert_called_once()
    assert post.call_args.args[0] == BASE + "/api/v1/papers/2501.00010v1/mineru-lease"
    delete.assert_called_once_with(
        BASE + "/api/v1/papers/2501.00010v1/mineru-lease/cid",
        headers=HEADERS, timeout=TIMEOUT, verify=False,
    )


@pytest.mark.parametrize("command", [
    ["paper", "mineru-lease", "2501.00010v1"],
    ["paper", "mineru-lease", "release", "2501.00010v1", "cid"],
    ["paper", "fetch", "10.1/a"],
])
def test_actual_cli_write_command_refuses_before_post_or_delete(monkeypatch, command):
    get = Mock(return_value=response(version=NEWER))
    post, delete = Mock(), Mock()
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "delete", delete)
    assert cli.main([*command, "--request-timeout", str(TIMEOUT)]) == 4
    get.assert_called_once_with(BASE + "/api/server/info", headers=HEADERS,
                                timeout=TIMEOUT, verify=False, allow_redirects=False)
    post.assert_not_called()
    delete.assert_not_called()


@pytest.mark.parametrize("version,expected", [
    (CLIENT, (0, 34)), (NEWER, (0, 35)), ("0.34.0+build", (0, 34)),
    ("dev-build", None), ("", None),
])
def test_existing_parser_accepts_pep440_and_go_rc_versions(version, expected):
    assert _common._parse_semver(version) == expected
