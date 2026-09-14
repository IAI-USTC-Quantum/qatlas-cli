"""Read-only guard: don't rebuild a version already recorded by PyPI."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.utils import canonicalize_name
import requests

ROOT = Path(__file__).resolve().parents[1]


def ensure_unpublished(session: requests.Session, name: str, version: str) -> None:
    response = session.get(
        f"https://pypi.org/pypi/{canonicalize_name(name)}/json",
        timeout=30,
        allow_redirects=False,
    )
    # A missing project is valid for a first release/pending trusted publisher.
    # An HTML proxy error, authentication error or outage is NOT proof of absence.
    if response.status_code == 404 and response.json() == {"message": "Not Found"}:
        return
    if response.status_code != 200:
        raise ValueError(f"Cannot confirm PyPI state (HTTP {response.status_code}); stopping")
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("releases"), dict):
        raise ValueError("Malformed PyPI project response; stopping")
    if canonicalize_name(data.get("info", {}).get("name", "")) != canonicalize_name(name):
        raise ValueError("Unexpected PyPI project identity; stopping")
    if version in data["releases"]:
        raise ValueError(
            f"PyPI already records {name} {version}; do not rebuild or rerun publication. "
            "Recover release-dist from the original run and complete only missing uploads."
        )


def main() -> int:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    try:
        with requests.Session() as session:
            session.trust_env = False  # no personal netrc credentials or proxy identity
            ensure_unpublished(session, project["name"], project["version"])
    except (requests.RequestException, ValueError, TypeError, AttributeError) as error:
        print(f"PyPI preflight failed: {error}", file=sys.stderr)
        return 1
    print("PyPI version is absent; first publication may proceed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
