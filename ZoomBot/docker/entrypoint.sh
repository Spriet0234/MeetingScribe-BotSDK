#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] version=2025-10-05a (bridge-first, no-idle by default)"

# ---------- Paths ----------
SDK_ROOT=/app/ZoomBot
SDK_DIR="$SDK_ROOT/sdk"
BUILD_DIR="$SDK_ROOT/build"

export HOME="${HOME:-/app}"
mkdir -p "$HOME/.config"
[[ -f "$HOME/.config/zoomus.conf" ]] || echo "system.audio.type=default" > "$HOME/.config/zoomus.conf"

if command -v pulseaudio >/dev/null 2>&1; then
  pulseaudio --check >/dev/null 2>&1 || true
  pulseaudio --start --exit-idle-time=-1 >/dev/null 2>&1 || true
  if command -v pactl >/dev/null 2>&1; then
    pactl load-module module-null-sink sink_name=DummyOutput >/dev/null 2>&1 || true
    pactl set-default-sink DummyOutput >/dev/null 2>&1 || true
  fi
fi

if [[ "${SDK_SYNC:-}" == "copy" || "${SDK_SYNC:-}" == "move" ]]; then
  PKG_DIR=$(ls -d "$SDK_ROOT"/zoom-meeting-sdk-linux_* 2>/dev/null | head -n1 || true)
  [[ -n "$PKG_DIR" && -d "$PKG_DIR" ]] || { echo "SDK_SYNC=$SDK_SYNC set, but no SDK found"; exit 2; }
  echo "Syncing SDK from $(basename "$PKG_DIR") -> sdk/ (mode=$SDK_SYNC)"
  mkdir -p "$SDK_DIR" "$SDK_DIR/lib"
  [[ -d "$PKG_DIR/include" ]] && { rm -rf "$SDK_DIR/include"; cp -a "$PKG_DIR/include" "$SDK_DIR/" || true; }
  [[ -d "$PKG_DIR/h" ]] &&       { rm -rf "$SDK_DIR/h";       cp -a "$PKG_DIR/h"       "$SDK_DIR/" || true; }
  [[ -d "$PKG_DIR/qt_libs" ]] && { rm -rf "$SDK_DIR/qt_libs"; cp -a "$PKG_DIR/qt_libs" "$SDK_DIR/" || true; }
  for lib in libmeetingsdk.so libmpg123.so libcml.so; do
    [[ -f "$PKG_DIR/$lib" ]] && cp -a "$PKG_DIR/$lib" "$SDK_DIR/lib/"
  done
fi

SDK_INC_DIR=""
for d in "$SDK_DIR/include" "$SDK_DIR/h"; do
  [[ -f "$d/zoom_sdk.h" ]] && SDK_INC_DIR="$d" && break
done
[[ -f "$SDK_DIR/lib/libmeetingsdk.so" ]] || { echo "Missing libmeetingsdk.so"; exit 2; }
[[ -d "$SDK_DIR/qt_libs" ]] || { echo "Missing qt_libs"; exit 2; }
[[ -n "$SDK_INC_DIR" ]] || { echo "Missing headers"; exit 2; }
[[ -f "$SDK_DIR/lib/libmeetingsdk.so.1" ]] || ln -sf libmeetingsdk.so "$SDK_DIR/lib/libmeetingsdk.so.1"

export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
export LD_LIBRARY_PATH="$SDK_DIR/lib:$SDK_DIR/qt_libs/Qt/lib:${LD_LIBRARY_PATH:-}"
export QT_PLUGIN_PATH="$SDK_DIR/qt_libs/Qt/plugins"
printf "%s\n" "/app/ZoomBot/sdk/lib" "/app/ZoomBot/sdk/qt_libs/Qt/lib" > /etc/ld.so.conf.d/zoombot.conf
ldconfig

# ---------- Build ----------
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
cmake .. >/dev/null
cmake --build . -j"$(nproc)"

# ---------- Node ASR bridge (Deepgram / AssemblyAI / null) ----------
NODE_BIN="$(command -v node || command -v nodejs || true)"
[[ -n "$NODE_BIN" ]] || { echo "ERROR: node not found" >&2; exit 2; }

export QUIET="${QUIET:-0}"             
export LOG_LEVEL="${LOG_LEVEL:-info}"
export BOT_PCM_HOST="${BOT_PCM_HOST:-127.0.0.1}"
export BOT_PCM_PORT="${BOT_PCM_PORT:-7000}"
export ASR_PROVIDER="${ASR_PROVIDER:-deepgram}"

