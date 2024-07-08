#!/bin/bash

set -euo pipefail

for path in "$@"; do
    commit_date=$(git log -1 --format="%ad" --date=raw -- "$path" | cut -d' ' -f1)
    touch -d "@$commit_date" "$path"
    echo "Set '$path' mtime to $(date -d "@$commit_date")"
done