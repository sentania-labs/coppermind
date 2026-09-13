"""Container health: the status file was rewritten recently.

The helper rewrites it every 30 seconds whatever else is happening, so an old
file means the loop is stuck or gone. A Git error does not make the container
unhealthy: restarting would not fix it, and the file's `last_error` says what
it is.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

MAX_AGE_S = 600


def status_path(data_dir: Path) -> Path:
    return data_dir / "state" / "git" / "status.json"


def main() -> None:
    path = status_path(Path(os.environ.get("COPPERMIND_DATA_DIR", "/data")))
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        sys.exit(1)
    sys.exit(0 if age < MAX_AGE_S else 1)


if __name__ == "__main__":
    main()
