"""Validate native Sphinx HTML output without importing the application.

Run after sphinx-build, never as a Sphinx extension or build hook.
BeautifulSoup is part of Furo's hash-locked documentation dependencies.
"""

import argparse
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from sphinx.search import js_index

PROJECT = "qatlas-cli"
DOCNAMES = {"index", "overview"}
SEARCH_TERMS = {"论文": "overview", "配置": "overview"}


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_dir", type=Path, nargs="?", default=Path("build/docs"))
    root = parser.parse_args().build_dir.resolve()
    pages = {}

    def page(path):
        if path not in pages:
            require(path.is_file(), f"Missing HTML page: {path}")
            pages[path] = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        return pages[path]

    for name in sorted(DOCNAMES | {"search"}):
        path = root / f"{name}.html"
        html = page(path)
        require(html.title and f"{PROJECT} 文档" in html.title.get_text(),
                f"Wrong project title: {path}")
        for element in html.find_all(["a", "link", "script", "img"]):
            link = element.get("href") or element.get("src")
            if not link:
                continue
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue  # Never fetch external URLs (including private services).
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            require(target.is_relative_to(root), f"Link escapes build directory: {link}")
            require(target.is_file(), f"Broken local resource in {name}: {link}")
            if url.fragment and target.suffix == ".html":
                require(page(target).find(id=unquote(url.fragment)) is not None,
                        f"Broken local anchor in {name}: {link}")

    for asset in ("_static/styles/furo.css", "_static/searchtools.js",
                  "_static/language_data.js"):
        require((root / asset).is_file(), f"Missing theme/search asset: {asset}")
    language_js = (root / "_static/language_data.js").read_text(encoding="utf-8")
    require("ChineseStemmer" not in language_js,
            "Unexpected ChineseStemmer reference: check Sphinx Chinese search regression")

    with (root / "searchindex.js").open(encoding="utf-8") as stream:
        index = js_index.load(stream)
    require(set(index["docnames"]) == DOCNAMES, "Unexpected search-index page set")
    for term, docname in SEARCH_TERMS.items():
        matches = set()
        for field in ("terms", "titleterms"):
            value = index[field].get(term, [])
            matches.update([value] if isinstance(value, int) else value)
        require(index["docnames"].index(docname) in matches,
                f"Chinese search term {term!r} does not find {docname}")
    print(f"{PROJECT}: {len(DOCNAMES)} indexed pages; local links, Furo assets and "
          f"Chinese terms {', '.join(SEARCH_TERMS)} OK")


if __name__ == "__main__":
    main()
