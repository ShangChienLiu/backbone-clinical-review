#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
for python in "$(command -v python3 || true)" \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [ -x "$python" ] || continue
    if [ "$python" = /usr/bin/python3 ] && [ -x /usr/bin/xcode-select ] &&
        ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
        continue
    fi
    if "$python" -c 'import sqlite3, sys; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null; then
        exec "$python" ./terminal.py "$@"
    fi
done
printf 'Install Python 3.10 or newer, then reopen Start.command.\n'
printf 'Official Mac installers: https://www.python.org/downloads/macos/\n'
if [ -t 0 ]; then
    printf 'Press Enter to close. '
    read -r unused
fi
exit 1
