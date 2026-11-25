#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
if [ -x ./runtime/backbone ]; then
    exec ./runtime/backbone "$@"
fi
if command -v python3 >/dev/null 2>&1 &&
    python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null; then
    exec python3 ./terminal.py "$@"
fi
printf 'Python 3.10+ is required for the source version. The macOS Apple Silicon release includes its runtime.\n'
if [ -t 0 ]; then
    printf 'Press Enter to close. '
    read -r unused
fi
exit 1
