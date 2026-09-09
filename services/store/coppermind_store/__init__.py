"""The store service.

Exactly one process writes the notes filesystem, and this is it. The API, the
curator and the indexer ask the store; nothing else opens a note file for
writing. That single writer rule is what makes the filesystem safe to treat as
the source of truth, and it is why the store runs as exactly one replica.
"""

__all__ = ["__version__"]

__version__ = "0.1.0.dev0"
