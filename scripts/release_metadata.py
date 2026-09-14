"""Validate release inputs before building/uploading; safe to exercise offline."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import tomllib

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]


def validate_release(root: Path, tag: str) -> tuple[str, bool, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    try:
        parsed = Version(version)
    except InvalidVersion as error:
        raise ValueError("Invalid PEP 440 project version") from error
    if str(parsed) != version or parsed.epoch or parsed.local is not None:
        raise ValueError("Use canonical PEP 440 without epoch/local version segments")
    if tag != f"v{version}":
        raise ValueError("tag != project version")
    if len(tag) > 128:
        raise ValueError("Version tag exceeds 128 characters")
    version_file = root / "VERSION"
    if version_file.exists() and version_file.read_text(encoding="utf-8").strip() != version:
        raise ValueError("VERSION drift")
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = [p for p in lock["package"] if canonicalize_name(p["name"]) == canonicalize_name(project["name"])]
    if len(packages) != 1 or packages[0]["version"] != version:
        raise ValueError("uv.lock project version drift")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.compile(
        r"^## v?" + re.escape(version) + r"(?:[ \t]+\(\d{4}-\d{2}-\d{2}\))?[ \t]*$",
        re.MULTILINE,
    )
    matches = list(heading.finditer(changelog))
    if len(matches) != 1:
        raise ValueError("Need exactly one matching CHANGELOG version heading")
    notes = re.split(r"^## ", changelog[matches[0].end():], maxsplit=1, flags=re.MULTILINE)[0].strip()
    if not notes:
        raise ValueError("Empty release notes")
    return version, parsed.is_prerelease, notes + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--notes", type=Path, default=ROOT / "build/RELEASE_NOTES.md")
    args = parser.parse_args()
    try:
        version, prerelease, notes = validate_release(ROOT, args.tag)
    except (ValueError, KeyError, OSError) as error:
        print(f"Release validation failed: {error}", file=sys.stderr)
        return 1
    args.notes.parent.mkdir(parents=True, exist_ok=True)
    args.notes.write_text(notes, encoding="utf-8")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"version={version}\nprerelease={str(prerelease).lower()}\n")
    print(f"Validated v{version}; prerelease={prerelease}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
