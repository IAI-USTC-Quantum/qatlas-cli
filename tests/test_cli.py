"""Tests for the top-level QuantumAtlas CLI."""

import importlib.metadata
import runpy
import sys
import tomllib
from pathlib import Path

import pytest

from qatlas import __version__, cli


def test_pyproject_console_script_points_to_top_level_cli():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["qatlas"] == "qatlas.cli:main"


def test_runtime_version_matches_project_metadata():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == importlib.metadata.version("qatlas-cli") == pyproject["project"]["version"]
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    packages = [package for package in lock["package"] if package["name"] == "qatlas-cli"]
    assert len(packages) == 1
    assert packages[0]["version"] == __version__


def test_runtime_version_uses_installed_metadata_even_in_checkout(monkeypatch):
    calls = []

    def installed_version(name):
        calls.append(name)
        return "1.2.3rc1"

    monkeypatch.setattr(importlib.metadata, "version", installed_version)
    source = Path(__file__).resolve().parents[1] / "src/qatlas/__init__.py"
    assert runpy.run_path(str(source))["__version__"] == "1.2.3rc1"
    assert calls == ["qatlas-cli"]


def test_uninstalled_checkout_does_not_invent_a_release_version(monkeypatch):
    def missing_version(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing_version)
    source = Path(__file__).resolve().parents[1] / "src/qatlas/__init__.py"
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        runpy.run_path(str(source))


def test_top_level_help(capsys):
    result = cli.main(["--help"])

    captured = capsys.readouterr()
    client_section, local_section = captured.out.split("  Local workspace commands:")
    assert result == 0
    assert "QuantumAtlas command line" in captured.out
    assert "Client/operator commands" in captured.out
    assert "Local workspace commands" in captured.out
    assert "paper" in client_section
    assert "contrib" in client_section
    assert "parser" not in client_section
    assert "parser" in local_section
    # The help must carry a publicly reachable documentation link.
    assert "https://qatlas.hfnl.app.chenzhaoyun.com/doc/guide/cli.html" in captured.out


def test_readme_documents_uv_tool_install():
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "uv tool install qatlas-cli" in readme
    assert "qatlas --help" in readme


def test_top_level_version(capsys):
    result = cli.main(["--version"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.strip() == f"qatlas {__version__}"


def test_unknown_command_returns_usage_error(capsys):
    result = cli.main(["nope"])

    captured = capsys.readouterr()
    assert result == 2
    assert "unknown command 'nope'" in captured.err
    assert "qatlas --help" in captured.err


def test_search_command_without_plugin_prints_install_hint(capsys, monkeypatch):
    """`qatlas search` with no plugin providing it hints at qatlas-search."""
    from qatlas.client.plugins import registry

    monkeypatch.setattr(registry, "top_level_commands", lambda: {})
    result = cli.main(["search", "quantum teleportation"])

    captured = capsys.readouterr()
    assert result == 2
    assert "qatlas-search" in captured.err
    assert "IAI-USTC-Quantum/qatlas-search" in captured.err


def test_rag_command_without_plugin_prints_install_hint(capsys, monkeypatch):
    """`qatlas rag` with no plugin providing it hints at qatlas-rag."""
    from qatlas.client.plugins import registry

    monkeypatch.setattr(registry, "top_level_commands", lambda: {})
    result = cli.main(["rag", "quantum error correction"])

    captured = capsys.readouterr()
    assert result == 2
    assert "qatlas-rag" in captured.err
    assert "IAI-USTC-Quantum/qatlas-rag" in captured.err


def test_match_command_without_plugin_prints_install_hint(capsys, monkeypatch):
    """`qatlas match` with no plugin providing it hints at qatlas-match."""
    from qatlas.client.plugins import registry

    monkeypatch.setattr(registry, "top_level_commands", lambda: {})
    result = cli.main(["match", "2401.12345"])

    captured = capsys.readouterr()
    assert result == 2
    assert "qatlas-match" in captured.err
    assert "IAI-USTC-Quantum/qatlas-match" in captured.err


def test_dispatches_to_existing_module_cli(monkeypatch):
    calls = []
    original_argv = sys.argv[:]

    def fake_run_module(module_name, run_name=None):
        calls.append((module_name, run_name, sys.argv[:]))

    monkeypatch.setattr(runpy, "run_module", fake_run_module)

    result = cli.main(["parser", "9508027", "--no-pdf"])

    assert result == 0
    assert calls == [
        (
            "qatlas.parser.__main__",
            "__main__",
            ["qatlas parser", "9508027", "--no-pdf"],
        )
    ]
    assert sys.argv == original_argv


def test_dispatch_normalizes_aliases(monkeypatch):
    calls = []

    def fake_run_module(module_name, run_name=None):
        calls.append((module_name, sys.argv[:]))

    monkeypatch.setattr(runpy, "run_module", fake_run_module)

    result = cli.main(["parse", "9508027"])

    assert result == 0
    assert calls == [
        ("qatlas.parser.__main__", ["qatlas parser", "9508027"])
    ]


def test_child_system_exit_code_is_returned(monkeypatch):
    def fake_run_module(module_name, run_name=None):
        raise SystemExit(7)

    monkeypatch.setattr(runpy, "run_module", fake_run_module)

    assert cli.main(["parser", "9508027"]) == 7


