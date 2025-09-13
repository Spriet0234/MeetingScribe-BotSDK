#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: docker run <image> <meetingNumber> [passcode] [zakToken]
Environment:
  MEETING_NUMBER / MEETING_PASSCODE / MEETING_ZAK  (used if no CLI args)
  USE_XVFB=1      Run under a virtual X server (Qt sometimes prefers X)
  QT_QPA_PLATFORM=offscreen (default)
  SDK_SYNC=copy|move  If a zoom-meeting-sdk-linux_* is mounted under /app/ZoomBot,
                     import it into /app/ZoomBot/sdk.
USAGE
  exit 0
fi

SDK_ROOT=/app/ZoomBot
SDK_DIR="$SDK_ROOT/sdk"         
BUILD_DIR="$SDK_ROOT/build"

export HOME="${HOME:-/app}"
mkdir -p "$HOME/.config"

if [[ ! -f "$HOME/.config/zoomus.conf" ]]; then
  echo "system.audio.type=default" > "$HOME/.config/zoomus.conf"
fi

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
  if [[ -z "${PKG_DIR}" || ! -d "${PKG_DIR}" ]]; then
    echo "SDK_SYNC=$SDK_SYNC set, but no zoom-meeting-sdk-linux_* directory found under $SDK_ROOT" >&2
    exit 2
  fi
  echo "Syncing Zoom SDK from $(basename "$PKG_DIR") into sdk/ (mode=${SDK_SYNC})"

  mkdir -p "$SDK_DIR" "$SDK_DIR/lib"

  if [[ -d "$PKG_DIR/include" ]]; then
    rm -rf "$SDK_DIR/include"
    [[ "$SDK_SYNC" == "move" ]] && mv -f "$PKG_DIR/include" "$SDK_DIR/" || cp -a "$PKG_DIR/include" "$SDK_DIR/"
  elif [[ -d "$PKG_DIR/h" ]]; then
    rm -rf "$SDK_DIR/h"
    [[ "$SDK_SYNC" == "move" ]] && mv -f "$PKG_DIR/h" "$SDK_DIR/" || cp -a "$PKG_DIR/h" "$SDK_DIR/"
  fi

  if [[ -d "$PKG_DIR/qt_libs" ]]; then
    rm -rf "$SDK_DIR/qt_libs"
    [[ "$SDK_SYNC" == "move" ]] && mv -f "$PKG_DIR/qt_libs" "$SDK_DIR/" || cp -a "$PKG_DIR/qt_libs" "$SDK_DIR/"
  fi

  for lib in libmeetingsdk.so libmpg123.so libcml.so; do
    if [[ -f "$PKG_DIR/$lib" ]]; then
      [[ "$SDK_SYNC" == "move" ]] && mv -f "$PKG_DIR/$lib" "$SDK_DIR/lib/" || cp -a "$PKG_DIR/$lib" "$SDK_DIR/lib/"
    fi
  done
fi

SDK_INC_DIR=""
for d in "$SDK_DIR/include" "$SDK_DIR/h"; do
  if [[ -f "$d/zoom_sdk.h" ]]; then SDK_INC_DIR="$d"; break; fi
done

# libmeetingsdk.so (sdk/lib/libmeetingsdk.so)
SDK_SO=""
if [[ -f "$SDK_DIR/lib/libmeetingsdk.so" ]]; then
  SDK_SO="$SDK_DIR/lib/libmeetingsdk.so"
fi

missing=()
[[ -n "$SDK_INC_DIR" ]]     || missing+=("headers (sdk/include or sdk/h)")
[[ -d "$SDK_DIR/qt_libs" ]] || missing+=("sdk/qt_libs")
[[ -n "$SDK_SO" ]]          || missing+=("sdk/lib/libmeetingsdk.so")
if (( ${#missing[@]} )); then
  echo "Missing Zoom SDK components under $SDK_DIR: ${missing[*]}" >&2
  echo "Place them there, or mount the unzipped SDK under /app/ZoomBot and run with SDK_SYNC=copy|move." >&2
  exit 2
fi

if [[ -f "$SDK_DIR/lib/libmeetingsdk.so" && ! -e "$SDK_DIR/lib/libmeetingsdk.so.1" ]]; then
  ln -s libmeetingsdk.so "$SDK_DIR/lib/libmeetingsdk.so.1"
fi

export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
export LD_LIBRARY_PATH="$SDK_DIR/lib:$SDK_DIR/qt_libs/Qt/lib:${LD_LIBRARY_PATH:-}"
export QT_PLUGIN_PATH="$SDK_DIR/qt_libs/Qt/plugins"
export OUT_DIR="${OUT_DIR:-$BUILD_DIR}"

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
cmake .. >/dev/null
cmake --build . -j"$(nproc)"

if [[ $# -eq 0 ]]; then
  if [[ -z "${MEETING_NUMBER:-}" ]]; then
    echo "Error: no args provided and MEETING_NUMBER env not set." >&2
    exit 2
  fi
  set -- "${MEETING_NUMBER}" "${MEETING_PASSCODE:-}" "${MEETING_ZAK:-}"
fi

NODE_BIN="$(command -v node || command -v nodejs || true)"
if [[ -z "$NODE_BIN" ]]; then
  echo "ERROR: node not found in PATH" >&2
  exit 2
fi

echo "[entrypoint] starting ASR bridge with $NODE_BIN at /app/ZoomBot/asr_stream_client.js"
ASR_WS_URL="${ASR_WS_URL:-ws://host.docker.internal:8080/ws}" \
BOT_PCM_PORT="${BOT_PCM_PORT:-7000}" \
"$NODE_BIN" /app/ZoomBot/asr_stream_client.js &

if [[ "${USE_XVFB:-}" == "1" ]]; then
  echo "Starting under Xvfb (headless X11)."
  export QT_QPA_PLATFORM=xcb
  exec xvfb-run -a -s "-screen 0 1280x720x24 +extension RANDR" ./zoom_bot "$@"
else
  exec ./zoom_bot "$@"
fi
