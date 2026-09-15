#!/usr/bin/env bash
set -euo pipefail

: "${GH_TOKEN:?GitHub token is required}"
: "${GITHUB_REPOSITORY:?GitHub repository is required}"
: "${IMAGE_ROOT:?image repository root is required}"

version="$(gh api --paginate "repos/$GITHUB_REPOSITORY/releases?per_page=100" | jq -r '
  .[] | select(.draft == false and .prerelease == false)
  | .tag_name | select(test("^v[0-9]+\\.[0-9]+\\.[0-9]+$"))
' | sort -V | tail -n 1)"
test -n "$version" || { echo "no completed version release exists" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
gh release download "$version" --pattern digests.txt --dir "$work"
expected="$(printf '%s\n' store api git obsidian-sync)"
actual="$(awk '{ print $1 }' "$work/digests.txt")"
test "$actual" = "$expected" || { echo "$version does not record every service once" >&2; exit 1; }
while read -r service digest; do
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "$service has an invalid digest" >&2; exit 1; }
  image="$IMAGE_ROOT/$service"
  skopeo copy --all --preserve-digests "docker://$image@$digest" "docker://$image:latest"
  latest="sha256:$(skopeo inspect --raw "docker://$image:latest" | sha256sum | cut -d ' ' -f 1)"
  test "$latest" = "$digest" || { echo "$image:latest differs from $version" >&2; exit 1; }
done < "$work/digests.txt"

echo "latest now names every image from $version"
