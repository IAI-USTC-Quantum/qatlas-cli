"""Discover + merge qatlas client plugins.

No first-party plugins ship with the package anymore (the lean / claim
plugins were removed when the CLI narrowed to paper workflows). Third-party
plugins register via the ``qatlas.plugins`` entry-point group and are always
considered (gated by their own ``available()``). Only plugins whose
``available()`` env-check passes contribute commands; a failing plugin import
is skipped (never breaks the CLI for unrelated commands).
"""

from __future__ import annotations

from .base import CommandSpec, QatlasPlugin


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


def active_plugins() -> list[QatlasPlugin]:
    """Entry-point plugins that report themselves available."""
    out: list[QatlasPlugin] = []
    for p in _entrypoint_plugins():
        try:
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
