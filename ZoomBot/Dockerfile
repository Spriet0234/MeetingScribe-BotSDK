# ZoomBot/Dockerfile
FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

# Core build/runtime deps
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    build-essential cmake pkg-config curl \
    libcurl4-openssl-dev \
    libglib2.0-dev libglib2.0-0 \
    libx11-6 libx11-xcb1 libxcb1 \
    libxext6 libxrender1 libxi6 libsm6 libxrandr2 libxfixes3 libxcursor1 \
    libxkbcommon-x11-0 \
    libxcb-randr0 libxcb-shm0 libxcb-image0 libxcb-keysyms1 libxcb-xfixes0 libxcb-shape0 libxcb-xtest0 \
    libgbm1 libdrm2 libgl1 \
    libdbus-1-3 libfontconfig1 libfreetype6 zlib1g libasound2 \
    pulseaudio pciutils \
    libgl1-mesa-dev libdrm-dev libgbm-dev \
    libxcb1-dev libxcb-randr0-dev libxcb-shm0-dev libxcb-image0-dev \
    libxcb-keysyms1-dev libxcb-xfixes0-dev libxcb-shape0-dev libxcb-xtest0-dev \
    xvfb xauth \
    nodejs npm \
    ; rm -rf /var/lib/apt/lists/*

RUN mkdir -p /usr/include/nlohmann \
    && curl -fsSL https://raw.githubusercontent.com/nlohmann/json/v3.11.3/single_include/nlohmann/json.hpp \
    -o /usr/include/nlohmann/json.hpp

# App
WORKDIR /app/ZoomBot
COPY . /app/ZoomBot

RUN mkdir -p /root/.config \
    && printf "%s\n" "system.audio.type=default" > /root/.config/zoomus.conf

ENV QT_QPA_PLATFORM=offscreen \
    LD_LIBRARY_PATH=/app/ZoomBot:/app/ZoomBot/qt_libs/Qt/lib \
    QT_PLUGIN_PATH=/app/ZoomBot/qt_libs/Qt/plugins

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

#For node
RUN cd /app/ZoomBot && npm init -y && npm install ws --omit=dev
RUN ln -s /usr/bin/nodejs /usr/local/bin/node



ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--help"]
