#!/usr/bin/env bash
set -euo pipefail

: "${VERSION:?release version is required}"
: "${IMAGE_ROOT:?image repository root is required}"
: "${DIGESTS:?digest record is required}"
test -s "$DIGESTS" || { echo "digest record is empty" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The quickstart runs whatever latest names, so latest may only move forward.
# A re-run of an older tag's job, or two tags promoting out of order, would
# otherwise hand a clean checkout an older build with nothing reporting it.
# The version already on the image is the record of where latest stands, so a
# registry saying latest does not exist is the only answer that may skip the
# comparison. Every other outcome leaves the question unanswered, and an
# unanswered question is not permission to move the tag.
while read -r service digest; do
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "$service has an invalid digest" >&2; exit 1; }
  image="$IMAGE_ROOT/$service"

  status=0
  skopeo inspect --format '{{ index .Labels "org.opencontainers.image.version" }}' \
    "docker://$image:latest" >"$work/label" 2>"$work/error" || status=$?

  if [ "$status" -ne 0 ]; then
    if grep -qiE 'manifest unknown|manifest_unknown|name unknown|name_unknown|repository name not known' "$work/error"; then
      continue
    fi
    echo "$image:latest could not be read (skopeo exit $status), so whether latest would move backwards is unknown" >&2
    cat "$work/error" >&2
    exit 1
  fi

  serving="$(tr -d '\r' <"$work/label")"
  if [[ ! "$serving" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "$image:latest exists but names no readable version, so whether latest would move backwards is unknown" >&2
    echo "its version label read as: $serving" >&2
    exit 1
  fi

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
