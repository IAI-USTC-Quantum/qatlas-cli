"""Actual committed-but-disconnected writes beyond blockapi and fetch."""

from __future__ import annotations

import argparse

import pytest
import requests

from qatlas.client import auth, mineru, paper, upload
from qatlas.parser.mineru_client import BatchFile, MinerUClient
from tests.client.test_write_compatibility_http import (
    PAPER_ID,
    compatibility_server,
    isolated_client as isolated_client,
)

KINDS = ["lease", "lease-release", "pdf-upload", "zip-upload", "mineru-claim",
         "mineru-upload", "cleanup-release", "mineru-task", "mineru-batch"]


def operation(kind, base_url, tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    zip_path = tmp_path / "mineru.zip"
    zip_path.write_bytes(b"PK-fixture")
    args = argparse.Namespace(
        request_timeout=2, id_or_doi=PAPER_ID, arxiv_id=PAPER_ID, claim_id="cid",
        ttl_seconds=None, pdf=str(pdf), zip=str(zip_path), overwrite=False,
        verify="warn", source=None,
    )
    options = {"request_timeout": 2, "verify": True, "headers": {}}
    if kind == "lease":
        return lambda: paper.cmd_mineru_lease(args)
    if kind == "lease-release":
        return lambda: paper.cmd_release_mineru_lease(args)
    if kind == "pdf-upload":
        return lambda: upload.cmd_upload_pdf(args)
    if kind == "zip-upload":
        return lambda: upload.cmd_upload_mineru(args)
    if kind == "mineru-claim":
        return lambda: mineru._claim_one(base_url, PAPER_ID, ttl_seconds=None, **options)
    if kind == "mineru-upload":
        return lambda: mineru._upload_mineru_zip(
            base_url=base_url, arxiv_id=PAPER_ID, zip_path=zip_path,
            overwrite=False, **options,
        )
    if kind == "cleanup-release":
        return lambda: mineru._release_claim(base_url, PAPER_ID, "cid", **options)
    client = MinerUClient("fixture-only", base_url=base_url, timeout=(2, 2))
    if kind == "mineru-task":
        return lambda: client.submit_url_task(url="https://papers.example.org/paper.pdf")
    return lambda: client.submit_url_batch([BatchFile(url="https://papers.example.org/paper.pdf")])


@pytest.mark.parametrize("kind", KINDS)
def test_actual_writes_committed_response_lost_unknown_single_attempt(
    isolated_client, tmp_path, capsys, kind,
):
    with compatibility_server(drop="write") as server:
        isolated_client(server.base_url)
        call = operation(kind, server.base_url, tmp_path)
        if kind == "mineru-claim":
            result, reason = call()
            assert result is None
            assert "UNKNOWN" in reason
        elif kind == "cleanup-release":
            # Best-effort cleanup must still not mask an earlier error.
            original = ValueError("original operation failure")
            with pytest.raises(ValueError) as exc:
                try:
                    raise original
                finally:
                    call()
            assert exc.value is original
        else:
            with pytest.raises(requests.ConnectionError) as exc:
                call()
            assert type(exc.value) is requests.ConnectionError
            assert any("UNKNOWN" in note for note in exc.value.__notes__)
        preflight = kind not in {"cleanup-release", "mineru-task", "mineru-batch"}
        assert len(server.requests) == (2 if preflight else 1)
        if preflight:
            assert server.requests[0]["method"] == "GET"
            assert server.requests[0]["path"] == "/atlas/api/server/info"
        assert server.requests[-1]["method"] == ("DELETE" if "release" in kind else "POST")
        assert server.committed == [PAPER_ID]
    output = capsys.readouterr()
    assert output.out == ""
    assert "UNKNOWN" in output.err
    assert "No automatic retry" in output.err
    assert "was not sent" not in output.err
    assert "Retrying is safe" not in output.err


@pytest.mark.parametrize("kind", KINDS[:6])
def test_actual_business_write_probe_disconnect_is_unsent(
    isolated_client, tmp_path, capsys, kind,
):
    with compatibility_server(drop="probe") as server:
        isolated_client(server.base_url)
        call = operation(kind, server.base_url, tmp_path)
        if kind == "mineru-claim":
            result, reason = call()
            assert result is None
            assert "was not sent" in reason
            assert "UNKNOWN" not in reason
        else:
            with pytest.raises(requests.RequestException, match="was not sent"):
                call()
        assert [(request["method"], request["path"]) for request in server.requests] == [
            ("GET", "/atlas/api/server/info"),
        ]
        assert server.committed == []
    assert "UNKNOWN" not in capsys.readouterr().err


@pytest.mark.parametrize("phase", ["code", "token"])
def test_device_auth_lost_committed_response_unknown_does_not_retry_or_store_token(
    isolated_client, monkeypatch, capsys, phase,
):
    # Only request handling is real; omit real browser actions and poll sleeping.
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)
    with compatibility_server(drop=f"/atlas/api/oauth/device/{phase}") as server:
        isolated_client(server.base_url)
        server.result = {
            "device_code": "fixture-device-code", "user_code": "FIXTURE",
            "verification_uri": "https://server.example.org/device",
            "interval": 1, "expires_in": 60,
        }
        with pytest.raises(auth._DeviceFlowError) as exc:
            auth._device_login(base_url=server.base_url, verify=True,
                               suggested_name="fixture", scopes=[], expires_days=1,
                               timeout=2, open_browser=False)
        assert type(exc.value.__cause__) is requests.ConnectionError
        assert any("UNKNOWN" in note for note in exc.value.__cause__.__notes__)
        paths = ["/atlas/api/oauth/device/code"]
        if phase == "token":
            paths.append("/atlas/api/oauth/device/token")
            assert "UNKNOWN" in str(exc.value)
        assert [(request["method"], request["path"]) for request in server.requests] == [
            ("POST", path) for path in paths
        ]
        assert len(server.committed) == len(paths)
        assert auth.get_stored_token(server.base_url) == ""
    output = capsys.readouterr()
    assert output.out == ""
    assert "UNKNOWN" in output.err and "No automatic retry" in output.err
    assert "retrying" not in output.err


