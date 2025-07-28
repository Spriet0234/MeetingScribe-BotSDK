cmake_minimum_required(VERSION 3.10)
project(zoom_bot)

set(CMAKE_CXX_STANDARD 17)

# Zoom SDK and Qt paths
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

# Link against required Qt5 and system libraries
target_link_libraries(zoom_bot
    ${ZOOM_LIB}
    ${QT_LIB_DIR}/libQt5Core.so
    ${QT_LIB_DIR}/libQt5Gui.so
    ${QT_LIB_DIR}/libQt5Widgets.so
    ${QT_LIB_DIR}/libQt5Network.so
    ${QT_LIB_DIR}/libQt5Qml.so
    ${QT_LIB_DIR}/libQt5Quick.so
    
    pthread
    dl
)

