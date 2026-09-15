#!/usr/bin/env bash
set -euo pipefail

: "${VERSION:?release version is required}"
: "${IMAGE_ROOT:?image repository root is required}"
: "${DIGESTS:?digest record is required}"
test -s "$DIGESTS" || { echo "digest record is empty" >&2; exit 1; }

# The quickstart runs whatever latest names, so latest may only move forward.
# A re-run of an older tag's job, or two tags promoting out of order, would
# otherwise hand a clean checkout an older build with nothing reporting it.
# The version already on the image is the record of where latest stands, and
# only a registry saying latest does not exist may skip the comparison: any
# other failure leaves the question unanswered, which is not an answer.
serving_version() {
  local image="$1" out
  if out="$(skopeo inspect --format '{{ index .Labels "org.opencontainers.image.version" }}' \
      "docker://$image:latest" 2>&1)"; then
    printf '%s' "$out"
    return 0
  fi
  if printf '%s' "$out" | grep -qiE 'manifest unknown|manifest_unknown|name unknown|name_unknown|repository name not known'; then
    return 0
  fi
  echo "$image:latest could not be read, so whether latest would move backwards is unknown" >&2
  printf '%s\n' "$out" >&2
  return 1
}

while read -r service digest; do
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "$service has an invalid digest" >&2; exit 1; }
  image="$IMAGE_ROOT/$service"
  serving="$(serving_version "$image")"
  [ -n "$serving" ] || continue
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