def test_mineru_watch_stops_after_caught_unknown_instead_of_resubmitting(
    isolated_client, monkeypatch, tmp_path, capsys,
):
    from types import SimpleNamespace

    calls = []
    with compatibility_server(drop="write") as server:
        isolated_client(server.base_url)
        config = SimpleNamespace(mineru_api_token="fixture", mineru_api_tokens=["fixture"],
                                 mineru_api_base_url=server.base_url)
        monkeypatch.setattr(mineru.ServerConfig, "from_env", lambda: config)
        monkeypatch.setattr(mineru, "base_url_from_args", lambda args: server.base_url)
        monkeypatch.setattr(mineru, "request_verify", lambda args: True)
        monkeypatch.setattr(mineru, "auth_headers", lambda args: {})
        monkeypatch.setattr(mineru, "_install_signal_handlers", lambda: None)
        monkeypatch.setattr(mineru, "_SHUTDOWN_REQUESTED", False)

        def drain(*args, key_ring, **kwargs):
            calls.append("pass")
            client, _ = key_ring.acquire()
            try:
                client.submit_url_batch([BatchFile(url="https://papers.example.org/paper.pdf")])
            except requests.RequestException:
                # Match _drain_queue_once's existing exception-to-outcome path.
                return mineru._BatchOutcome(processed=0, failures=1, daily_limit_hit=False)
            pytest.fail("fixture should lose the committed response")

        monkeypatch.setattr(mineru, "_drain_queue_once", drain)
        monkeypatch.setattr(mineru, "_sleep_interruptible",
                            lambda _: pytest.fail("UNKNOWN must not continue watching"))
        args = argparse.Namespace(arxiv_id=None, watch=True, watch_interval=1, batch_size=1)
        assert mineru.cmd_mineru(args) == 1
        assert calls == ["pass"]
        assert len(server.requests) == 1 and server.requests[0]["method"] == "POST"
        assert server.committed == [PAPER_ID]
    err = capsys.readouterr().err
    assert "UNKNOWN" in err and "stopping watch" in err
