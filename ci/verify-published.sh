#!/usr/bin/env bash
set -euo pipefail

: "${VERSION:?release version is required}"
: "${IMAGE_ROOT:?image repository root is required}"
: "${GITHUB_REPOSITORY:?GitHub repository is required}"
: "${DIGESTS:?digest record is required}"
test -s "$DIGESTS" || { echo "digest record is empty" >&2; exit 1; }
expected="$(printf '%s\n' store api admin git obsidian-sync)"
actual="$(awk '{ print $1 }' "$DIGESTS")"
test "$actual" = "$expected" || { echo "digest record does not name every service once" >&2; exit 1; }

# Each registry read below runs with an empty credential store, so a public
# release that only its publisher can pull fails here. The caller's own
# ghcr.io login and docker CLI plugins are left alone.
anonymous="$(mktemp -d)"
trap 'rm -rf "$anonymous"' EXIT
anon=(env "DOCKER_CONFIG=$anonymous" "REGISTRY_AUTH_FILE=$anonymous/auth.json")
while read -r service digest; do
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "$service has an invalid digest" >&2; exit 1; }
  image="$IMAGE_ROOT/$service"
  if ! "${anon[@]}" skopeo inspect --raw "docker://$image:$VERSION" >"$anonymous/manifest" 2>"$anonymous/error"; then
    cat "$anonymous/error" >&2
    if grep -qiE 'manifest unknown|not found|unauthorized|denied' "$anonymous/error"; then
      echo "$image:$VERSION was pushed but cannot be read anonymously." >&2
      echo "The likely cause is GHCR package visibility: a package is private" >&2
      echo "when it is first published and does not inherit the repository's" >&2
      echo "visibility. The remedy is to set the store, api, admin, git" >&2
      echo "and obsidian-sync packages to public once, then re-run this job." >&2
    fi
    exit 1
  fi
  published="sha256:$(sha256sum <"$anonymous/manifest" | cut -d ' ' -f 1)"
  test "$published" = "$digest" || { echo "$image:$VERSION differs from the release record" >&2; exit 1; }
  "${anon[@]}" cosign verify "$image@$digest" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp "^https://github.com/$GITHUB_REPOSITORY/" >/dev/null
  "${anon[@]}" docker pull "$image:$VERSION" >/dev/null
done < "$DIGESTS"

COMPOSE_FILES="-f docker-compose.yml -f ci/docker-compose.published.yml" make smoke
