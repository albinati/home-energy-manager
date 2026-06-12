# HEM-local Quartz solar forecast sidecar (#542) — linux/arm64 (Hetzner CAX11).
# Self-hosts Open Climate Fix's open-source site-level PV model so HEM has no
# external forecast-API dependency (the hosted token expired; open.quartz.solar
# remains the drop-in fallback because this container mirrors its schema).
#
# Python 3.11, NOT 3.12: quartz-solar-forecast → pv-site-prediction==0.1.19
# declares requires-python <3.12 (first arm64 build failed ResolutionImpossible
# on 3.12). Its pinned numpy 1.23.5 / scikit-learn 1.1.3 ship cp311 manylinux
# aarch64 wheels, so 3.11 resolves cleanly.
FROM python:3.11-slim

# Model/weights caches live OUTSIDE the read-only rootfs — compose mounts a
# named volume at /cache so the one-time Hugging Face download survives
# restarts and redeploys.
ENV HF_HOME=/cache/hf \
    XDG_CACHE_HOME=/cache/xdg \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ARG BUILD_SHA=unknown
RUN echo "${BUILD_SHA}" > /app/.git-sha

RUN useradd --uid 1001 --no-create-home quartz \
    && mkdir -p /cache && chown -R 1001:1001 /cache
USER 1001

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
