# syntax=docker/dockerfile:1

# ---- build: produce a wheel and install it into an isolated virtualenv ----
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
RUN python -m venv /opt/venv
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN /opt/venv/bin/pip install .

# ---- runtime: slim image, non-root user, no build tooling ----
FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MYTUNNEL_HOST=0.0.0.0 \
    MYTUNNEL_PORT=8080
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin mytunnel
COPY --from=build /opt/venv /opt/venv
USER mytunnel
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"MYTUNNEL_PORT\"]}/healthz', timeout=2)"]
CMD ["mytunnel", "server"]
