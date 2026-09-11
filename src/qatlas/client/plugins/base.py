"""Plugin contract for the qatlas client CLI.

Protocol v2 adds :class:`CliContext` — plugin command handlers may accept
``(ctx, argv)`` instead of the v1 ``(argv)`` and reuse the shared HTTP layer
in :mod:`qatlas.client.pluginsupport` (auth header, version negotiation,
timeouts, error formatting) instead of re-implementing it per plugin.
The v1 signature keeps working; dispatch picks the shape by introspection
(see :func:`qatlas.client.plugins.registry.invoke`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

#: Version of the plugin protocol this CLI implements.
#:
#: * v1 (implicit): ``CommandSpec.handler`` receives ``(argv)`` only.
#: * v2: handlers may receive ``(ctx, argv)`` with a :class:`CliContext`,
#:   and ``qatlas.client.pluginsupport`` is the supported HTTP layer for
#:   plugin commands.
#:
#: A plugin built against a NEWER protocol sets
#: ``QatlasPlugin.cli_api_version`` explicitly; this CLI skips it with a
#: one-line warning instead of failing obscurely (upgrade qatlas-cli).
PLUGIN_API_VERSION = 2


@dataclass(frozen=True)
class CliContext:
    """Resolved client context handed to plugin command handlers.

    Built once per dispatch from ``~/.config/qatlas/config.yaml`` and the
    per-host token store — the same sources the built-in commands use.
    Empty ``server_base_url`` / ``token`` mean "not configured": plugin
    commands should surface a readable error pointing at
    ``qatlas config set server_url ...`` / ``qatlas auth login``.
    """

    server_base_url: str = ""
    token: str = ""
    request_timeout: float = 120.0
    insecure: bool = False
    client_version: str = ""

    def auth_headers(self) -> dict[str, str]:
        """Authorization header dict (empty when no token is stored)."""
        token = (self.token or "").strip()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}


@dataclass(frozen=True)
class CommandSpec:
    """One CLI command a plugin contributes.

    ``handler`` receives the remaining argv (everything after the command
    name) and returns a process exit code. Protocol v2 handlers take
    ``(ctx, argv)``; v1 handlers take ``(argv)`` — both shapes dispatch.

    ``usage`` is an optional one-line usage string shown under the summary
    in ``qatlas --help`` (e.g. ``"search QUERY [--sources a,b] [--json]"``).
    """

    handler: Callable[..., int]
    summary: str
    usage: str | None = None


class QatlasPlugin:
    """Base class for a qatlas client plugin.

    Subclasses set ``name`` and override the contribution hooks they use.
    ``available`` gates whether the plugin's commands appear in the CLI at all
    (e.g. the lean plugin is only available when a checkout is configured).
    """

    name: str = ""

    #: Plugin protocol version this plugin was built against. The default
    #: tracks whatever CLI provides the base class; only plugins relying on
    #: features from a FUTURE protocol need to set it explicitly.
    cli_api_version: int = PLUGIN_API_VERSION

    def available(self) -> bool:
        return True

    def top_level_commands(self) -> dict[str, CommandSpec]:
        """Commands mounted as ``qatlas <name>``."""
        return {}

    def contrib_subcommands(self) -> dict[str, CommandSpec]:
        """Subcommands mounted as ``qatlas contrib <name>``."""
        return {}
