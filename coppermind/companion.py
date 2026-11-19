"""Compare companion files without treating a release-only version bump as a change."""

import hashlib
import json


def fingerprint(files: dict[str, bytes]) -> str:
    manifest = json.loads(files["manifest.json"])
    if not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str):
        raise ValueError("Invalid companion manifest")
    manifest.pop("version")
    digest = hashlib.sha256()
    for name in ("main.js", "manifest.json", "styles.css"):
        content = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            if name == "manifest.json"
            else files[name]
        )
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()
