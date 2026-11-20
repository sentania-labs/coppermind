# The files this shard runs, one path per line. Everything else this decision needs
# is derived from these lines, so the file list is the only thing to print.
#
# Usage:
#   bash ci/shard-list.sh                  # all files, in order
#   SHARDS=4 SHARD=2 bash ci/shard-list.sh
#
# The list is sorted by the shell, so every shard sees the same order. Each shard
# keeps the files whose position in that list satisfies `index % SHARDS == SHARD - 1`,
# an exact partition of the list, and so of the tests, since a file's tests are all
# collected together.
SHARDS="${SHARDS:-1}"
SHARD="${SHARD:-1}"

cd "$(dirname "$0")/.." || exit 1
i=0
for path in tests/integration/test_*.py; do
  if [ "$SHARDS" -le 1 ] || [ $((i % SHARDS)) -eq $((SHARD - 1)) ]; then
    printf '%s\n' "$path"
  fi
  i=$((i + 1))
done
