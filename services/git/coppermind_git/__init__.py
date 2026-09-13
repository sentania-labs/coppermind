"""Coppermind Git helper: records the history of the notes filesystem."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("coppermind-git")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0.0.0"
