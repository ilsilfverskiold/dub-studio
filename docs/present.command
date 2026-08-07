#!/bin/zsh
# Double-click me to present.
# Serves this folder over http://localhost:8123 (like the app's server does)
# and opens the presentation in your default browser. Ctrl+C here to stop.
cd "$(dirname "$0")"
( sleep 1; open "http://localhost:8123/presentation.html" ) &
exec python3 -m http.server 8123 --bind 127.0.0.1
