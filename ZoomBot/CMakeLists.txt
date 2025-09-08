cmake_minimum_required(VERSION 3.10)
project(ZoomBot CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

find_package(PkgConfig REQUIRED)
pkg_check_modules(GLIB REQUIRED glib-2.0)
find_package(CURL REQUIRED)

set(SDK_BASE "${CMAKE_SOURCE_DIR}/sdk")

if (EXISTS "${SDK_BASE}/include/zoom_sdk.h")
  set(SDK_INC "${SDK_BASE}/include")
elseif (EXISTS "${SDK_BASE}/h/zoom_sdk.h")
  set(SDK_INC "${SDK_BASE}/h")
else()
  message(FATAL_ERROR "zoom_sdk.h not found in ${SDK_BASE}/include or ${SDK_BASE}/h")
endif()

set(ZOOM_SDK_SO "${SDK_BASE}/lib/libmeetingsdk.so")
if (NOT EXISTS "${ZOOM_SDK_SO}")
  message(FATAL_ERROR "libmeetingsdk.so not found at ${ZOOM_SDK_SO}")
endif()

# ---- Include & link dirs -------------------------------------------
include_directories(
  ${GLIB_INCLUDE_DIRS}
  ${CURL_INCLUDE_DIRS}
  ${SDK_INC}
  ${SDK_INC}/rawdata
  ${SDK_INC}/meeting_service_components
)

link_directories(
  ${GLIB_LIBRARY_DIRS}
  "${SDK_BASE}/lib"
)

# ---- Target ---------------------------------------------------------
add_executable(zoom_bot
  ${CMAKE_SOURCE_DIR}/main.cpp
)

target_link_libraries(zoom_bot PRIVATE
  "${ZOOM_SDK_SO}"
  ${GLIB_LIBRARIES}
  pthread
  dl
)

if (TARGET CURL::libcurl)
  target_link_libraries(zoom_bot PRIVATE CURL::libcurl)
else()
  target_link_libraries(zoom_bot PRIVATE ${CURL_LIBRARIES})
endif()

target_compile_options(zoom_bot PRIVATE ${GLIB_CFLAGS_OTHER})
