"""HEM-local Quartz solar forecast sidecar (#542).

Thin FastAPI wrapper around Open Climate Fix's open-source
``quartz-solar-forecast`` model (MIT, xgboost ``gb`` model trained on ~25k UK
sites; pulls its NWP input from Open-Meteo itself — no API keys anywhere).

The endpoint schema deliberately MIRRORS the hosted ``open.quartz.solar``
``POST /forecast/`` contract, so HEM's client can point at either this
container (``http://hem-quartz:8000``) or the hosted service with nothing but
a URL change. See ``src/weather.py:_fetch_quartz_open_forecast``.

Model weights are fetched once into ``$HF_HOME`` (a named docker volume in
deploy/compose.yaml) and lazily loaded on the first request — /health stays
fast at boot so the compose healthcheck doesn't gate on the download.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hem-quartz")

app = FastAPI(title="HEM local Quartz solar forecast", version="1.0.0")

_model_lock = threading.Lock()
_model_loaded = False


class PVSiteIn(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    capacity_kwp: float = Field(gt=0)
    tilt: float = Field(default=35, ge=0, le=90)
    orientation: float = Field(default=180, ge=0, le=360)


class GenerationValue(BaseModel):
    timestamp: datetime
    generation: float


class ForecastRequest(BaseModel):
    site: PVSiteIn
    timestamp: datetime | None = None
    # Accepted for hosted-API compatibility. The local ``run_forecast`` entry
    # point does not take recent-generation input (that path needs the
    # package's inverter integrations), so it is ignored here — HEM's
    # calibration/bias stack covers the nowcast correction instead.
    live_generation: list[GenerationValue] | None = None


def _warm_model() -> None:
    """Background warm-up: the first ``run_forecast`` downloads model weights
    (Hugging Face → /cache volume) and the NWP frame — can take minutes on a
    cold volume. Doing it off-thread at boot means HEM's first real fetch
    (typically < 60 s timeout) hits a warm model instead of timing out once.
    """
    global _model_loaded
    try:
        from quartz_solar_forecast.forecast import run_forecast
        from quartz_solar_forecast.pydantic_models import PVSite
        with _model_lock:
            run_forecast(
                site=PVSite(latitude=51.5, longitude=-0.2, capacity_kwp=1.0),
                ts=datetime.now(UTC).replace(tzinfo=None),
            )
            _model_loaded = True
        logger.info("model warm-up complete")
    except Exception:  # pragma: no cover — warm-up is best-effort
        logger.exception("model warm-up failed (first request will retry)")


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_warm_model, name="model-warmup", daemon=True).start()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model_loaded": _model_loaded}


@app.post("/forecast/")
def forecast(req: ForecastRequest) -> dict:
    global _model_loaded
    try:
        from quartz_solar_forecast.forecast import run_forecast
        from quartz_solar_forecast.pydantic_models import PVSite
    except Exception as exc:  # pragma: no cover — import failure = broken image
        logger.exception("quartz-solar-forecast import failed")
        raise HTTPException(status_code=500, detail=f"model import failed: {exc}") from exc

    ts = req.timestamp or datetime.now(UTC)
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)  # package expects naive UTC
    # Defense-in-depth (#544 review): the model anchors its 15-min prediction
    # grid at this ts. Floor it so multi-plane callers always get identical,
    # quarter-aligned grids even if they forget to send a shared timestamp.
    ts = ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)

    site = PVSite(
        latitude=req.site.latitude,
        longitude=req.site.longitude,
        capacity_kwp=req.site.capacity_kwp,
        tilt=req.site.tilt,
        orientation=req.site.orientation,
    )

    # xgboost predict is not re-entrancy-hazardous, but the first call also
    # downloads + loads weights; serialize so concurrent first requests don't
    # double-download into the cache volume.
    with _model_lock:
        try:
            df = run_forecast(site=site, ts=ts)
        except Exception as exc:
            logger.exception("run_forecast failed")
            raise HTTPException(status_code=502, detail=f"forecast failed: {exc}") from exc
        _model_loaded = True

    col = "power_kw" if "power_kw" in df.columns else df.columns[0]
    preds = {
        idx.isoformat(): max(0.0, float(v))
        for idx, v in df[col].items()
    }
    return {
        "timestamp": ts.isoformat(),
        "predictions": {"power_kw": preds},
    }
