#!/usr/bin/env bash
set -euo pipefail

DISPLAY="${DISPLAY:-:99}"
PORT="${PORT:-8080}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5900}"
SCREEN_WIDTH="${SCREEN_WIDTH:-1440}"
SCREEN_HEIGHT="${SCREEN_HEIGHT:-900}"

cleanup() {
  set +e
  [[ -n "${UVICORN_PID:-}" ]] && kill "$UVICORN_PID" >/dev/null 2>&1
  [[ -n "${NOVNC_PID:-}" ]] && kill "$NOVNC_PID" >/dev/null 2>&1
  [[ -n "${X11VNC_PID:-}" ]] && kill "$X11VNC_PID" >/dev/null 2>&1
  [[ -n "${FLUXBOX_PID:-}" ]] && kill "$FLUXBOX_PID" >/dev/null 2>&1
  [[ -n "${XVFB_PID:-}" ]] && kill "$XVFB_PID" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

echo "[1/5] Starting Xvfb on ${DISPLAY} (${SCREEN_WIDTH}x${SCREEN_HEIGHT})"
Xvfb "$DISPLAY" -screen 0 "${SCREEN_WIDTH}x${SCREEN_HEIGHT}x24" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 0.8

export DISPLAY

echo "[2/5] Starting Fluxbox window manager"
fluxbox >/tmp/fluxbox.log 2>&1 &
FLUXBOX_PID=$!
sleep 0.5

echo "[3/5] Starting x11vnc on port ${VNC_PORT}"
x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -forever -shared -nopw -xkb >/tmp/x11vnc.log 2>&1 &
X11VNC_PID=$!
sleep 0.8

NOVNC_WEB_ROOT="/usr/share/novnc"
if [[ ! -d "$NOVNC_WEB_ROOT" ]]; then
  echo "ERROR: noVNC web root not found at ${NOVNC_WEB_ROOT}"
  exit 1
fi

echo "[4/5] Starting noVNC on port ${NOVNC_PORT}"
websockify --web "$NOVNC_WEB_ROOT" "$NOVNC_PORT" "localhost:${VNC_PORT}" >/tmp/novnc.log 2>&1 &
NOVNC_PID=$!
sleep 0.5

echo "[5/5] Starting API server on port ${PORT} (headed browser mode)"
echo "Open noVNC at: http://localhost:${NOVNC_PORT}/vnc.html"
echo "Open app at:    http://localhost:${PORT}"

uvicorn gemini.server:app --host 0.0.0.0 --port "$PORT" --workers 1 &
UVICORN_PID=$!

wait "$UVICORN_PID"
