# syntax=docker/dockerfile:1
#
# The Controller, as one image.
#
#     docker build -f packaging/Dockerfile -t bystack .
#
# Built from the repository root, because it assembles all three halves of the
# product -- the agent, the dashboard and the Controller -- and each lives in a
# different directory. Nothing has to be built beforehand and no artifact is
# copied in from the host: a clean checkout is the whole input, which is the
# only version of "reproducible" that survives contact with a laptop.
#
# ## The one thing that is not obvious
#
# The wheel is built by `backend/hatch_build.py`, the same hook a release wheel
# goes through, by staging the agent and the dashboard into the two paths it
# already looks in. That is deliberate: an image with its own bespoke way of
# placing those files would be a second packaging path, and the first sign of
# it drifting would be a container whose local agent cannot be found.

# --------------------------------------------------------------------------
# The dashboard. Output is JavaScript, so it is architecture-independent and
# is built natively on the build host even when the image is not.
# --------------------------------------------------------------------------
FROM --platform=$BUILDPLATFORM docker.io/library/node:22-alpine AS web

WORKDIR /build
# The manifests alone first, so a change to a component does not reinstall
# the whole of npm.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --------------------------------------------------------------------------
# The agent. Built *for the target*, because it ends up inside the image and
# gets executed there.
#
# `rust:alpine` rather than the cross images `scripts/build-agent.sh` uses:
# there is nothing to cross-compile here, since buildx is already running this
# stage on (or emulating) the target platform, and a native alpine toolchain is
# a much smaller thing to depend on. The musl targets default to `crt-static`,
# so this comes out static without being asked.
# --------------------------------------------------------------------------
FROM docker.io/library/rust:1-alpine AS agent

RUN apk add --no-cache musl-dev
WORKDIR /build
COPY agent/ ./
# The `readelf` output goes through a file rather than a pipe on purpose: in a
# pipeline, a missing readelf leaves grep reading nothing, grep exits 1, and
# the leading `!` turns that into a pass. A check that a base image can silently
# retire by dropping binutils is not a check.
RUN cargo build --release --locked \
	&& readelf -l target/release/bystack-agent > /tmp/elf.txt \
	&& ! grep -q INTERP /tmp/elf.txt

# --------------------------------------------------------------------------
# The Controller, into a virtualenv that gets copied forward. Build tooling,
# pip's cache and the source tree all stay behind in this stage.
# --------------------------------------------------------------------------
FROM docker.io/library/python:3.12-slim-bookworm AS wheel

ARG TARGETARCH
# `hatch_build.py` names the binary after `uname -m` and already knows that
# Docker spells those two differently.
ENV BYSTACK_TARGET_ARCH=$TARGETARCH

COPY backend/ /src/backend/
COPY --from=agent /build/target/release/bystack-agent /src/dist/bystack-agent-placeholder
COPY --from=web /build/dist/ /src/frontend/dist/

RUN set -eux; \
	case "$TARGETARCH" in \
	amd64) arch=x86_64 ;; \
	arm64) arch=aarch64 ;; \
	*) echo "no agent naming known for TARGETARCH=$TARGETARCH" >&2; exit 1 ;; \
	esac; \
	mv /src/dist/bystack-agent-placeholder "/src/dist/bystack-agent-${arch}"; \
	python -m venv /opt/bystack; \
	/opt/bystack/bin/pip install --no-cache-dir --upgrade pip; \
	/opt/bystack/bin/pip install --no-cache-dir /src/backend

# --------------------------------------------------------------------------
# What actually ships.
# --------------------------------------------------------------------------
FROM docker.io/library/python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="ByStack Controller" \
	org.opencontainers.image.description="Live topology and control plane for Docker fleets" \
	org.opencontainers.image.source="https://github.com/yaliby/bystack" \
	org.opencontainers.image.licenses="Apache-2.0"

COPY --from=wheel /opt/bystack /opt/bystack

# A fixed uid, because the state directory is a volume and an image rebuild
# that renumbered its user would leave a CA private key nobody can read.
# 10001 is high enough not to collide with a distribution's own accounts.
RUN set -eux; \
	groupadd --gid 10001 bystack; \
	useradd --uid 10001 --gid 10001 --home-dir /var/lib/bystack --shell /usr/sbin/nologin bystack; \
	install -d -m 0700 -o bystack -g bystack /var/lib/bystack

# The one directory worth backing up (`config.py`, `agents.state_dir`): the CA
# private key, the enrolment registry, and -- since ADR-0012 landed a durable
# audit log -- `operations.jsonl`. Declared as a volume because a container
# that keeps its audit trail only until it is replaced is worse than one with
# no audit trail at all: it looks like a record and is not.
VOLUME ["/var/lib/bystack"]

COPY packaging/bystack.container.yaml /etc/bystack/bystack.yaml

USER bystack
WORKDIR /var/lib/bystack

# The browser's port and the fleet's. Two, for the reason MIGRATION section 3
# gives: one may sit behind an ordinary reverse proxy and the other must not be
# terminated by anything that would strip a client certificate.
EXPOSE 8000 8443

ENV PATH=/opt/bystack/bin:$PATH

# `bystack` rather than `python -m bystack`, so the process that receives
# SIGTERM is the one whose `serve()` owns it (`main.py`, commit 4143b5f).
# There is no init shim in front of it: it installs its own handlers, reaps
# exactly one child, and an extra pid 1 would only add a hop for the signal.
ENTRYPOINT ["bystack"]
CMD ["--config", "/etc/bystack/bystack.yaml"]