echo "[entrypoint] node=$($NODE_BIN -v 2>/dev/null || echo missing) cwd=$(pwd)"
echo "[entrypoint] ls /app/ZoomBot:"
ls -al /app/ZoomBot || true
echo "[entrypoint] ls node_modules (if present):"
ls -al /app/ZoomBot/node_modules 2>/dev/null || echo "(no node_modules visible)"

cd /app/ZoomBot

echo "[entrypoint] starting ASR bridge (provider=$ASR_PROVIDER, pcm=${BOT_PCM_HOST}:${BOT_PCM_PORT}, bucket=${S3_BUCKET:-unset})"
set +e
("$NODE_BIN" /app/ZoomBot/asr_stream_client.js) |& tee -a /var/log/asr_bridge.out &
ASR_PID=$!
set -e
echo "[entrypoint] ASR bridge pid=$ASR_PID (logs also in /var/log/asr_bridge.out)"

wait_for_port() {
  local host="$1" port="$2" max="$3" waited=0
  while ! nc -z "$host" "$port" 2>/dev/null; do
    sleep 0.5
    waited=$((waited+1))
    if (( waited >= max )); then
      echo "[entrypoint] ERROR: PCM bridge not listening on $host:$port after $((max/2))s"
      echo "----- last 120 lines of /var/log/asr_bridge.out -----"
      tail -n 120 /var/log/asr_bridge.out || true
      echo "-----------------------------------------------------"
      exit 3
    fi
  done
}
wait_for_port "$BOT_PCM_HOST" "$BOT_PCM_PORT" 40 
echo "[entrypoint] PCM bridge is live at ${BOT_PCM_HOST}:${BOT_PCM_PORT}"

: "${BOT_IDLE:=0}"  
BOT_ARGS=()
PASS_ARGS=()

if [[ "$BOT_IDLE" == "1" ]]; then
  BOT_ARGS+=( "--idle" )
else
  if [[ $# -eq 0 && -z "${MEETING_NUMBER:-}" ]]; then
    echo "Error: no args provided and MEETING_NUMBER not set. Use BOT_IDLE=1 for idle mode." >&2
    kill "$ASR_PID" 2>/dev/null || true
    exit 2
  fi
  if [[ $# -eq 0 ]]; then
    set -- "${MEETING_NUMBER}" "${MEETING_PASSCODE:-}" "${MEETING_ZAK:-}"
  fi
  PASS_ARGS=("$@")
fi

start_http_gateway_once() {
  [[ "${HTTP_CONTROL_ENABLED:-0}" != "1" ]] && return 0
  if [[ -n "${HTTP_PID:-}" ]] && kill -0 "${HTTP_PID}" 2>/dev/null; then return 0; fi
  local port="${HTTP_CONTROL_PORT:-7601}"
  echo "[entrypoint] starting HTTP control gateway on :$port"
  "$NODE_BIN" /app/ZoomBot/http_control_gateway.js &
  HTTP_PID=$!
}

start_bot_once() {
  if [[ "${USE_XVFB:-}" == "1" ]]; then
    QT_QPA_PLATFORM=xcb xvfb-run -a -s "-screen 0 1280x720x24 +extension RANDR" "$@"
  else
    QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}" "$@"
  fi
}

# Supervisor
: "${COREDUMP:=0}"; [[ "$COREDUMP" = "1" ]] && ulimit -c unlimited || ulimit -c 0
: "${SUPERVISE:=1}"

term_all() {
  echo "[supervisor] TERM received"
  kill -TERM 0 2>/dev/null || true
  wait || true
  exit 0
}
trap term_all INT TERM

if [[ "$SUPERVISE" = "0" ]]; then
  echo "[supervisor] one-shot mode (SUPERVISE=0)"
  start_http_gateway_once
QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}" "$BUILD_DIR/zoom_bot" "${BOT_ARGS[@]}" "${PASS_ARGS[@]}" || true
  echo "[supervisor] bot exited; stopping bridge and exiting…"
  kill "$ASR_PID" 2>/dev/null || true
  wait || true
  exit 0
fi

RESTART_BACKOFF="${RESTART_BACKOFF:-2}"
MAX_BACKOFF="${MAX_BACKOFF:-30}"

while true; do
  echo "[supervisor] starting bot cycle"
  set +e
  start_http_gateway_once
QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}" "$BUILD_DIR/zoom_bot" "${BOT_ARGS[@]}" "${PASS_ARGS[@]}" &
  BOT_PID=$!
  set -e

  rc=0
  wait "$BOT_PID" || rc=$?
  echo "[supervisor] zoom_bot exited with $rc. Restarting in ${RESTART_BACKOFF}s…"
  sleep "${RESTART_BACKOFF}"
  RESTART_BACKOFF=$(( RESTART_BACKOFF < MAX_BACKOFF ? RESTART_BACKOFF * 2 : MAX_BACKOFF ))
done
