#!/usr/bin/env bash
set -euo pipefail

# =========================
# ZoomBot Entrypoint (full)
# =========================
# Modes:
#   - BOT_IDLE=1 : start in idle mode (controller on 127.0.0.1:7600)
#   - USE_XVFB=1 : run Qt under Xvfb (else offscreen)
#
# Key env (with defaults):
#   HTTP_CONTROL_ENABLED=1
#   HTTP_CONTROL_PORT=7601
#   IDLE_TCP_HOST=127.0.0.1
#   IDLE_TCP_PORT=7600
#   ASR_WS_URL=ws://127.0.0.1:1  (silenced by default for tests)
#   SDK_SYNC=copy|move (if you want to import zoom-meeting-sdk-linux_* into /app/ZoomBot/sdk)
#
# Assumptions:
#   - SDK is baked in the image at /app/ZoomBot/zoom-meeting-sdk-linux_*
#   - Dockerfile already registered ld.so paths:
#       /app/ZoomBot/sdk/lib
#       /app/ZoomBot/sdk/qt_libs/Qt/lib

# ---------- Help ----------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: docker run <image> [<meetingNumber> [passcode] [zakToken]]
Env:
  BOT_IDLE=1                          Start in idle mode (listen on 127.0.0.1:7600)
  HTTP_CONTROL_ENABLED=1              Enable HTTP gateway
  HTTP_CONTROL_PORT=7601              Gateway port
  IDLE_TCP_HOST=127.0.0.1             Controller host
  IDLE_TCP_PORT=7600                  Controller port
  USE_XVFB=1                          Run bot under Xvfb (Qt xcb)
  SDK_SYNC=copy|move                  Import zoom-meeting-sdk-linux_* into /app/ZoomBot/sdk at start
  ASR_WS_URL=ws://127.0.0.1:1         ASR bridge endpoint (set to a real ws://host:port/ws to enable)
  MEETING_NUMBER / MEETING_PASSCODE / MEETING_ZAK  (non-idle mode)
USAGE
  exit 0
fi

# ---------- Paths ----------
SDK_ROOT=/app/ZoomBot
SDK_DIR="$SDK_ROOT/sdk"
BUILD_DIR="$SDK_ROOT/build"

export HOME="${HOME:-/app}"
mkdir -p "$HOME/.config"
if [[ ! -f "$HOME/.config/zoomus.conf" ]]; then
  echo "system.audio.type=default" > "$HOME/.config/zoomus.conf"
fi

# ---------- Optional PulseAudio (no-op if missing) ----------
if command -v pulseaudio >/dev/null 2>&1; then
  pulseaudio --check >/dev/null 2>&1 || true
  pulseaudio --start --exit-idle-time=-1 >/dev/null 2>&1 || true
  if command -v pactl >/dev/null 2>&1; then
    pactl load-module module-null-sink sink_name=DummyOutput >/dev/null 2>&1 || true
    pactl set-default-sink DummyOutput >/dev/null 2>&1 || true
  fi
fi

# ---------- Import SDK if requested ----------
if [[ "${SDK_SYNC:-}" == "copy" || "${SDK_SYNC:-}" == "move" ]]; then
  PKG_DIR=$(ls -d "$SDK_ROOT"/zoom-meeting-sdk-linux_* 2>/dev/null | head -n1 || true)
  if [[ -z "${PKG_DIR}" || ! -d "${PKG_DIR}" ]]; then
    echo "SDK_SYNC=$SDK_SYNC set, but no zoom-meeting-sdk-linux_* directory under $SDK_ROOT" >&2
    exit 2
  fi
  echo "Syncing Zoom SDK from $(basename "$PKG_DIR") into sdk/ (mode=${SDK_SYNC})"
  mkdir -p "$SDK_DIR" "$SDK_DIR/lib"

  # headers (include/ or h/)
  if [[ -d "$PKG_DIR/include" ]]; then
    rm -rf "$SDK_DIR/include"
    [[ "$SDK_SYNC" == "move" ]] && mv -f "$PKG_DIR/include" "$SDK_DIR/" || cp -a "$PKG_DIR/include" "$SDK_DIR/"
  elif [[ -d "$PKG_DIR/h" ]]; then
    rm -rf "$SDK_DIR/h"
    [[ "$SDK_SYNC" == "move" ]] && mv -f "$PKG_DIR/h" "$SDK_DIR/" || cp -a "$PKG_DIR/h" "$SDK_DIR/"
  fi

  # qt libs
  if [[ -d "$PKG_DIR/qt_libs" ]]; then
    rm -rf "$SDK_DIR/qt_libs"
    [[ "$SDK_SYNC" == "move" ]] && mv -f "$PKG_DIR/qt_libs" "$SDK_DIR/" || cp -a "$PKG_DIR/qt_libs" "$SDK_DIR/"
  fi

  # core .so
  for lib in libmeetingsdk.so libmpg123.so libcml.so; do
    if [[ -f "$PKG_DIR/$lib" ]]; then
      [[ "$SDK_SYNC" == "move" ]] && mv -f "$PKG_DIR/$lib" "$SDK_DIR/lib/" || cp -a "$PKG_DIR/$lib" "$SDK_DIR/lib/"
    fi
  done
fi

