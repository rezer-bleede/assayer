# syntax=docker/dockerfile:1.7
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./

# Keep pyproject.toml as the single source of truth while installing third-party
# dependencies before application code, so ordinary source changes reuse this layer.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -c 'import tomllib; p=tomllib.load(open("pyproject.toml", "rb")); deps=p["project"]["dependencies"] + sum((p["project"]["optional-dependencies"][name] for name in ("mysql", "s3", "dev", "ui")), []) + [p["build-system"]["requires"][0]]; open("/tmp/requirements.txt", "w").write("\n".join(deps) + "\n")' \
    && pip install --upgrade pip setuptools wheel \
    && pip install --prefer-binary --timeout 120 --retries 8 -r /tmp/requirements.txt

RUN python -m playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# the clone workdir must be writable by the unprivileged app user
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/assayer-repos /data/ui-artifacts \
    && chown appuser:appuser /tmp/assayer-repos /data/ui-artifacts /app

# Browser binaries are large; keep them in a source-independent layer.
RUN python -m playwright install chromium \
    && chmod -R a+rX /ms-playwright

COPY --chown=appuser:appuser app ./app
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --no-deps --no-build-isolation .

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=6 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
