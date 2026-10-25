# syntax=docker/dockerfile:1
# Daily Brief: one image for both compose services (api, builder). Targets: `runtime` (default) and `test`.
FROM python:3.12-slim AS base

# Everything that moves is pinned here (uv in the COPY --from below); bump deliberately. WHISPER_MODEL must match [transcribe] model in your config.toml.
ARG NODE_VERSION=22.23.4
ARG CLAUDE_CODE_VERSION=2.1.310
ARG YTDLP_VERSION=2026.10.21

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISABLE_AUTOUPDATER=1 \
    HOME=/home/brief \
    CLAUDE_CONFIG_DIR=/home/brief/.claude \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# Node (Claude Code, and yt-dlp's JS runtime): the official tarball, matched to the CPU architecture
RUN arch="$(dpkg --print-architecture)"; case "$arch" in amd64) n=x64;; arm64) n=arm64;; *) echo "unsupported arch $arch" >&2; exit 1;; esac \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${n}.tar.gz" \
  | tar -xz -C /usr/local --strip-components=1 --exclude='*.md' --exclude=LICENSE \
 && node --version

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && npm cache clean --force \
 && pip install --no-cache-dir "yt-dlp==${YTDLP_VERSION}"
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv

# uid 1000 owns state; the claude-home volume mounts at CLAUDE_CONFIG_DIR (created here so it inherits the owner)
RUN groupadd -g 1000 brief && useradd -u 1000 -g 1000 -m -d /home/brief brief \
 && mkdir -p /home/brief/.claude /app/var && chown -R 1000:1000 /home/brief /app
WORKDIR /app
COPY pyproject.toml uv.lock ./


FROM base AS test
# runtime + dev deps + tests: `docker build --target test` (bin/release runs it before going live)
RUN uv sync --frozen
COPY daily_brief ./daily_brief
COPY prompts ./prompts
COPY brief ./brief
COPY config.example.toml ./
COPY tests ./tests
COPY ops ./ops
COPY demo ./demo
USER 1000:1000
CMD ["pytest", "-q", "-p", "no:warnings"]


FROM base AS runtime
ARG WHISPER_MODEL=small.en
RUN uv sync --frozen --no-dev
# Bake the Whisper model so the container never needs the network for it (HF_HUB_OFFLINE=1 below)
ENV HF_HOME=/opt/hf
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')" \
 && chmod -R a+rX /opt/hf
ENV HF_HUB_OFFLINE=1
COPY daily_brief ./daily_brief
COPY prompts ./prompts
COPY brief ./brief
# config.toml is not baked in: compose mounts yours at /app/config.toml
USER 1000:1000
EXPOSE 8765
CMD ["python", "-m", "daily_brief.api", "/app/config.toml"]
