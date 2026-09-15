#!/usr/bin/env bash
set -euo pipefail

: "${VERSION:?release version is required}"
: "${IMAGE_ROOT:?image repository root is required}"
: "${DIGESTS:?digest record is required}"
test -s "$DIGESTS" || { echo "digest record is empty" >&2; exit 1; }
expected="$(printf '%s\n' store api git obsidian-sync)"
actual="$(awk '{ print $1 }' "$DIGESTS")"
test "$actual" = "$expected" || { echo "digest record does not name every service once" >&2; exit 1; }

# Remove the publishing credential before every registry read below. A public
# release that only its publisher can pull is not a release operators can use.
docker logout ghcr.io >/dev/null 2>&1 || true
while read -r service digest; do
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "$service has an invalid digest" >&2; exit 1; }
  image="$IMAGE_ROOT/$service"
  published="sha256:$(skopeo inspect --raw "docker://$image:$VERSION" | sha256sum | cut -d ' ' -f 1)"
  test "$published" = "$digest" || { echo "$image:$VERSION differs from the release record" >&2; exit 1; }
  cosign verify "$image@$digest" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp '^https://github.com/sentania-labs/coppermind/' >/dev/null
  docker pull "$image:$VERSION" >/dev/null
done < "$DIGESTS"

COMPOSE_FILES="-f docker-compose.yml -f ci/docker-compose.published.yml" make smoke
