# HEM-local Quartz solar forecast sidecar (#542) — linux/arm64 (Hetzner CAX11).
# Self-hosts Open Climate Fix's open-source site-level PV model so HEM has no
# external forecast-API dependency (the hosted token expired; open.quartz.solar
# remains the drop-in fallback because this container mirrors its schema).
#
# Python 3.11, NOT 3.12: quartz-solar-forecast → pv-site-prediction==0.1.19
# declares requires-python <3.12 (first arm64 build failed ResolutionImpossible
# on 3.12).
#
# Multi-stage (same pattern as the main HEM Dockerfile): the dependency tree
# contains sdists with C extensions and no aarch64 wheels (numcodecs via
# ocf-blosc2 — second build failure), so the builder stage carries gcc and the
# runtime stage stays slim.
FROM python:3.11-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# numcodecs (pulled via pv-site-prediction → ocf-blosc2) has no aarch64
# wheel for any version compatible with the pinned numpy 1.23.5, and its
# sdist hardcodes -msse2/-mavx2 unless these flags are set (third build
# failure: "gcc: error: unrecognized command-line option '-msse2'"). With
# them, the C codecs compile as generic portable code — the documented
# non-x86 build path in numcodecs' own setup.py.
ENV DISABLE_NUMCODECS_SSE2=1 \
    DISABLE_NUMCODECS_AVX2=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── runtime ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Model/weights caches live OUTSIDE the read-only rootfs — compose mounts a
# named volume at /cache so the one-time Hugging Face download survives
# restarts and redeploys.
ENV HF_HOME=/cache/hf \
    XDG_CACHE_HOME=/cache/xdg \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app.py .

ARG BUILD_SHA=unknown
RUN echo "${BUILD_SHA}" > /app/.git-sha

RUN useradd --uid 1001 --no-create-home quartz \
    && mkdir -p /cache && chown -R 1001:1001 /cache
USER 1001

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