# ---------- Verify SDK presence ----------
SDK_INC_DIR=""
for d in "$SDK_DIR/include" "$SDK_DIR/h"; do
  [[ -f "$d/zoom_sdk.h" ]] && SDK_INC_DIR="$d" && break
done
[[ -f "$SDK_DIR/lib/libmeetingsdk.so" ]] || { echo "Missing sdk/lib/libmeetingsdk.so"; exit 2; }
[[ -d "$SDK_DIR/qt_libs" ]] || { echo "Missing sdk/qt_libs"; exit 2; }
[[ -n "$SDK_INC_DIR" ]] || { echo "Missing headers (sdk/include or sdk/h)"; exit 2; }
[[ -f "$SDK_DIR/lib/libmeetingsdk.so.1" ]] || ln -sf libmeetingsdk.so "$SDK_DIR/lib/libmeetingsdk.so.1"

# ---------- Qt / Loader env for runtime ----------
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
export LD_LIBRARY_PATH="$SDK_DIR/lib:$SDK_DIR/qt_libs/Qt/lib:${LD_LIBRARY_PATH:-}"
export QT_PLUGIN_PATH="$SDK_DIR/qt_libs/Qt/plugins"
echo "[diag] LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
echo "[diag] QT_PLUGIN_PATH=$QT_PLUGIN_PATH"
ls -1 "$SDK_DIR/qt_libs/Qt/lib/libQt5Quick.so"* >/dev/null 2>&1 || echo "[diag] QtQuick not found under $SDK_DIR/qt_libs/Qt/lib"

# ---------- Build (idempotent) ----------
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
cmake .. >/dev/null
cmake --build . -j"$(nproc)"

# ---------- Node ASR bridge (silent by default for testing) ----------
NODE_BIN="$(command -v node || command -v nodejs || true)"
if [[ -z "$NODE_BIN" ]]; then
  echo "ERROR: node not found in PATH" >&2
  exit 2
fi
ASR_URL="${ASR_WS_URL:-ws://127.0.0.1:1}"
echo "[entrypoint] starting ASR bridge with $NODE_BIN at /app/ZoomBot/asr_stream_client.js (ASR_WS_URL=$ASR_URL)"
ASR_WS_URL="$ASR_URL" \
BOT_PCM_PORT="${BOT_PCM_PORT:-7000}" \
"$NODE_BIN" /app/ZoomBot/asr_stream_client.js &

# ---------- Decide run mode ----------
BOT_ARGS=()
if [[ "${BOT_IDLE:-0}" == "1" ]]; then
  BOT_ARGS+=( "--idle" )
fi
if [[ "${#BOT_ARGS[@]}" -eq 0 ]]; then
  if [[ $# -eq 0 && -z "${MEETING_NUMBER:-}" ]]; then
    echo "Error: no args provided and MEETING_NUMBER not set. Use BOT_IDLE=1 for idle mode." >&2
    exit 2
  fi
  # If no CLI args but MEETING_* env are present, transform them
  if [[ $# -eq 0 ]]; then
    set -- "${MEETING_NUMBER}" "${MEETING_PASSCODE:-}" "${MEETING_ZAK:-}"
  fi
fi

# ---------- Start bot first ----------
start_bot() {
  if [[ "${USE_XVFB:-}" == "1" ]]; then
    echo "Starting under Xvfb (headless X11)."
    export QT_QPA_PLATFORM=xcb
    xvfb-run -a -s "-screen 0 1280x720x24 +extension RANDR" ./zoom_bot "${BOT_ARGS[@]}" "$@"
  else
    export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
    ./zoom_bot "${BOT_ARGS[@]}" "$@"
  fi
}

start_bot "$@" &
BOT_PID=$!

# ---------- Wait for idle controller on :7600 ----------
IDLE_TCP_HOST="${IDLE_TCP_HOST:-127.0.0.1}"
IDLE_TCP_PORT="${IDLE_TCP_PORT:-7600}"
echo "[diag] waiting for idle controller at ${IDLE_TCP_HOST}:${IDLE_TCP_PORT}"
for i in $(seq 1 30); do
  if command -v nc >/dev/null 2>&1; then
    nc -z "$IDLE_TCP_HOST" "$IDLE_TCP_PORT" >/dev/null 2>&1 && { echo "[diag] idle controller up"; break; }
  else
    # bash /dev/tcp works because we run under bash
    (echo > /dev/tcp/"$IDLE_TCP_HOST"/"$IDLE_TCP_PORT") >/dev/null 2>&1 && { echo "[diag] idle controller up"; break; }
  fi
  echo "[diag] waiting (try $i/30)"; sleep 1
done

# ---------- Start HTTP gateway AFTER controller is up ----------
if [[ "${HTTP_CONTROL_ENABLED:-1}" == "1" ]]; then
  HTTP_CONTROL_PORT="${HTTP_CONTROL_PORT:-7601}"
  echo "[entrypoint] starting HTTP control gateway on :$HTTP_CONTROL_PORT -> ${IDLE_TCP_HOST}:${IDLE_TCP_PORT}"
  "$NODE_BIN" /app/ZoomBot/http_control_gateway.js &
fi

# ---------- Keep container alive with bot ----------
wait "$BOT_PID"
