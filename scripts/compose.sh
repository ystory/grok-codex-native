#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -f "$project_dir/deploy/.env" ]; then
    echo 'Run scripts/prepare.sh with an absolute private data directory first.' >&2
    exit 1
fi
exec docker compose --env-file "$project_dir/deploy/.env" -f "$project_dir/deploy/compose.yaml" "$@"
