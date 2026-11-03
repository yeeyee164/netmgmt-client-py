# Multi-stage Dockerfile for netmgnt-client-py
# Stage 1: Build C libraries (libyang) and Python virtual environment
FROM python:3.10-slim-bookworm AS builder

ARG LIBYANG_TAG=v5.4.9
ENV DEBIAN_FRONTEND=noninteractive

# Install build tools and C library development packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    pkg-config \
    libpcre2-dev \
    libxml2-dev \
    libxslt1-dev \
    libffi-dev \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Compile and install libyang (pinned to requested tag)
RUN git clone --depth 1 --branch ${LIBYANG_TAG} https://github.com/CESNET/libyang.git /tmp/libyang \
 && cd /tmp/libyang \
 && mkdir build && cd build \
 && cmake -D CMAKE_BUILD_TYPE=Release \
          -D ENABLE_BUILD_TESTS=OFF \
          -D CMAKE_INSTALL_PREFIX=/usr/local \
          -D CMAKE_INSTALL_LIBDIR=lib .. \
 && make -j$(nproc) \
 && make install \
 && ldconfig

# Create virtual environment and install Python dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r /tmp/requirements.txt


# Stage 2: Runtime image
FROM python:3.10-slim-bookworm AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# Install runtime C libraries and utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpcre2-8-0 \
    libxml2 \
    libxslt1.1 \
    libffi8 \
    openssh-client \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy compiled libyang libraries and headers
COPY --from=builder /usr/local/lib/libyang* /usr/local/lib/
COPY --from=builder /usr/local/include/libyang /usr/local/include/libyang
COPY --from=builder /usr/local/lib/pkgconfig/libyang.pc /usr/local/lib/pkgconfig/
RUN ldconfig

# Copy virtual environment
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH="/app"

WORKDIR /app

# Copy application source tree
COPY . /app

# Ensure entrypoint is executable
RUN chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]

