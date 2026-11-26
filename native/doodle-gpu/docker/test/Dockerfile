FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        gcc libc6-dev make libwayland-dev libegl-dev libgles-dev libegl-mesa0 libgl1-mesa-dri python3 \
    && rm -rf /var/lib/apt/lists/*
