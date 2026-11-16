#!/bin/bash
# Set up (first time only) and start Referral First. Works on macOS and Linux.
#   bash run.sh
set -e
cd "$(dirname "$0")"

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9 or newer is needed and wasn't found."
  echo "Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up. This takes a few minutes."
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ! .venv/bin/python -m playwright install chromium; then
  echo
  echo "WARNING: couldn't download the browser the bot uses to fill in applications."
  echo "The app will still start, but the bot can't apply until this works."
  echo "Check your internet connection, then stop the app and run this script again."
fi

PORT="${PORT:-8000}"
echo
echo "Referral First is starting at http://127.0.0.1:$PORT"
echo "Leave this window open while you use it. Press Ctrl+C to stop."
echo
( sleep 3; command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$PORT" ) >/dev/null 2>&1 &
exec .venv/bin/python -m uvicorn app.main:app --port "$PORT"
