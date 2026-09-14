"""QuantumAtlas's independently installed HTTP command-line client."""

from importlib.metadata import version

# Editable checkouts are installed by `uv sync`, just like wheel installations.
# Never mask stale/missing distribution metadata by reading a nearby pyproject.
__version__ = version("qatlas-cli")

__all__ = ["__version__"]
