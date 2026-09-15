#!/usr/bin/env bash
set -euo pipefail

# An overlay that leaves a build section behind silently builds that service
# from the working tree, so the stack no longer runs the images it claims to.
# Rendering the overlay and reading back what compose resolved is the only way
# to see that, because an inherited build section survives in ways the source
# file does not show.
overlay="${1:?overlay file is required}"
export IMAGE_ROOT="${2:?image root is required}"
export VERSION="${3:?image tag is required}"

docker compose -f docker-compose.yml -f "$overlay" \
  --profile smoke config --format json | OVERLAY="$overlay" python3 -c '
import json, os, sys

expected_images = {
    "bootstrap": "store",
    "migrate": "store",
    "store": "store",
    "editor": "store",
    "api": "api",
    "admin": "admin",
    "git": "git",
    "obsidian-sync": "obsidian-sync",
}
overlay = os.environ["OVERLAY"]
root = os.environ["IMAGE_ROOT"]
version = os.environ["VERSION"]
services = json.load(sys.stdin)["services"]

problems = []
for name in sorted(set(expected_images) - set(services)):
    problems.append(f"{name} is missing from the rendered configuration")
for name, rendered in sorted(services.items()):
    if "build" in rendered:
        problems.append(f"{name} still builds from the working tree, so it would not run the {version} image")
for name, image in sorted(expected_images.items()):
    rendered = services.get(name)
    if rendered is None:
        continue
    expected = f"{root}/{image}:{version}"
    actual = rendered.get("image")
    if actual != expected:
        problems.append(f"{name} runs {actual}, expected {expected}")
if problems:
    print(f"{overlay}:", file=sys.stderr)
    print("\n".join(problems), file=sys.stderr)
    raise SystemExit(1)
print(f"{overlay}: {len(services)} rendered services build nothing, {len(expected_images)} run {version} images")
'
