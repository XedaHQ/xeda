# The image of Xeda's Docker tests of the bsc flows: bsc 2026.07.1, with g++ for Bluesim and the
# BDPI C sources, and Icarus Verilog. Build it locally; it is never pushed:
#   docker build -t xeda-test-bsc:2026.07.1 -f tests/docker/bsc.Dockerfile tests/docker
FROM ubuntu:24.04
ARG TARGETARCH
ARG BSC_VERSION=2026.07.1
ARG BSC_SHA256_AMD64=4e69030f3a28cb819192a3780e20fd174ff7caf30cacf1cdbf2195cce913a933
ARG BSC_SHA256_ARM64=da1950453cce9b249e48556fd8c7f3fb6f28867e14e25b7b036ff87f7c1db0fb
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl g++ make libgmp10 tcl8.6 libtcl8.6 iverilog \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) name="bsc-${BSC_VERSION}-ubuntu-24.04"; sum="$BSC_SHA256_AMD64" ;; \
      arm64) name="bsc-${BSC_VERSION}-ubuntu-24.04-arm"; sum="$BSC_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/bsc.tar.gz \
      "https://github.com/B-Lang-org/bsc/releases/download/${BSC_VERSION}/${name}.tar.gz"; \
    echo "$sum  /tmp/bsc.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/bsc; tar -xzf /tmp/bsc.tar.gz -C /opt/bsc --strip-components=1; rm /tmp/bsc.tar.gz
ENV PATH=/opt/bsc/bin:$PATH
