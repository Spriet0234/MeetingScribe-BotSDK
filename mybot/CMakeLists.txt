cmake_minimum_required(VERSION 3.10)
project(zoom_bot)

set(CMAKE_CXX_STANDARD 17)

# ─── Locate pkg‑config and GLib/GObject ─────────────────────────────────────────
find_package(PkgConfig REQUIRED)
pkg_check_modules(GLIB2 REQUIRED glib-2.0 gobject-2.0)

# Include GLib headers and apply any compile flags pkg-config suggests
include_directories(${GLIB2_INCLUDE_DIRS})
add_compile_options(${GLIB2_CFLAGS_OTHER})
# ────────────────────────────────────────────────────────────────────────────────

# Zoom SDK and Qt paths
enable_language(ASM)
set(SDK_ROOT "${CMAKE_SOURCE_DIR}/..")
set(ZOOM_LIB "${SDK_ROOT}/libmeetingsdk.so")
set(QT_LIB_DIR "${SDK_ROOT}/qt_libs/Qt/lib")
link_directories(${QT_LIB_DIR})

# Include directories
include_directories(
    ${SDK_ROOT}/include
    ${SDK_ROOT}/h
    ${SDK_ROOT}/include/zoom_sdk_components
)

# Source files
add_executable(zoom_bot main.cpp)

# Link against required libraries
target_link_libraries(zoom_bot
    ${ZOOM_LIB}

    # Qt dependencies
    ${QT_LIB_DIR}/libQt5Core.so
    ${QT_LIB_DIR}/libQt5Gui.so
    ${QT_LIB_DIR}/libQt5Widgets.so
    ${QT_LIB_DIR}/libQt5Network.so
    ${QT_LIB_DIR}/libQt5Qml.so
    ${QT_LIB_DIR}/libQt5Quick.so

    # Networking and threading
    curl
    pthread
    dl

    # GLib / GObject (main loop, event dispatch)
    ${GLIB2_LIBRARIES}
)

# JSON parsing headers
target_include_directories(zoom_bot PRIVATE /usr/include/nlohmann)
