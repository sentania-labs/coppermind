#!/usr/bin/env bash
set -euo pipefail

# The release proof only means something if every Coppermind service runs the
# published image. Rendering the overlay and reading back what compose resolved
# is the only way to see that, because an inherited build section survives in
# ways the source file does not show.
export IMAGE_ROOT="${IMAGE_ROOT:-ghcr.io/sentania-labs/coppermind}"
export VERSION="${VERSION:-v0.0.0}"

docker compose -f docker-compose.yml -f ci/docker-compose.published.yml \
  --profile smoke config --format json | python3 -c '
import json, os, sys

published = {
    "bootstrap": "store",
    "migrate": "store",
    "store": "store",
    "editor": "store",
    "api": "api",
    "git": "git",
    "obsidian-sync": "obsidian-sync",
}
root = os.environ["IMAGE_ROOT"]
version = os.environ["VERSION"]
services = json.load(sys.stdin)["services"]

problems = []
for name in sorted(set(published) - set(services)):
    problems.append(f"{name} is missing from the rendered configuration")
for name, image in sorted(published.items()):
    rendered = services.get(name)
    if rendered is None:
        continue
    if "build" in rendered:
        problems.append(f"{name} still builds from the working tree, so it would not run the published image")
    expected = f"{root}/{image}:{version}"
    actual = rendered.get("image")
    if actual != expected:
        problems.append(f"{name} runs {actual}, expected {expected}")
if problems:
    print("\n".join(problems), file=sys.stderr)
    raise SystemExit(1)
print(f"published overlay: {len(published)} services run published images and build nothing")
'
