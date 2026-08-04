# syntax=docker/dockerfile:1

# Native Apple-silicon build of the pinned FORCE submodule.  The upstream
# image is amd64-only; building this image avoids Docker/QEMU emulation.
FROM --platform=linux/arm64 ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        dos2unix \
        gdal-bin \
        libcurl4-openssl-dev \
        libgdal-dev \
        libgsl-dev \
        libjansson-dev \
        lockfile-progs \
        parallel \
        pkg-config \
        python3 \
        python3-gdal \
        rename \
        unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/force
COPY vendor/force/ ./

# FORCE 3.10.04 hard-codes Debian's x86_64 multiarch directory and its build
# gate checks tools needed by unrelated FORCE modules.  Adapt the paths for
# arm64 and limit that gate to dependencies required by TerraVault's L2PS and
# mosaic workflow.  The pinned submodule itself remains unmodified.
RUN multiarch="$(dpkg-architecture -qDEB_HOST_MULTIARCH)" \
    && sed -i "s#x86_64-linux-gnu#${multiarch}#g" Makefile \
    && sed -i \
        -e '/landsatlinks/d' \
        -e '/opencv_version/d' \
        -e '/pip3/d' \
        -e '/  - R$/d' \
        requirements.yml \
    && make -j"$(nproc)" force-l2ps force-info force-mdcp force-cube-init bash misc \
    && mkdir -p /opt/force/bin \
    && cp -a bin/. /opt/force/bin/ \
    && find /opt/force/bin -type f -maxdepth 1 -exec chmod 0755 {} +

# GNU Parallel's --eta assumes an interactive /dev/tty and reports a false
# 100% for a single busy scene. TerraVault supplies a durable heartbeat/status
# command, so keep child output line-buffered without the misleading display.
RUN sed -i 's/--eta/--line-buffer/' /opt/force/bin/force-level2

ENV PATH="/opt/force/bin:${PATH}"
ENV PARALLEL_HOME=/tmp/parallel

WORKDIR /data
CMD ["force-info"]
