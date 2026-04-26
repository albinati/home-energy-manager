# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12-slim

FROM python:${PYTHON_VERSION} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt


FROM python:${PYTHON_VERSION} AS runtime

ARG GIT_SHA=unknown

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Europe/London

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1001 hem \
 && useradd  --system --uid 1001 --gid 1001 --home /app --shell /usr/sbin/nologin hem

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=hem:hem src/ /app/src/
COPY --chown=hem:hem bin/ /app/bin/
COPY --chown=hem:hem pyproject.toml requirements.txt /app/

RUN echo "${GIT_SHA}" > /app/.git-sha \
 && chown hem:hem /app/.git-sha

LABEL org.opencontainers.image.source="https://github.com/albinati/home-energy-manager" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.licenses="proprietary"

USER hem

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "src.cli", "serve"]
