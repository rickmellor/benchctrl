# benchctrl-vision sidecar: Axelera Metis runtime + Basler pylon + OpenCV, Ubuntu 24.04.
#
# Why a container: the Axelera runtime (axelera-runtime + the axelera-rt wheel)
# maps PCIe BARs and is packaged for Ubuntu 24.04 only; the bench boxes run
# Raspberry Pi OS (Debian) and Arch. Everything else in benchctrl stays on the
# host. Axelera publishes arm64 and amd64 in the same apt suite, so one
# Dockerfile builds on the Pi 5 and on a desktop.
#
# benchctrl itself is NOT copied in. The checkout's src/ is bind-mounted read-only
# at /opt/benchctrl/src (see run-vision.sh), so a `git pull` on the host is a
# restart, not a rebuild. Only pyserial is installed for the package's own
# imports; the vision code needs nothing else from the venv below.
#
#   docker build --build-arg AX_VERSION=1.8.0 -t benchctrl-vision:1.8.0 -f deploy/vision/Dockerfile .

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ARG AX_VERSION=1.8.0

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg \
      python3 python3-venv python3-pip \
      libgstreamer-plugins-base1.0-0 libgstreamer1.0-0 \
      libglib2.0-0 \
      pciutils kmod udev \
    && rm -rf /var/lib/apt/lists/*

# Axelera apt repo: runtime shared libs + device firmware artifacts (axdevice, axcmd).
RUN install -d /etc/apt/keyrings \
    && curl -fsSL https://software.axelera.ai/artifactory/api/security/keypair/axelera/public \
       | gpg --dearmor -o /etc/apt/keyrings/axelera.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/axelera.gpg] https://software.axelera.ai/artifactory/axelera-apt-source/ ubuntu24 main" \
       > /etc/apt/sources.list.d/axelera.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       axelera-runtime-${AX_VERSION} axelera-device-${AX_VERSION} \
    && rm -rf /var/lib/apt/lists/*

# The Python side: axelera-rt (pure Python, from Axelera's index), pypylon
# (Basler's wheel bundles the pylon transport layer), OpenCV headless, onnxruntime
# for the bundled post-process graph, pyserial for benchctrl's own imports.
RUN python3 -m venv /opt/axenv \
    && /opt/axenv/bin/pip install --no-cache-dir -U pip \
    && /opt/axenv/bin/pip install --no-cache-dir \
       --extra-index-url https://software.axelera.ai/artifactory/api/pypi/axelera-pypi/simple \
       axelera-rt pypylon numpy opencv-python-headless onnxruntime pyserial

# pypylon's bundled pylon transport layer links libxcb, X11 client libs and libGL
# even with no display — the import fails on a headless image without them.
# A layer of its own so a wheel bump does not invalidate the runtime layers.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libxcb1 libx11-6 libxext6 libgl1 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/opt/axenv/bin:/opt/axelera/${AX_VERSION}/bin:/opt/axelera/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/axelera/${AX_VERSION}/lib:/opt/axelera/lib
ENV PYTHONPATH=/opt/benchctrl/src
ENV PYTHONUNBUFFERED=1
WORKDIR /opt/benchctrl
ENTRYPOINT ["/opt/axenv/bin/python", "-m", "benchctrl.vision.server"]
