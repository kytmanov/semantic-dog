FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip \
    && python -m pip install uv

WORKDIR /build

COPY pyproject.toml README.md uv.lock config.example.yaml LICENSE /build/
COPY semanticdog /build/semanticdog

RUN uv export --frozen --format requirements.txt --no-dev --no-editable --no-emit-project -o /tmp/requirements.txt \
    && python -m pip wheel --no-deps --wheel-dir /tmp/dist .


FROM python:3.13-slim

ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="SemanticDog" \
      org.opencontainers.image.description="File semantic integrity validator for NAS and media libraries" \
      org.opencontainers.image.source="https://github.com/kytmanov/semantic-dog" \
      org.opencontainers.image.url="https://github.com/kytmanov/semantic-dog" \
      org.opencontainers.image.documentation="https://github.com/kytmanov/semantic-dog#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with fixed UID/GID
RUN groupadd --system --gid 999 semanticdog \
    && useradd --system --uid 999 --gid 999 --create-home semanticdog

# Prevent privilege escalation
RUN echo 'semanticdog ALL=(ALL) NOPASSWD: !ALL' >> /etc/sudoers.d/semanticdog \
    && chmod 0440 /etc/sudoers.d/semanticdog

WORKDIR /app

COPY --from=builder /tmp/requirements.txt /tmp/requirements.txt
COPY --from=builder /tmp/dist /tmp/dist

RUN python -m pip install --upgrade pip \
    && python -m pip install --require-hashes -r /tmp/requirements.txt \
    && python -m pip install /tmp/dist/*.whl \
    && rm -rf /tmp/dist /tmp/requirements.txt

RUN mkdir -p /data/config /data/state /data/logs \
    && chown -R semanticdog:semanticdog /app /data

USER semanticdog

EXPOSE 8181
VOLUME ["/data/config", "/data/state", "/data/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; port = int(os.getenv('SDOG_HTTP_PORT', '8181')); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).read()"

CMD ["sdog", "serve", "--host", "0.0.0.0"]
