#!/bin/sh
set -eu
exec "$(dirname -- "$0")/compose.sh" exec -T control python /opt/project/deploy/healthcheck.py --require-chatgpt
