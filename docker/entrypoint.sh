#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: docker run <image> <meetingNumber> [passcode] [zakToken]"
  echo "Environment:"
  echo "  QT_QPA_PLATFORM=offscreen (default)"
  echo "  LD_LIBRARY_PATH includes Zoom SDK + Qt libs"
  echo "  Or set MEETING_NUMBER, MEETING_PASSCODE, MEETING_ZAK"
  exit 0
fi

SDK_ROOT=/app/MeetingScribe-BotSDK

# Optional one-time sync from a packaged SDK folder if explicitly requested.
if [[ "${SDK_SYNC:-}" == "copy" || "${SDK_SYNC:-}" == "move" ]]; then
  PKG_DIR=$(ls -d "$SDK_ROOT"/zoom-meeting-sdk-linux_* 2>/dev/null | head -n1 || true)
  if [[ -z "${PKG_DIR}" || ! -d "${PKG_DIR}" ]]; then
    echo "SDK_SYNC=$SDK_SYNC set, but no zoom-meeting-sdk-linux_* directory found under $SDK_ROOT" >&2
    exit 2
  fi
  echo "Syncing Zoom SDK from $(basename "$PKG_DIR") into $(basename "$SDK_ROOT")... (mode=${SDK_SYNC})"
  # Clean symlinks first
  for p in h qt_libs libmeetingsdk.so libmpg123.so libcml.so; do
    if [[ -L "$SDK_ROOT/$p" ]]; then rm -f "$SDK_ROOT/$p"; fi
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

# Validate required SDK layout; fail with a helpful message if missing.
missing=()
[[ -d "$SDK_ROOT/h" ]] || missing+=("h/")
[[ -d "$SDK_ROOT/qt_libs" ]] || missing+=("qt_libs/")
[[ -f "$SDK_ROOT/libmeetingsdk.so" ]] || missing+=("libmeetingsdk.so")
if (( ${#missing[@]} )); then
  echo "Missing Zoom SDK components: ${missing[*]} under $SDK_ROOT" >&2
  echo "Place the SDK contents there, or run with SDK_SYNC=copy|move if you still keep a zoom-meeting-sdk-linux_* folder." >&2
  exit 2
fi

mkdir -p "$SDK_ROOT/build"
cd "$SDK_ROOT/build"

# Always run an incremental build (fast when nothing changed)
cmake .. >/dev/null
cmake --build . -j"$(nproc)"

# Allow env-based invocation if no args are passed
if [[ $# -eq 0 ]]; then
  if [[ -z "${MEETING_NUMBER:-}" ]]; then
    echo "Error: no args provided and MEETING_NUMBER env not set." >&2
    exit 2
  fi
  set -- "${MEETING_NUMBER}" "${MEETING_PASSCODE:-}" "${MEETING_ZAK:-}"
fi

if [[ "${USE_XVFB:-}" == "1" ]]; then
  echo "Starting under Xvfb (headless X11)."
  export QT_QPA_PLATFORM=xcb
  exec xvfb-run -a -s "-screen 0 1280x720x24 +extension RANDR" ./zoom_bot "$@"
else
  exec ./zoom_bot "$@"
fi
