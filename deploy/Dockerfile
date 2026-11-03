# syntax=docker/dockerfile:1.7
FROM node:26-bookworm-slim@sha256:bc0a2afd3b74ce12222fc8bab99271e66257f021078f5f4a5d687a77d280c1d9 AS native

ARG TARGETARCH
ARG APP_UID=1000
ARG APP_GID=1000
ARG DEBIAN_SNAPSHOT=20261101T000000Z
ENV DEBIAN_FRONTEND=noninteractive
RUN node -e 'require("node:fs").writeFileSync("/tmp/grok-codex-bootstrap-ca.pem", require("node:tls").rootCertificates.join("\n") + "\n")' \
    && sed -i "s|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g; s|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::https::CaInfo=/tmp/grok-codex-bootstrap-ca.pem -o Acquire::Check-Valid-Until=false update \
    && apt-get -o Acquire::https::CaInfo=/tmp/grok-codex-bootstrap-ca.pem install -y --no-install-recommends ca-certificates curl git openssh-client python3 python3-venv bash zsh jq ripgrep unzip xz-utils \
    && rm -f /tmp/grok-codex-bootstrap-ca.pem \
    && rm -rf /var/lib/apt/lists/* \
    && if getent group "$APP_GID" >/dev/null; then usermod --gid "$APP_GID" node; else groupmod --gid "$APP_GID" node; fi \
    && if [ "$APP_UID" != 1000 ]; then usermod --uid "$APP_UID" node; fi
COPY deploy/package.json deploy/package-lock.json /opt/codex-cli/
RUN npm ci --omit=dev --ignore-scripts --prefix /opt/codex-cli \
    && ln -s /opt/codex-cli/node_modules/.bin/codex /usr/local/bin/codex \
    && codex --version | grep -Fx 'codex-cli 0.166.0'
FROM native AS control
ARG TARGETARCH
RUN architecture="${TARGETARCH:-$(dpkg --print-architecture)}"; case "$architecture" in \
      amd64) asset=x86_64-unknown-linux-musl; checksum=924a39a86c50aeb0e5374ba2098391455b7997e43c1f9a2ea1ccd7e0eeb08eec ;; \
      arm64) asset=aarch64-unknown-linux-musl; checksum=a4ed418b149498596a4432daeb54c0867b94030123c4f713a40d218be112e1a9 ;; \
      *) echo 'Supported architectures are amd64 and arm64.' >&2; exit 1 ;; \
    esac \
    && curl --fail --show-error --location "https://github.com/vi/websocat/releases/download/v4.0.0-alpha3/websocat.$asset" --output /usr/local/bin/websocat4 \
    && echo "$checksum  /usr/local/bin/websocat4" | sha256sum --check - \
    && chmod 0755 /usr/local/bin/websocat4

COPY deploy/requirements.lock /opt/requirements.lock
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r /opt/requirements.lock
ENV PATH="/opt/venv/bin:${PATH}" CODEX_HOME=/var/lib/grok-codex/codex HOME=/home/node
COPY . /opt/project
RUN /opt/venv/bin/pip install --no-deps --no-build-isolation /opt/project \
    && mkdir -p /var/lib/grok-codex/codex /var/lib/grok-codex/artifacts /run/grok-codex \
    && chown -R node:node /var/lib/grok-codex /run/grok-codex
USER node
WORKDIR /opt/project
CMD ["codex", "app-server", "--listen", "unix:///run/grok-codex/control.sock"]

FROM native AS worker
ARG TARGETARCH
RUN apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential pkg-config python3-dev chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN architecture="${TARGETARCH:-$(dpkg --print-architecture)}"; case "$architecture" in \
      amd64) checksum=4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d ;; \
      arm64) checksum=ee32df2323c8847f5fac6cb6b4b69ca75828e9329a8f22f677021f7b3306ce63 ;; \
      *) exit 1 ;; \
    esac \
    && curl --fail --show-error --location "https://github.com/cli/cli/releases/download/v2.103.3/gh_2.103.3_linux_${architecture}.tar.gz" --output /tmp/gh.tar.gz \
    && echo "$checksum  /tmp/gh.tar.gz" | sha256sum --check - \
    && tar -xzf /tmp/gh.tar.gz -C /tmp \
    && install /tmp/gh_2.103.3_linux_${architecture}/bin/gh /usr/local/bin/gh \
    && rm -rf /tmp/gh.tar.gz /tmp/gh_2.103.3_linux_${architecture} \
    && mkdir -p /work/source /work/artifacts /home/node/.codex \
    && chown -R node:node /work /home/node
ENV HOME=/home/node CODEX_HOME=/home/node/.codex SHELL=/bin/bash
USER node
WORKDIR /work/source
CMD ["/bin/false"]
