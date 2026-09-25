#!/usr/bin/env bash
# Public URL for the demo site: starts site/serve.py (if not already up) and a Cloudflare quick tunnel.
# Prints the https://*.trycloudflare.com base URL and saves it to site/live/tunnel_url.txt.
# Ctrl-C stops the tunnel (and the server, if this script started it).
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8765}"
CF="${CLOUDFLARED:-$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)}"
SERVER_PID=""

cleanup() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/"; then
  echo "serve.py already running on :$PORT"
else
  mkdir -p site/live
  if command -v uv >/dev/null; then PY=(uv run python); else PY=(python3); fi
  "${PY[@]}" site/serve.py --port "$PORT" > site/live/serve.log 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 50); do curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.2; done
  echo "started serve.py on :$PORT (pid $SERVER_PID, log site/live/serve.log)"
fi

"$CF" tunnel --no-autoupdate --url "http://localhost:$PORT" 2>&1 | while IFS= read -r line; do
  echo "$line"
  if [[ "$line" =~ (https://[a-z0-9-]+\.trycloudflare\.com) ]]; then
    url="${BASH_REMATCH[1]}"
    echo "$url" > site/live/tunnel_url.txt
    echo
    echo "PUBLIC BASE URL: $url   (saved to site/live/tunnel_url.txt)"
    echo "  $url/vendors/tinybird/subprocessors      $url/vendors/tinybird/subprocessors.json"
    echo "  $url/vendors/tinybird/dpa                $url/vendors/tinybird/dpa.md"
    echo
  fi
done
