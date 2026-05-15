# Multi-stage Dockerfile for Matter RAG Pipeline
#
# Stage 1: Clone spec repo (lightweight, cached)
# Stage 2: Pipeline runtime (Python + deps + data)
#
# Build args:
#   SPEC_BRANCH  — spec repo branch (default: main)
#   GITHUB_TOKEN — for private repos (optional)

# ── Stage 1: Clone spec repo ──────────────────────────────────────────────
FROM alpine/git:latest AS spec-clone
ARG SPEC_BRANCH=master
ARG SPEC_URL=https://github.com/CHIP-Specifications/connectedhomeip-spec.git
ARG GITHUB_TOKEN=""
RUN if [ -n "$GITHUB_TOKEN" ]; then \
      CLONE_URL=$(echo "$SPEC_URL" | sed "s|https://github.com|https://x-access-token:${GITHUB_TOKEN}@github.com|"); \
    else \
      CLONE_URL="$SPEC_URL"; \
    fi && \
    git clone --depth=1 --branch "$SPEC_BRANCH" --single-branch "$CLONE_URL" /spec-repo

# ── Stage 2: Pipeline runtime ─────────────────────────────────────────────
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ruby ruby-dev build-essential && \
    gem install asciidoctor --no-document && \
    apt-get remove -y build-essential && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first (layer caching)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline source
COPY . /app/

# Copy spec repo from stage 1
COPY --from=spec-clone /spec-repo /app/spec-repo

# Copy pre-built data (KG, FAISS, DM XMLs) if available
# These are populated by build_docker_image.py before docker build
COPY data/ /app/data/

# Volume mount points for persistent data
VOLUME ["/app/data/faiss_index", "/app/data/knowledge_graph", "/app/data/input_doc", \
        "/app/reports", "/app/logs"]

# Environment
ENV MATTER_RAG_DOCKER=1
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
