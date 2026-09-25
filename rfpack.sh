#!/bin/sh
# Portable launcher - keep this next to rfpack.py, copy both anywhere.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for PY in python3 python; do
  command -v "$PY" >/dev/null 2>&1 && exec "$PY" "$HERE/rfpack.py" "$@"
done
echo "Python 3.8+ was not found on this machine." >&2
exit 1
