#!/usr/bin/env sh
# Easy YT To Anything — one-click launcher for macOS / Linux.
cd "$(dirname "$0")" || exit 1
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
echo "Installing/updating dependencies..."
"$PY" -m pip install --upgrade -r requirements.txt
echo
echo "Starting server... a browser tab will open automatically."
exec "$PY" app.py "$@"
