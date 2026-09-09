"""Shared Coppermind package.

Everything here is imported by more than one service image: identifiers,
the note file format, portable naming, the frontmatter schema, product
settings, control state files, and the store contract. Nothing here touches
the notes filesystem directly; only the store service does that.
"""

__all__ = ["__version__"]

__version__ = "0.1.0.dev0"
