#!/usr/bin/env bash
# The diff the GitHub code reviewer reads, and its size limit, in one place.
#
# Called by code-reviewer.yml, which reviews it, and by ci.yml, which fails a
# feature PR over the limit in seconds instead of after the review is asked for.
#
# Usage: scripts/review_diff.sh FROM HEAD OUT
# Writes the diff to OUT and prints `lines=N` and `max=M`, for $GITHUB_OUTPUT.
#
# The diff leaves out lock files, docs and CI config (owner, 2026-10-09: a
# review costs about 100k tokens). `scripts/` stays in: it holds the gate's own
# thresholds and the jobs CI runs against staging.
set -euo pipefail

# A diff larger than this is refused with a note rather than reviewed.
MAX_DIFF_LINES=3000

if [ "$#" -ne 3 ]; then
  echo "usage: $0 FROM HEAD OUT" >&2
  exit 2
fi
from=$1
head=$2
out=$3

git diff "$from" "$head" -- . \
  ':(exclude)uv.lock' ':(exclude)docs/**' ':(exclude)**/*.md' \
  ':(exclude).claude/**' ':(exclude).github/**' > "$out"
# The rulebook is markdown, so the filter above drops it; a change to the rules
# is exactly what must be reviewed, so it goes back in.
git diff "$from" "$head" -- AGENTS.md >> "$out"

echo "lines=$(wc -l < "$out" | tr -d ' ')"
echo "max=$MAX_DIFF_LINES"
