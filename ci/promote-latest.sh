#!/usr/bin/env bash
set -euo pipefail

: "${VERSION:?release version is required}"
: "${IMAGE_ROOT:?image repository root is required}"
: "${DIGESTS:?digest record is required}"
test -s "$DIGESTS" || { echo "digest record is empty" >&2; exit 1; }
expected="$(printf '%s\n' store api git obsidian-sync)"
actual="$(awk '{ print $1 }' "$DIGESTS")"
test "$actual" = "$expected" || { echo "$VERSION does not record every service once" >&2; exit 1; }

# The quickstart runs whatever latest names, so latest may only move forward.
# A re-run of an older tag's job, or two tags promoting out of order, would
# otherwise hand a clean checkout an older build with nothing reporting it.
# The version already on the image is the record of where latest stands.
serving_version() {
  skopeo inspect --format '{{ index .Labels "org.opencontainers.image.version" }}' \
    "docker://$1:latest" 2>/dev/null || true
}

while read -r service digest; do
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "$service has an invalid digest" >&2; exit 1; }
  image="$IMAGE_ROOT/$service"
  serving="$(serving_version "$image")"
  [[ "$serving" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
  if [ "$serving" != "$VERSION" ] &&
     [ "$(printf '%s\n%s\n' "$VERSION" "$serving" | sort -V | head -n 1)" = "$VERSION" ]; then
    echo "$image:latest already names $serving, which is newer than $VERSION" >&2
    echo "latest only moves forward, so nothing was promoted" >&2
    exit 1
  fi
done < "$DIGESTS"

while read -r service digest; do
  image="$IMAGE_ROOT/$service"
  skopeo copy --all --preserve-digests "docker://$image@$digest" "docker://$image:latest"
  latest="sha256:$(skopeo inspect --raw "docker://$image:latest" | sha256sum | cut -d ' ' -f 1)"
  test "$latest" = "$digest" || { echo "$image:latest differs from $VERSION" >&2; exit 1; }
done < "$DIGESTS"

echo "latest now names every image from $VERSION"
