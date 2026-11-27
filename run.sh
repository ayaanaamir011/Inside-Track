#!/bin/bash
# Set up (first time only) and start Inside Track. Works on macOS and Linux.
#   bash run.sh                            start it and open it in your browser
#   bash run.sh --start-at-login           also start it in the background every time you log in,
#                                          so a Monday newsletter is never missed
#   bash run.sh --stop-starting-at-login   undo that
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"
PORT="${PORT:-8000}"
URL="http://127.0.0.1:$PORT"
LABEL="local.internship-app"        # name of the login item / background service
LOG="$HERE/data/app.log"            # where the background copy writes its messages

find_python() {
  PY=""
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PY="$candidate"
      return
    fi
  done
  echo "Python 3.9 or newer is needed and wasn't found."
  echo "Install it from https://www.python.org/downloads/ and run this again."
  exit 1
}

setup() {
  find_python
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
}

running() {   # is a copy of the app already answering on this port?
  command -v curl >/dev/null 2>&1 && curl -s -o /dev/null -m 3 "$URL/api/summary"
}

open_browser() {
  ( sleep "${1:-0}"
    if command -v open >/dev/null 2>&1; then open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"; fi
  ) >/dev/null 2>&1 &
}

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

autostart_file() { echo "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$LABEL.desktop"; }
unit_file() { echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$LABEL.service"; }
has_systemd() { command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; }

install_login() {
  mkdir -p "$HERE/data"
  case "$(uname -s)" in
    Darwin)
      local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
      mkdir -p "$HOME/Library/LaunchAgents"
      cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$(xml "$HERE/run.sh")</string>
    <string>--background</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml "$HERE")</string>
  <key>EnvironmentVariables</key>
  <dict><key>PORT</key><string>$(xml "$PORT")</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$(xml "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF
      launchctl unload "$plist" >/dev/null 2>&1 || true
      launchctl load -w "$plist"
      ;;
    Linux)
      if has_systemd; then
        local unit; unit="$(unit_file)"
        mkdir -p "$(dirname "$unit")"
        cat > "$unit" <<EOF
[Unit]
Description=Inside Track (internship applications)

[Service]
WorkingDirectory=$HERE
Environment=PORT=$PORT
ExecStart=/bin/bash "$HERE/run.sh" --background
Restart=on-failure
RestartSec=60
StandardOutput=append:$LOG
StandardError=append:$LOG

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now "$LABEL.service"
      else
        local desktop; desktop="$(autostart_file)"
        mkdir -p "$(dirname "$desktop")"
        cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Inside Track
Exec=/bin/bash "$HERE/run.sh" --background
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
        running || nohup /bin/bash "$HERE/run.sh" --background >>"$LOG" 2>&1 &
      fi
      ;;
    *)
      echo "Starting at login is only set up for macOS and Linux."
      exit 1
      ;;
  esac
}

remove_login() {
  case "$(uname -s)" in
    Darwin)
      local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
      if [ -f "$plist" ]; then
        launchctl unload -w "$plist" >/dev/null 2>&1 || true
        rm -f "$plist"
      fi
      ;;
    Linux)
      if [ -f "$(unit_file)" ]; then
        systemctl --user disable --now "$LABEL.service" >/dev/null 2>&1 || true
        rm -f "$(unit_file)"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
      fi
      if [ -f "$(autostart_file)" ]; then
        rm -f "$(autostart_file)"
        echo "The copy that is running now stops when you log out."
      fi
      ;;
  esac
}

case "${1:-}" in
  --background)
    # Started at login: no setup (you may be offline), no browser, nothing to read.
    echo "$(date): starting"
    if [ ! -x .venv/bin/python ]; then
      echo "Not set up yet. Run 'bash run.sh' once by hand."
      exit 0          # exiting cleanly stops the login item from retrying every minute
    fi
    if running; then
      echo "Already running at $URL."
      exit 0
    fi
    exec .venv/bin/python -m uvicorn app.main:app --port "$PORT"
    ;;
  --start-at-login)
    setup
    install_login
    echo
    echo "Done. Inside Track now starts in the background whenever you log in, and it is running now."
    echo "Open $URL to use it. Its messages go to data/app.log."
    echo "To undo: bash run.sh --stop-starting-at-login"
    open_browser 5
    exit 0
    ;;
  --stop-starting-at-login)
    remove_login
    echo "Inside Track no longer starts when you log in."
    exit 0
    ;;
  "")
    ;;
  *)
    echo "Usage: bash run.sh [--start-at-login | --stop-starting-at-login]"
    exit 2
    ;;
esac

setup
if running; then
  echo
  echo "Inside Track is already running at $URL (it may have started when you logged in). Opening it."
  open_browser 0
  exit 0
fi

echo
echo "Inside Track is starting at $URL"
echo "Leave this window open while you use it. Press Ctrl+C to stop."
echo "Tip: 'bash run.sh --start-at-login' starts it in the background whenever you log in,"
echo "so it can pick up Monday's newsletter without you opening it."
echo
open_browser 3
exec .venv/bin/python -m uvicorn app.main:app --port "$PORT"
