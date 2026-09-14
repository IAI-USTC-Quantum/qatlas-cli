"""Offline fixtures for version governance and publication failure boundaries."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

from packaging.version import Version
import pytest
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metadata = load_script("release_metadata")
preflight = load_script("pypi_preflight")


def project_fixture(root, version="1.2.3", changelog=None):
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "qatlas-cli"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "uv.lock").write_text(
        f'[[package]]\nname = "qatlas-cli"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(
        changelog if changelog is not None else f"# Changelog\n\n## v{version} (2026-09-14)\n\n- Fixed something.\n",
        encoding="utf-8",
    )


def test_current_repository_has_coherent_release_metadata():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version, prerelease, notes = metadata.validate_release(ROOT, "v" + project["version"])
    assert version == project["version"]
    assert prerelease == Version(project["version"]).is_prerelease
    assert notes.strip()


@pytest.mark.parametrize("version,prerelease", [
    ("1.2.3", False), ("1.2.3a1", True), ("1.2.3b2", True),
    ("1.2.3rc1", True), ("1.2.3.dev1", True), ("1.2.3.post1", False),
])
def test_canonical_versions_and_prerelease_detection(tmp_path, version, prerelease):
    project_fixture(tmp_path, version)
    assert metadata.validate_release(tmp_path, "v" + version) == (
        version, prerelease, "- Fixed something.\n"
    )


@pytest.mark.parametrize("version", ["not-a-version", "v1.2.3", "1.2.3-rc.1", "01.2.3", "1!1.2.3", "0!1.2.3", "1.2.3+local"])
def test_invalid_or_noncanonical_versions_fail(tmp_path, version):
    project_fixture(tmp_path, version)
    with pytest.raises(ValueError, match="PEP 440"):
        metadata.validate_release(tmp_path, "v" + version)


def test_excessive_tag_length_fails(tmp_path):
    version = "1." * 64 + "1"
    project_fixture(tmp_path, version)
    with pytest.raises(ValueError, match="128"):
        metadata.validate_release(tmp_path, "v" + version)


@pytest.mark.parametrize("tag", ["1.2.3", "master", "v1.2.30", "v1.2.3rc1"])
def test_tag_must_exactly_match_project_version(tmp_path, tag):
    project_fixture(tmp_path)
    with pytest.raises(ValueError, match="tag != project"):
        metadata.validate_release(tmp_path, tag)


@pytest.mark.parametrize("heading", ["## v1.2.30", "## v1.2.3rc1", "## v1x2x3", "## v1.2.3 undocumented suffix"])
def test_changelog_does_not_accept_substring_or_regex_matches(tmp_path, heading):
    project_fixture(tmp_path, changelog=f"{heading}\n\n- Wrong release.\n")
    with pytest.raises(ValueError, match="exactly one"):
        metadata.validate_release(tmp_path, "v1.2.3")


@pytest.mark.parametrize("changelog", [
    "## v1.2.3\n\n- First\n\n## v1.2.3\n\n- Second\n",
    "## v1.2.3\n\n- First\n\n## 1.2.3 (2026-09-14)\n\n- Second\n",
])
def test_duplicate_headings_fail(tmp_path, changelog):
    project_fixture(tmp_path, changelog=changelog)
    with pytest.raises(ValueError, match="exactly one"):
        metadata.validate_release(tmp_path, "v1.2.3")


def test_empty_notes_do_not_fall_back_to_another_release(tmp_path):
    project_fixture(tmp_path, changelog="## v1.2.3\n\n## v1.2.2\n\n- Old notes\n")
    with pytest.raises(ValueError, match="Empty release notes"):
        metadata.validate_release(tmp_path, "v1.2.3")


def test_notes_only_include_selected_section(tmp_path):
    project_fixture(tmp_path, changelog="## Unreleased\n\n- Future\n\n## v1.2.3 (2026-09-14)\n\n### Fix\n\n- Current\n\n## v1.2.2\n\n- Old\n")
    assert metadata.validate_release(tmp_path, "v1.2.3")[2] == "### Fix\n\n- Current\n"


def test_optional_version_file_is_checked(tmp_path):
    project_fixture(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="VERSION drift"):
        metadata.validate_release(tmp_path, "v1.2.3")


@pytest.mark.parametrize("packages", [
    '[[package]]\nname = "other"\nversion = "1.2.3"\n',
    '[[package]]\nname = "qatlas-cli"\nversion = "1.2.2"\n',
    '[[package]]\nname = "qatlas-cli"\nversion = "1.2.3"\n' * 2,
])
def test_lock_project_drift_fails(tmp_path, packages):
    project_fixture(tmp_path)
    (tmp_path / "uv.lock").write_text(packages, encoding="utf-8")
    with pytest.raises(ValueError, match="uv.lock"):
        metadata.validate_release(tmp_path, "v1.2.3")


def test_validator_cli_writes_notes_and_outputs_only_after_validation(tmp_path, monkeypatch):
    project_fixture(tmp_path, "1.2.3.dev1")
    notes = tmp_path / "build/notes.md"
    outputs = tmp_path / "outputs"
    monkeypatch.setattr(metadata, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setattr("sys.argv", ["metadata", "--tag", "master", "--notes", str(notes)])
    assert metadata.main() == 1
    assert not notes.exists() and not outputs.exists()
    monkeypatch.setattr("sys.argv", ["metadata", "--tag", "v1.2.3.dev1", "--notes", str(notes)])
    assert metadata.main() == 0
    assert notes.read_text(encoding="utf-8") == "- Fixed something.\n"
    assert outputs.read_text(encoding="utf-8") == "version=1.2.3.dev1\nprerelease=true\n"


def fake_pypi(status, payload):
    def get(url, **kwargs):
        assert url == "https://pypi.org/pypi/qatlas-cli/json"
        assert kwargs == {"timeout": 30, "allow_redirects": False}
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        return response
    return SimpleNamespace(get=get)


@pytest.mark.parametrize("files", [[], [{"filename": "qatlas_cli-1.2.3-py3-none-any.whl"}]])
def test_any_recorded_pypi_version_stops_rebuild(files):
    session = fake_pypi(200, {"info": {"name": "qatlas-cli"}, "releases": {"1.2.3": files}})
    with pytest.raises(ValueError, match="do not rebuild"):
        preflight.ensure_unpublished(session, "qatlas-cli", "1.2.3")


def test_new_version_and_first_project_may_proceed():
    preflight.ensure_unpublished(fake_pypi(200, {"info": {"name": "qatlas-cli"}, "releases": {"1.2.2": []}}), "qatlas-cli", "1.2.3")
    preflight.ensure_unpublished(fake_pypi(404, {"message": "Not Found"}), "qatlas-cli", "1.2.3")


@pytest.mark.parametrize("status,payload", [
    (401, {}), (403, {}), (429, {}), (500, {}), (302, {}), (404, {"message": "Proxy error"}),
    (200, {}), (200, {"releases": []}), (200, {"info": {"name": "wrong-project"}, "releases": {}}),
])
def test_unknown_pypi_state_fails_closed(status, payload):
    with pytest.raises(ValueError):
        preflight.ensure_unpublished(fake_pypi(status, payload), "qatlas-cli", "1.2.3")


def test_pypi_network_failure_is_not_absence():
    def unavailable(*args, **kwargs):
        raise requests.ConnectionError("fixture unavailable")
    with pytest.raises(requests.ConnectionError):
        preflight.ensure_unpublished(SimpleNamespace(get=unavailable), "qatlas-cli", "1.2.3")


def test_commitizen_uses_locked_tools_and_preserves_existing_policy():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    cz = config["tool"]["commitizen"]
    assert cz["version_provider"] == "uv"
    assert cz["tag_format"] == "v$version"
    assert cz["annotated_tag"] and cz["update_changelog_on_bump"] and cz["major_version_zero"]
    assert cz["pre_bump_hooks"] == [
        "uv run --extra dev ruff check .", "uv run --extra dev python -m pytest",
    ]
    assert "version_files" not in cz and not (ROOT / "VERSION").exists()
    dev = config["project"]["optional-dependencies"]["dev"]
    assert "commitizen>=4.16.4,<5" in dev
    for name in ("ruff", "pytest", "packaging"):
        assert any(requirement.startswith(name + ">=") for requirement in dev)
    assert config["build-system"]["build-backend"] == "hatchling.build"
    assert config["dependency-groups"]["build"] == config["build-system"]["requires"]
    assert config["tool"]["ruff"]["lint"]["select"] == ["E4", "E7", "E9", "F"]


def test_ci_tests_default_branch_and_tag_before_any_publication():
    # BaseLoader retains the YAML key `on` rather than YAML 1.1's boolean True.
    workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    triggers = workflow["on"]
    assert triggers["push"]["branches"] == triggers["pull_request"]["branches"] == ["master"]
    assert triggers["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in triggers
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    jobs = workflow["jobs"]
    test_runs = [step["run"] for step in jobs["test"]["steps"] if "run" in step]
    assert test_runs == ["uv sync --locked --extra dev", "uv run --no-sync ruff check .", "uv run --no-sync python -m pytest"]
    release = jobs["release"]
    assert release["if"] == "github.ref_type == 'tag'"
    assert release["needs"] == "test"
    assert release["environment"]["name"] == "pypi"
    assert release["permissions"]["id-token"] == "write"
    steps = release["steps"]
    runs = [step.get("run", "") for step in steps]
    validate = next(i for i, run in enumerate(runs) if "release_metadata.py" in run)
    guard = next(i for i, run in enumerate(runs) if "pypi_preflight.py" in run)
    build = runs.index("uv build --no-build-isolation --python .venv/bin/python")
    artifact = next(i for i, step in enumerate(steps) if step.get("uses") == "actions/upload-artifact@v4")
    publish = next(i for i, step in enumerate(steps) if step.get("uses") == "pypa/gh-action-pypi-publish@release/v1")
    assert validate < guard < build < artifact < publish
    assert steps[artifact]["with"]["name"] == "release-dist"
    assert steps[artifact]["with"]["path"] == "dist/"
    assert "skip-existing" not in steps[publish].get("with", {})
    github = jobs["github-release"]
    assert github["if"] == "github.ref_type == 'tag'" and github["needs"] == "release"
    assert github["permissions"] == {"contents": "write"}
    assert not any("uv build" in step.get("run", "") for step in github["steps"])
    download = next(step for step in github["steps"] if step.get("uses") == "actions/download-artifact@v4")
    assert download["with"] == {"name": "release-dist", "path": "dist/"}
    upload = github["steps"][-1]["with"]
    assert upload["files"] == "dist/*" and upload["overwrite_files"] == "false"
    assert upload["body_path"] == "build/RELEASE_NOTES.md"
    assert "needs.release.outputs.prerelease" in upload["prerelease"]
