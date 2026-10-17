# syntax=docker/dockerfile:1.7
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

# Every dependency ships a manylinux wheel, so no build toolchain is needed.
RUN pip install --upgrade pip setuptools wheel \
    && pip install --prefer-binary -e ".[mysql,s3,dev]"

# the clone workdir must be writable by the unprivileged app user
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/qualitest-repos \
    && chown -R appuser:appuser /tmp/qualitest-repos /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=6 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]