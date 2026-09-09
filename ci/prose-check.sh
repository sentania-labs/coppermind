#!/usr/bin/env bash
# House rule, enforced rather than remembered: no em-dashes anywhere in the
# tree. Chat, docs, code comments and commit messages all use a comma, a colon,
# parentheses or a period instead.
#
# Untracked but not ignored files are scanned too, so a developer sees the
# failure before the commit rather than in CI.
#
# The character is built from its code point so this file does not contain the
# thing it is looking for. uv.lock is skipped because its content comes from
# package metadata upstream, not from anyone here.
set -euo pipefail

emdash=$(printf '\xe2\x80\x94')
failed=0

while IFS= read -r -d '' file; do
    case "$file" in
        uv.lock) continue ;;
    esac
    [ -f "$file" ] || continue
    if grep -n -- "$emdash" "$file" 2>/dev/null; then
        echo "  ^ em-dash in $file" >&2
        failed=1
    fi
done < <(git ls-files -z --cached --others --exclude-standard)

if [ "$failed" -ne 0 ]; then
    echo "em-dashes found; use a comma, a colon, parentheses or a period" >&2
    exit 1
fi
echo "prose check passed: no em-dashes"
