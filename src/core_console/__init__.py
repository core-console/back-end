"""Core Console main backend."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("core-console")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]
