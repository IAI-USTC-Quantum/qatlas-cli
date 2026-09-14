"""Standalone documentation; no application imports or build hooks."""

project = "qatlas-cli"
author = "IAI-USTC-Quantum"
language = "zh_CN"
extensions = ["myst_parser"]
root_doc = "index"
myst_heading_anchors = 6
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "requirements.txt"]
html_theme = "furo"
html_title = f"{project} 文档"
html_search_language = "zh"
