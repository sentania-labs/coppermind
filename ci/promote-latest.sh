#!/usr/bin/env bash
set -euo pipefail

: "${VERSION:?release version is required}"
: "${IMAGE_ROOT:?image repository root is required}"
: "${DIGESTS:?digest record is required}"
test -s "$DIGESTS" || { echo "digest record is empty" >&2; exit 1; }
expected="$(printf '%s\n' store api git obsidian-sync)"
actual="$(awk '{ print $1 }' "$DIGESTS")"
test "$actual" = "$expected" || { echo "$VERSION does not record every service once" >&2; exit 1; }
while read -r service digest; do
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "$service has an invalid digest" >&2; exit 1; }
  image="$IMAGE_ROOT/$service"
  skopeo copy --all --preserve-digests "docker://$image@$digest" "docker://$image:latest"
  latest="sha256:$(skopeo inspect --raw "docker://$image:latest" | sha256sum | cut -d ' ' -f 1)"
  test "$latest" = "$digest" || { echo "$image:latest differs from $VERSION" >&2; exit 1; }
done < "$DIGESTS"

echo "latest now names every image from $VERSION"
