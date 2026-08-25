"""
QuantumAtlas - AI 驱动的量子算法开发辅助系统

核心功能：从论文到可执行量子代码的完整转化链路
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version_from_pyproject() -> str:
    """Read [project].version from the nearest repo-root pyproject.toml.

    Supports both the legacy flat layout (``qatlas/`` at the repo root,
    pyproject at ``parents[1]``) and the src layout (``src/qatlas/``,
    pyproject at ``parents[2]``).
    """
    here = Path(__file__).resolve()
    for parent in (here.parents[1], here.parents[2]):
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        try:
            return str(data["project"]["version"])
        except KeyError:
            continue
    raise FileNotFoundError("no pyproject.toml with [project].version found")


def _resolve_version() -> str:
    """Prefer repo pyproject when present, else installed distribution metadata."""
    try:
        return _version_from_pyproject()
    except (FileNotFoundError, KeyError, OSError):
        try:
            return version("qatlas-cli")
        except PackageNotFoundError:
            return "0+unknown"


__version__ = _resolve_version()

__all__ = ["__version__"]
