FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system semanticdog \
    && useradd --system --create-home --gid semanticdog semanticdog

WORKDIR /app

COPY pyproject.toml README.md uv.lock config.example.yaml /app/
COPY semanticdog /app/semanticdog

RUN python -m pip install --upgrade pip \
    && python -m pip install .

RUN mkdir -p /data/config /data/state /data/logs \
    && chown -R semanticdog:semanticdog /app /data

USER semanticdog

EXPOSE 9090
VOLUME ["/data/config", "/data/state", "/data/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "from semanticdog.cli import _find_config; from semanticdog.runtime import load_runtime; import urllib.request; runtime = load_runtime(_find_config()); port = runtime.cfg.http_port if runtime.cfg else 9090; urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).read()"

CMD ["sdog", "serve", "--host", "0.0.0.0"]
