"""Top-level command-line entry point for QuantumAtlas.

This module backs the ``qatlas`` console script declared in
``pyproject.toml``.  It intentionally delegates to the existing module CLIs so
their behavior stays identical to ``python -m qatlas.<module>``.
"""

from __future__ import annotations

import runpy
import sys
from dataclasses import dataclass
from typing import Mapping, Sequence

from qatlas import __version__


@dataclass(frozen=True)
class Command:
    """A delegated QuantumAtlas CLI command."""

    module: str
    summary: str
    client_friendly: bool = True


COMMANDS: Mapping[str, Command] = {
    "config": Command(
        "qatlas.client.config",
        "Manage the user-level config file (~/.config/qatlas/config.yaml)",
    ),
    "auth": Command(
        "qatlas.client.auth",
        "Manage saved PATs / session tokens per host (login, status, token, logout)",
    ),
    "paper": Command(
        "qatlas.client.paper",
        "Paper workflows: get markdown/images/figures/image/metadata, status (single or batch), list, lookup, batch fetch, downloader jobs",
    ),
    "contrib": Command(
        "qatlas.client.contrib",
        "Contributor workflows: upload PDFs (contrib pdf) or run local MinerU and push (contrib mineru)",
    ),
    "parser": Command("qatlas.parser.__main__", "Fetch and parse arXiv papers", False),
}

ALIASES: Mapping[str, str] = {
    "papers": "paper",
    "parse": "parser",
}


def _print_help() -> None:
    """Print top-level CLI help."""

    print(
        """QuantumAtlas command line

Usage:
  qatlas <command> [args...]
  qatlas --version
  qatlas --help

Commands:"""
    )

    print("  Client/operator commands:")
    for name, command in COMMANDS.items():
        if not command.client_friendly:
            continue
        print(f"    {name:<10} {command.summary}")

    print("\n  Local workspace commands:")
    for name, command in COMMANDS.items():
        if command.client_friendly:
            continue
        print(f"    {name:<10} {command.summary}")

    # Plugin-contributed top-level commands (only those available in the
    # current environment). No first-party plugins ship today; third-party
    # plugins register via the ``qatlas.plugins`` entry-point group.
    try:
        from qatlas.client.plugins import registry

        plugin_cmds = registry.top_level_commands()
    except Exception:
        plugin_cmds = {}
    if plugin_cmds:
        print("\n  Plugin commands:")
        for name in sorted(plugin_cmds):
            spec = plugin_cmds[name]
            print(f"    {name:<10} {spec.summary}")
            usage = getattr(spec, "usage", None)
            if usage:
                print(f"    {'':10} {usage}")

    print(
        """
Aliases:
  papers -> paper
  parse -> parser

Examples:
  qatlas paper get markdown quant-ph/9508027 --output paper.md
  qatlas paper get images 10.1103/PhysRevLett.103.150502 -o images.zip
  qatlas contrib pdf quant-ph/9508027v1 --pdf paper.pdf
  qatlas contrib mineru 2501.00010v1
  qatlas contrib mineru --watch

Documentation:
  https://qatlas.hfnl.app.chenzhaoyun.com/doc/guide/cli.html

Use "qatlas <command> --help" for command-specific options."""
    )


def _print_usage_error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    print("Run 'qatlas --help' to see available commands.", file=sys.stderr)


# Top-level commands that ship as standalone plugins (entry-point group
# ``qatlas.plugins``). When the user invokes one without the plugin
# installed, the CLI prints an install hint instead of a bare "unknown
# command". The table lives in the plugin registry so it stays next to the
# discovery code; the fallback covers a broken plugins package.
try:
    from qatlas.client.plugins.registry import (
        KNOWN_STANDALONE_PLUGINS as _KNOWN_PLUGIN_COMMANDS,
    )
except Exception:  # pragma: no cover - defensive, mirrors dispatch fallback
    _KNOWN_PLUGIN_COMMANDS: dict[str, tuple[str, str]] = {}


def _print_plugin_hint(command_name: str) -> None:
    """Explain that ``command_name`` ships as a standalone plugin."""

    package, repo = _KNOWN_PLUGIN_COMMANDS[command_name]
    print(
        f"The '{command_name}' command is provided by the standalone plugin "
        f"{package}, which is not installed.\n"
        f"Install it with pip/uv from the private repository {repo}, e.g.:\n"
        f"  uv tool install --from git+ssh://git@github.com/{repo}.git {package}\n"
        "or, into the current environment:\n"
        f"  uv pip install git+ssh://git@github.com/{repo}.git",
        file=sys.stderr,
    )


def _exit_code(code: object) -> int:
    """Normalize a child ``SystemExit.code`` value to an integer exit code."""

    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def _run_module(module: str, argv0: str, args: Sequence[str]) -> int:
    """Run a child module as if it had been invoked with ``python -m``."""

    original_argv = sys.argv[:]
    sys.argv = [argv0, *args]
    try:
        try:
            runpy.run_module(module, run_name="__main__")
        except SystemExit as exc:
            return _exit_code(exc.code)
        return 0
    finally:
        sys.argv = original_argv


def main(argv: Sequence[str] | None = None) -> int:
    """Run the QuantumAtlas CLI."""

    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in {"-h", "--help"}:
        _print_help()
        return 0

    if args[0] in {"-V", "--version"}:
        print(f"qatlas {__version__}")
        return 0

    requested_command = args[0].replace("_", "-")
    command_name = ALIASES.get(requested_command, requested_command)
    command = COMMANDS.get(command_name)

    # Plugin-contributed top-level commands fill gaps the static table
    # doesn't cover. Built-ins always take precedence.
    plugin_spec = None
    if command is None:
        try:
            from qatlas.client.plugins import registry

            plugin_spec = registry.top_level_commands().get(command_name)
        except Exception:
            plugin_spec = None
        if plugin_spec is None:
            if command_name in _KNOWN_PLUGIN_COMMANDS:
                _print_plugin_hint(command_name)
                return 2
            _print_usage_error(f"unknown command '{args[0]}'")
            return 2

    # v0.17.0+: client config lives exclusively in
    # ~/.config/qatlas/config.yaml. Ensure it exists on first run so
    # the user can immediately edit it; idempotent on subsequent runs.
    #
    # Exception: skip for `qatlas config` itself — its `path` / `show`
    # subcommands intentionally tolerate a missing file and would
    # display misleading "auto-created on first read" behaviour
    # otherwise.
    if command_name != "config":
        try:
            from qatlas.config import ensure_default_config_exists
            ensure_default_config_exists()
        except Exception:
            # Defensive: never block a subcommand on config-file IO;
            # the embedded defaults work for any read-only command.
            pass

    if plugin_spec is not None:
        from qatlas.client.plugins import registry

        ctx = registry.build_cli_context()
        return registry.invoke(plugin_spec, ctx, args[1:])

    return _run_module(
        command.module,
        argv0=f"qatlas {command_name}",
        args=args[1:],
    )


if __name__ == "__main__":
    raise SystemExit(main())
