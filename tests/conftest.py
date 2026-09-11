from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


CONFIG_ENV_KEYS = [
    # New QATLAS_* names
    "QATLAS_SERVER_HOST",
    "QATLAS_SERVER_PORT",
    "QATLAS_SERVER_DEBUG",
    "QATLAS_WIKI_DIR",
    "QATLAS_RAW_DIR",
    "QATLAS_DATA_DIR",
    "QATLAS_SERVER_URL",
    "QATLAS_INSECURE",
    "QATLAS_USER_HEADER",
    # Legacy bare aliases (kept active for back-compat)
    "SERVER_HOST",
    "SERVER_PORT",
    "SERVER_DEBUG",
    "WIKI_DIR",
    "RAW_DIR",
    "DATA_DIR",
    "PUBLIC_BASE_URL",
    "USER_HEADER",
    # Third-party vendor names (no prefix by design)
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "MINERU_API_TOKEN",
    "MINERU_API_TOKENS",
    "MINERU_API_BASE_URL",
    "MINERU_MODEL_VERSION",
    "MINERU_LANGUAGE",
    "MINERU_IS_OCR",
    "MINERU_ENABLE_FORMULA",
    "MINERU_ENABLE_TABLE",
    "MINERU_POLL_INTERVAL",
    "MINERU_TIMEOUT",
]


def _clear_config_env() -> None:
    for key in CONFIG_ENV_KEYS:
        os.environ.pop(key, None)


# Set both new and legacy skip-dotenv names so any code path is covered.
os.environ["QATLAS_SKIP_DOTENV"] = "1"
os.environ["QUANTUMATLAS_SKIP_DOTENV"] = "1"
_clear_config_env()


@pytest.fixture(autouse=True)
def isolate_project_env(request, monkeypatch):
    """Keep ordinary tests independent from the developer's repository .env."""
    if request.node.get_closest_marker("e2e"):
        monkeypatch.delenv("QATLAS_SKIP_DOTENV", raising=False)
        monkeypatch.delenv("QUANTUMATLAS_SKIP_DOTENV", raising=False)
        yield
    else:
        monkeypatch.setenv("QATLAS_SKIP_DOTENV", "1")
        monkeypatch.setenv("QUANTUMATLAS_SKIP_DOTENV", "1")
        _clear_config_env()
        yield

    _clear_config_env()
    os.environ["QATLAS_SKIP_DOTENV"] = "1"
    os.environ["QUANTUMATLAS_SKIP_DOTENV"] = "1"


@pytest.fixture
def c_locale_runner():
    """Run a python snippet in a fresh interpreter whose default text
    encoding is NOT UTF-8 (``LC_ALL=C`` → us-ascii).

    Reproduces on any platform the zh-CN Windows failure mode where
    ``open()`` / ``read_text()`` without an explicit encoding decode
    UTF-8 files as GBK and crash with UnicodeDecodeError. The locale is
    interpreter-startup state, so this cannot be monkeypatched in-proc.
    """
    def run(
        code: str, *, home: Path, timeout: float = 120
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "LC_ALL": "C",
            "LANG": "C",
            # Stop CPython from rescuing us: no UTF-8 mode, no PEP 538
            # locale coercion to C.UTF-8.
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
        }
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )

    return run
