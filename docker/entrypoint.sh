#!/usr/bin/env bash
set -euo pipefail

# --- Help -----------------------------------------------------------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: docker run <image> <meetingNumber> [passcode] [zakToken]
Environment:
  MEETING_NUMBER / MEETING_PASSCODE / MEETING_ZAK  (used if no CLI args)
  USE_XVFB=1      Run under a virtual X server (Qt needs X in some setups)
  QT_QPA_PLATFORM=offscreen (default)
  LD_LIBRARY_PATH includes Zoom SDK + Qt libs
USAGE
  exit 0
fi

# --- Paths ----------------------------------------------------------
SDK_ROOT=/app/MeetingScribe-BotSDK
BUILD_DIR="$SDK_ROOT/build"

# Ensure HOME is sane (matters for ~/.config/zoomus.conf and pulseaudio)
export HOME="${HOME:-/app}"
mkdir -p "$HOME/.config"

# Zoom SDK audio backend: force PulseAudio
if [[ ! -f "$HOME/.config/zoomus.conf" ]]; then
  echo "system.audio.type=default" > "$HOME/.config/zoomus.conf"
fi

# --- Start PulseAudio + create a null sink (virtual speaker) --------
if command -v pulseaudio >/dev/null 2>&1; then
  # Start per-user Pulse daemon and keep it alive
  pulseaudio --check >/dev/null 2>&1 || true
  pulseaudio --start --exit-idle-time=-1 >/dev/null 2>&1 || true

  # Provide a dummy output so JoinVoip succeeds in headless containers
  if command -v pactl >/dev/null 2>&1; then
    pactl load-module module-null-sink sink_name=DummyOutput >/dev/null 2>&1 || true
    pactl set-default-sink DummyOutput >/dev/null 2>&1 || true
  fi
fi

# --- Optional: sync packaged SDK bundle into expected layout --------
if [[ "${SDK_SYNC:-}" == "copy" || "${SDK_SYNC:-}" == "move" ]]; then
  PKG_DIR=$(ls -d "$SDK_ROOT"/zoom-meeting-sdk-linux_* 2>/dev/null | head -n1 || true)
  if [[ -z "${PKG_DIR}" || ! -d "${PKG_DIR}" ]]; then
    echo "SDK_SYNC=$SDK_SYNC set, but no zoom-meeting-sdk-linux_* directory found under $SDK_ROOT" >&2
    exit 2
  fi
  echo "Syncing Zoom SDK from $(basename "$PKG_DIR") into $(basename "$SDK_ROOT")... (mode=${SDK_SYNC})"
  for p in h qt_libs libmeetingsdk.so libmpg123.so libcml.so; do
    [[ -L "$SDK_ROOT/$p" ]] && rm -f "$SDK_ROOT/$p"
  done
  if [[ "$SDK_SYNC" == "move" ]]; then
    [[ -e "$PKG_DIR/h" ]] && mv -f "$PKG_DIR/h" "$SDK_ROOT/" || true
    [[ -e "$PKG_DIR/qt_libs" ]] && mv -f "$PKG_DIR/qt_libs" "$SDK_ROOT/" || true
    for lib in libmeetingsdk.so libmpg123.so libcml.so; do
      [[ -e "$PKG_DIR/$lib" ]] && mv -f "$PKG_DIR/$lib" "$SDK_ROOT/" || true
    done
  else
    [[ -e "$PKG_DIR/h" ]] && cp -a "$PKG_DIR/h" "$SDK_ROOT/" || true
    [[ -e "$PKG_DIR/qt_libs" ]] && cp -a "$PKG_DIR/qt_libs" "$SDK_ROOT/" || true
    for lib in libmeetingsdk.so libmpg123.so libcml.so; do
      [[ -e "$PKG_DIR/$lib" ]] && cp -a "$PKG_DIR/$lib" "$SDK_ROOT/" || true
    done
  fi
  ln -snf libmeetingsdk.so "$SDK_ROOT/libmeetingsdk.so.1"
fi

# --- Validate SDK layout -------------------------------------------
missing=()
[[ -d "$SDK_ROOT/h" ]] || missing+=("h/")
[[ -d "$SDK_ROOT/qt_libs" ]] || missing+=("qt_libs/")
[[ -f "$SDK_ROOT/libmeetingsdk.so" ]] || missing+=("libmeetingsdk.so")
if (( ${#missing[@]} )); then
  echo "Missing Zoom SDK components: ${missing[*]} under $SDK_ROOT" >&2
  echo "Place the SDK there, or run with SDK_SYNC=copy|move if a zoom-meeting-sdk-linux_* folder exists." >&2
  exit 2
fi

# --- Build (incremental) -------------------------------------------
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
cmake .. >/dev/null
cmake --build . -j"$(nproc)"

# --- Args / env fallbacks ------------------------------------------
if [[ $# -eq 0 ]]; then
  if [[ -z "${MEETING_NUMBER:-}" ]]; then
    echo "Error: no args provided and MEETING_NUMBER env not set." >&2
    exit 2
  fi
  set -- "${MEETING_NUMBER}" "${MEETING_PASSCODE:-}" "${MEETING_ZAK:-}"
fi

# --- Optional Xvfb (Qt sometimes prefers X) ------------------------
if [[ "${USE_XVFB:-}" == "1" ]]; then
  echo "Starting under Xvfb (headless X11)."
  export QT_QPA_PLATFORM=xcb
  exec xvfb-run -a -s "-screen 0 1280x720x24 +extension RANDR" ./zoom_bot "$@"
else
  exec ./zoom_bot "$@"
fi
