# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "sphinx==8.2.3",
#     "furo==2025.12.19",
#     "myst-parser==5.1.0",
#     "jieba==0.42.1",
# ]
# ///
"""Build and verify standalone Sphinx documentation without installing the application."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    os.chdir(ROOT)
    output = ROOT / "build/docs"
    subprocess.check_call(
        [sys.executable, "-m", "sphinx", "-W", "--keep-going", "-n", "-b", "html", "docs", str(output)],
    )
    sys.path.insert(0, str(ROOT / "docs"))
    import check_build
    sys.argv = ["check_build.py", str(output)]
    check_build.main()


if __name__ == "__main__":
    main()
