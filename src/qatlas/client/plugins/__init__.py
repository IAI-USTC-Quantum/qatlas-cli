"""Client-side plugin system for the ``qatlas`` CLI.

A plugin contributes commands to the CLI without the core having to hard-code
them. Two contribution surfaces:

* **top-level commands** — ``qatlas <name> ...``;
* **contrib subcommands** — ``qatlas contrib <name> ...`` (a plugin may add a
  subcommand under the existing ``qatlas contrib`` group).

No first-party plugins ship today; plugins are discovered from the
``qatlas.plugins`` Python entry-point group. Each declares whether it is
``available()`` in the current environment, so the CLI surface adapts without
code changes.
"""

from __future__ import annotations
