"""Discover + merge qatlas client plugins.

No first-party plugins ship with the package anymore (the lean / claim
plugins were removed when the CLI narrowed to paper workflows). Third-party
plugins register via the ``qatlas.plugins`` entry-point group and are always
considered (gated by their own ``available()``). Only plugins whose
``available()`` env-check passes contribute commands; a failing plugin import
is skipped (never breaks the CLI for unrelated commands).

Protocol-version mismatches are the one exception to silent skipping: a
plugin declaring a ``cli_api_version`` newer than this CLI's
``PLUGIN_API_VERSION`` is skipped with a single stderr warning, because
silently dropping commands the user expects is much harder to debug.
Set ``QATLAS_QUIET=1`` to silence that warning.
"""

from __future__ import annotations

import inspect
import os
import sys

from .base import PLUGIN_API_VERSION, CliContext, CommandSpec, QatlasPlugin

#: Top-level commands that ship as standalone plugin packages. When the user
#: invokes one without the plugin installed, the CLI prints an install hint
#: instead of a bare "unknown command". Maps command name →
#: (package name, GitHub repo). Third parties can mirror this table in their
#: own docs; the CLI only consults it for hint text.
KNOWN_STANDALONE_PLUGINS: dict[str, tuple[str, str]] = {
    "search": ("qatlas-search", "IAI-USTC-Quantum/qatlas-search"),
    "rag": ("qatlas-rag", "IAI-USTC-Quantum/qatlas-rag"),
}


def _warn(message: str) -> None:
    if os.environ.get("QATLAS_QUIET"):
        return
    print(f"Warning: {message}", file=sys.stderr)


def _entrypoint_plugins() -> list[QatlasPlugin]:
    out: list[QatlasPlugin] = []
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        group = eps.select(group="qatlas.plugins") if hasattr(eps, "select") else eps.get("qatlas.plugins", [])  # type: ignore[union-attr]
        for ep in group:
            try:
                factory = ep.load()
                plugin = factory() if callable(factory) else factory
                if isinstance(plugin, QatlasPlugin):
                    out.append(plugin)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def _plugin_protocol_version(plugin: QatlasPlugin) -> int:
    try:
        return int(getattr(plugin, "cli_api_version", 1))
    except (TypeError, ValueError):
        return 1


def active_plugins() -> list[QatlasPlugin]:
    """Entry-point plugins that report themselves available."""
    out: list[QatlasPlugin] = []
    for p in _entrypoint_plugins():
        try:
            version = _plugin_protocol_version(p)
            if version > PLUGIN_API_VERSION:
                _warn(
                    f"plugin '{getattr(p, 'name', '') or 'unnamed'}' requires CLI "
                    f"plugin API v{version} but this qatlas-cli provides "
                    f"v{PLUGIN_API_VERSION}; skipping its commands "
                    f"(upgrade qatlas-cli to use them)"
                )
                continue
            if p.available():
                out.append(p)
        except Exception:  # noqa: BLE001
            continue
    return out


def top_level_commands() -> dict[str, CommandSpec]:
    """Merged ``qatlas <name>`` commands from all active plugins."""
    out: dict[str, CommandSpec] = {}
    for p in active_plugins():
        try:
            out.update(p.top_level_commands())
        except Exception:  # noqa: BLE001
            continue
    return out


def contrib_subcommands() -> dict[str, CommandSpec]:
    """Merged ``qatlas contrib <name>`` subcommands from all active plugins."""
    out: dict[str, CommandSpec] = {}
    for p in active_plugins():
        try:
            out.update(p.contrib_subcommands())
        except Exception:  # noqa: BLE001
            continue
    return out


def build_cli_context(request_timeout: float = 120.0) -> CliContext:
    """Resolve the current config/token state into a :class:`CliContext`.

    Reads the same sources as the built-in commands
    (``~/.config/qatlas/config.yaml`` + the per-host token store). Any
    failure degrades to empty-string fields — plugin commands then produce
    their own readable "not configured" errors instead of crashing here.
    """
    from qatlas import __version__ as client_version

    base, token, insecure = "", "", False
    try:
        from qatlas.client._common import default_base_url, resolve_token
        from qatlas.config import ServerConfig

        base = default_base_url()
        token = resolve_token(None) or ""  # signature ignores the arg since v0.19
        insecure = bool(ServerConfig.from_env().insecure)
    except Exception:  # noqa: BLE001
        pass
    return CliContext(
        server_base_url=base,
        token=token,
        request_timeout=request_timeout,
        insecure=insecure,
        client_version=client_version,
    )


def _handler_takes_context(handler) -> bool:
    """Whether ``handler`` declares the v2 ``(ctx, argv)`` signature."""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):  # builtins / C callables without a signature
        return False
    params = list(sig.parameters.values())
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return True  # ``*args`` handlers accept anything
    positional = [
        p
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


def invoke(spec: CommandSpec, ctx: CliContext, argv: list[str]) -> int:
    """Invoke a plugin command handler with protocol-shape dispatch.

    v2 handlers are called as ``handler(ctx, argv)``; v1 handlers keep the
    legacy ``handler(argv)`` call shape. Return values are normalized the
    same way the core CLI normalizes ``SystemExit`` codes.
    """
    if _handler_takes_context(spec.handler):
        result = spec.handler(ctx, argv)
    else:
        result = spec.handler(argv)
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    print(result, file=sys.stderr)
    return 1
