#!/bin/sh
set -eu

cd "$(dirname "$0")"

if command -v python3.12 >/dev/null 2>&1; then
    exec python3.12 run_workbench.py "$@"
fi
if python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
    exec python3 run_workbench.py "$@"
fi

printf '%s\n' 'Install Python 3.12 or later and make python3 available on PATH.' >&2
exit 1
