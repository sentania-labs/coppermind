"""The Coppermind API service.

Stateless by design: it holds no notes, mounts no volume, and owns no
long lived state. Every note operation is a call to the store, which is the
only writer of the notes filesystem. That is what lets the API run as more
than one replica while the store stays a single writer.

Admin is a separate service and image with its own lifecycle, exposure and
authentication posture. Nothing here assumes Admin lives inside this process.
"""

__all__ = ["__version__"]

__version__ = "0.1.0.dev0"
