FROM python:3.12-slim

ARG TARGETARCH
ARG PMTILES_VERSION=1.31.2

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates libexpat1 \
 && rm -rf /var/lib/apt/lists/*

# The pmtiles command line tool (cuts regions out of the OpenStreetMap map)
RUN set -e; \
    case "$TARGETARCH" in arm64) ARCH=arm64 ;; *) ARCH=x86_64 ;; esac; \
    curl -fsSL "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin pmtiles; \
    pmtiles version

RUN pip install --no-cache-dir numpy pillow rasterio rio-tiler pystac-client pmtiles

WORKDIR /app
COPY scripts/ /app/
RUN chmod +x /app/*.sh

# The container only waits. You start the build jobs by hand (see README.md).
CMD ["sleep", "infinity"]
