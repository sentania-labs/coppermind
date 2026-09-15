#!/usr/bin/env bash
set -euo pipefail

: "${VERSION:?release version is required}"
: "${GITHUB_SHA:?build commit is required}"
: "${MAINLINE_REF:=origin/main}"

if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "release tag '$VERSION' must match vMAJOR.MINOR.PATCH" >&2
  exit 2
fi

remote="$(git ls-remote --tags origin "refs/tags/$VERSION" "refs/tags/$VERSION^{}")"
commit="$(printf '%s\n' "$remote" | awk '/\^\{\}$/ { print $1 }')"
if [ -z "$commit" ]; then
  echo "release tag $VERSION must exist on origin and be annotated" >&2
  exit 2
fi
if [ "$commit" != "$GITHUB_SHA" ]; then
  echo "release tag $VERSION belongs to $commit, not build $GITHUB_SHA" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$GITHUB_SHA" "$MAINLINE_REF"; then
  echo "release tag $VERSION is not reachable from $MAINLINE_REF" >&2
  exit 1
fi

echo "release tag $VERSION is annotated and reachable from $MAINLINE_REF"
