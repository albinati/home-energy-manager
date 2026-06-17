#!/usr/bin/env python
"""Offline feasibility study: can ML beat the v2 median LOAD forecast? — read-only.

The user asked whether to enhance the household LOAD/CONSUMPTION model with ML. A
median load profile (`db.residual_load_profile_v2`) already exists, and a prior
additive corrector (`load_bias.py`) was rejected because it was worse out-of-sample
— the documented diagnosis being that the dominant error is heat-pump *timing*
(→ #540), not the residual occupancy load. Before committing to ML infrastructure
(the prod image has no ML stack), this script answers two questions with data:

  1. Does an ML model beat the v2 median residual-load forecast **out-of-sample**?
  2. Is the reducible error in the **residual (occupancy)** load or in **heat-pump
     timing** (high-Daikin slots)?

It runs LOCALLY against a copy of the prod DB, in the dev venv where scikit-learn is
available (requirements-research.txt). It NEVER writes to the DB and touches no
production code. Output: per-hour MAE/bias tables for v2 vs ML candidates, the
residual-vs-heat-pump decomposition, and a verdict.

    .venv/bin/python scripts/research/load_ml_feasibility.py \
        --db scripts/research/.data/energy_state.db

Data limits (current prod): ~106 days of measured load, but Daikin split only
~45 days and weather only ~30 days (retention). So this is a DIRECTIONAL read,
not a definitive benchmark — re-run as history accrues.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# Repo root on sys.path so `import src.*` works when run as a file.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import statistics as st
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

SLOT_MIN = 30
LOW_DAIKIN_KWH = 0.10  # a slot is "residual-dominated" when est. heat-pump <= this


def _connect_src_db(db_path: str):
    """Point src.db at the DB COPY and import it, so we reuse the EXACT production
    baseline (residual_load_profile_v2 + lookup) and the trapezoidal load roll-up.
    Set DB_PATH before importing config so it binds to the copy."""
    os.environ["DB_PATH"] = os.path.abspath(db_path)
    os.environ.setdefault("OPENCLAW_READ_ONLY", "true")
    from src import db as _db  # noqa: E402
    from src.config import config as _config  # noqa: E402
    return _db, _config


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_load_series(_db, day_lo, day_hi) -> dict[str, float]:
    """Measured household load kWh per UTC half-hour slot, via the production
    trapezoidal roll-up (`_half_hourly_grid_kwh_for_day`, load_power_kw)."""
    out: dict[str, float] = {}
    d = day_lo
    while d <= day_hi:
        try:
            for k, v in _db._half_hourly_grid_kwh_for_day(d, "load_power_kw").items():
                out[_iso_z(datetime.fromisoformat(k.replace("Z", "+00:00")))] = float(v)
        except Exception:
            pass
        d += timedelta(days=1)
    return out


def build_hp_series(_db, day_lo, day_hi, tz) -> dict[str, float]:
    """Estimated heat-pump kWh per UTC half-hour slot, distributing each measured
    2-hourly Daikin bucket (local TZ) evenly over its four half-hour slots."""
    rows = _db.get_daikin_consumption_2hourly_range(day_lo.isoformat(), day_hi.isoformat())
    # (local_date, bucket_idx) -> kwh_total
    bucket = {(r["date"], int(r["bucket_idx"])): float(r.get("kwh_total") or 0.0) for r in rows}
    out: dict[str, float] = {}
    # Walk every half-hour slot in the window, map to its local 2h bucket.
    t = datetime(day_lo.year, day_lo.month, day_lo.day, tzinfo=UTC)
    end = datetime(day_hi.year, day_hi.month, day_hi.day, tzinfo=UTC) + timedelta(days=1)
    while t < end:
        lt = t.astimezone(tz)
        key = (lt.date().isoformat(), lt.hour // 2)
        if key in bucket:
            out[_iso_z(t)] = bucket[key] / 4.0  # 4 half-hour slots per 2h bucket
        t += timedelta(minutes=SLOT_MIN)
    return out


def build_weather(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-slot causal nowcast: for each slot_time, the forecast row from the most
    recent fetch at/before that slot_time. NaN-friendly (GBM handles missing)."""
    cur = conn.execute(
        """SELECT slot_time, temp_c, cloud_cover_pct, solar_w_m2, forecast_fetch_at_utc
           FROM meteo_forecast_value ORDER BY slot_time, forecast_fetch_at_utc"""
    )
    best: dict[str, dict] = {}
    best_fetch: dict[str, str] = {}
    for slot_time, temp, cloud, solar, fetch in cur.fetchall():
        try:
            st_dt = datetime.fromisoformat(str(slot_time).replace("Z", "+00:00"))
            f_dt = datetime.fromisoformat(str(fetch).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if f_dt > st_dt:  # not yet available at slot time → not causal
            continue
        key = _iso_z(st_dt)
        if key not in best_fetch or fetch > best_fetch[key]:
            best_fetch[key] = fetch
            best[key] = {"temp_c": temp, "cloud_pct": cloud, "solar_wm2": solar}
    return best


def assemble(load, hp, weather, tz) -> pd.DataFrame:
    """One row per UTC slot with target (load, residual) + features."""
    keys = sorted(load)
    recs = []
    for k in keys:
        t = datetime.fromisoformat(k.replace("Z", "+00:00"))
        lt = t.astimezone(tz)
        w = weather.get(k, {})
        recs.append({
            "slot_utc": k, "t": t,
            "load_kwh": load[k],
            "hp_kwh": hp.get(k, np.nan),
            "dow": lt.weekday(),
            "hour": lt.hour,
            "half": 1 if lt.minute >= 30 else 0,
            "is_weekend": 1 if lt.weekday() >= 5 else 0,
            "slot_of_day": lt.hour * 2 + (1 if lt.minute >= 30 else 0),
            "month": lt.month,
            "temp_c": w.get("temp_c", np.nan),
            "cloud_pct": w.get("cloud_pct", np.nan),
            "solar_wm2": w.get("solar_wm2", np.nan),
        })
    df = pd.DataFrame(recs)
    df["residual_kwh"] = (df["load_kwh"] - df["hp_kwh"]).clip(lower=0.0)
    # cyclical encodings
    df["sod_sin"] = np.sin(2 * np.pi * df["slot_of_day"] / 48)
    df["sod_cos"] = np.cos(2 * np.pi * df["slot_of_day"] / 48)
    df["dow_sin"] = np.sin(2 * np.pi * df["dow"] / 7)
    df["dow_cos"] = np.cos(2 * np.pi * df["dow"] / 7)
    # causal lag features on the TOTAL load series, keyed by ISO string to avoid
    # tz-aware Timestamp matching pitfalls (same time-of-day, strictly past).
    by_iso = dict(zip(df["slot_utc"], df["load_kwh"]))
    def at(t, days):
        return by_iso.get(_iso_z(t - timedelta(days=days)), np.nan)
    df["lag_1d"] = df["t"].map(lambda x: at(x, 1))
    df["lag_7d"] = df["t"].map(lambda x: at(x, 7))
    for N, lbl in ((7, "roll7"), (28, "roll28")):
        vals = []
        for x in df["t"]:
            prior = [at(x, k) for k in range(1, N + 1)]
            prior = [p for p in prior if not (p is None or (isinstance(p, float) and np.isnan(p)))]
            vals.append(np.mean(prior) if prior else np.nan)
        df[lbl] = vals
    return df


def _mae(a, b):
    m = (~np.isnan(a)) & (~np.isnan(b))
    return float(np.mean(np.abs(a[m] - b[m]))) if m.any() else float("nan")


def _bias(a, b):
    m = (~np.isnan(a)) & (~np.isnan(b))
    return float(np.mean(a[m] - b[m])) if m.any() else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="scripts/research/.data/energy_state.db")
    ap.add_argument("--test-frac", type=float, default=0.30, help="chronological holdout fraction")
    ap.add_argument("--report", default="docs/research/load_ml_feasibility_raw.md")
    args = ap.parse_args()

    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline

    _db, _config = _connect_src_db(args.db)
    tz = ZoneInfo(getattr(_config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    conn = sqlite3.connect(args.db)

    # Window = where the Daikin split exists (residual decomposition needs it).
    hp_lo, hp_hi = conn.execute("SELECT min(date), max(date) FROM daikin_consumption_2hourly").fetchone()
    day_lo = datetime.fromisoformat(hp_lo).date()
    day_hi = datetime.fromisoformat(hp_hi).date()
    print(f"HP-split window: {day_lo} .. {day_hi}")

    load = build_load_series(_db, day_lo, day_hi)
    hp = build_hp_series(_db, day_lo, day_hi, tz)
    weather = build_weather(conn)
    print(f"slots: load={len(load)} hp={len(hp)} weather={len(weather)}")

    df = assemble(load, hp, weather, tz).dropna(subset=["load_kwh", "residual_kwh"]).reset_index(drop=True)
    df = df.sort_values("t").reset_index(drop=True)
    n = len(df)
    split = int(n * (1 - args.test_frac))
    train, test = df.iloc[:split], df.iloc[split:]
    print(f"rows={n}  train {train['t'].min()}..{train['t'].max()}  test {test['t'].min()}..{test['t'].max()}")

    train_end_date = train["t"].max().astimezone(tz).date().isoformat()

    # --- Baseline A: production v2 median residual profile, as-of train end. ----
    prof = _db.residual_load_profile_v2(window_days=200, end_date=train_end_date)
    v2_pred = test.apply(
        lambda r: _db.lookup_residual_kwh(prof, int(r["dow"]), int(r["hour"]), 30 if r["half"] else 0),
        axis=1,
    ).to_numpy()

    # --- ML candidates predicting RESIDUAL load. -------------------------------
    feat_temporal = ["sod_sin", "sod_cos", "dow_sin", "dow_cos", "is_weekend", "month",
                     "lag_1d", "lag_7d", "roll7", "roll28"]
    feat_weather = feat_temporal + ["temp_c", "cloud_pct", "solar_wm2"]
    y_tr = train["residual_kwh"].to_numpy()
    y_te = test["residual_kwh"].to_numpy()

    def fit_pred(feats, model):
        Xtr, Xte = train[feats].to_numpy(), test[feats].to_numpy()
        if isinstance(model, str) and model == "ridge":
            # Ridge can't take NaN → median-impute + scale.
            med = np.nanmedian(Xtr, axis=0)
            Xtr = np.where(np.isnan(Xtr), med, Xtr)
            Xte = np.where(np.isnan(Xte), med, Xte)
            mdl = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
        else:
            mdl = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05,
                                                max_depth=4, l2_regularization=1.0)
        mdl.fit(Xtr, y_tr)
        return np.clip(mdl.predict(Xte), 0, None)

    preds = {
        "v2_median (prod)": v2_pred,
        "ridge_temporal": fit_pred(feat_temporal, "ridge"),
        "gbm_temporal": fit_pred(feat_temporal, "gbm"),
        "gbm_temporal+weather": fit_pred(feat_weather, "gbm"),
    }

    # --- Decomposition: low-Daikin (residual) vs high-Daikin (HP-present). ------
    low = (test["hp_kwh"] <= LOW_DAIKIN_KWH).to_numpy()
    print(f"\ntest slots: {len(test)}  low-Daikin {low.sum()}  high-Daikin {(~low).sum()}")

    lines = []
    def emit(s=""):
        print(s); lines.append(s)

    emit("\n=== Residual-load forecast: MAE / bias vs measured residual (test, out-of-sample) ===")
    emit(f"{'model':<24} {'MAE_all':>8} {'bias':>7} {'MAE_lowD':>9} {'MAE_highD':>10}")
    for name, p in preds.items():
        mae_all = _mae(p, y_te)
        bias = _bias(p, y_te)
        mae_low = _mae(p[low], y_te[low])
        mae_high = _mae(p[~low], y_te[~low])
        emit(f"{name:<24} {mae_all:8.4f} {bias:+7.4f} {mae_low:9.4f} {mae_high:10.4f}")

    # Best ML vs v2 on residual-dominated (low-Daikin) slots — the headline.
    base_low = _mae(preds["v2_median (prod)"][low], y_te[low])
    gbm_low = _mae(preds["gbm_temporal"][low], y_te[low])
    impr = (base_low - gbm_low) / base_low * 100 if base_low > 0 else 0.0
    emit(f"\nHeadline (low-Daikin / residual slots): v2 MAE {base_low:.4f} -> gbm {gbm_low:.4f} "
         f"({impr:+.1f}% )")

    # Decomposition: how much TOTAL-load error lives in HP-present slots.
    tot_pred = preds["v2_median (prod)"] + np.nan_to_num(test["hp_kwh"].to_numpy())  # v2 residual + measured HP
    tot_actual = test["load_kwh"].to_numpy()
    emit(f"\nError attribution (v2 residual + MEASURED hp vs total load):")
    emit(f"  total MAE all slots : {_mae(tot_pred, tot_actual):.4f}")
    emit(f"  MAE low-Daikin slots: {_mae(tot_pred[low], tot_actual[low]):.4f}  (residual error)")
    emit(f"  MAE high-Daikin slot: {_mae(tot_pred[~low], tot_actual[~low]):.4f}  (residual+HP-allocation error)")

    # --- Rejected corrector reference (its own out-of-sample number). ----------
    try:
        bt = _db_load_bias_backtest()
        emit(f"\nRejected additive corrector (load_bias backtest, for reference):")
        emit(f"  {bt}")
    except Exception as e:
        emit(f"\n(load_bias backtest unavailable: {e})")

    # --- Verdict ---------------------------------------------------------------
    emit("\n=== VERDICT ===")
    if impr >= 10 and gbm_low < base_low:
        emit(f"ML beats v2 on residual slots by {impr:.0f}% out-of-sample → worth an LP-cost replay "
             "stage before productionizing.")
    elif impr >= 3:
        emit(f"Marginal residual gain ({impr:.0f}%). Unlikely to move dispatch £ — confirm with an "
             "LP-cost replay before any build.")
    else:
        emit(f"No meaningful residual gain ({impr:+.0f}%). The v2 median is near the noise floor for "
             "occupancy load — consistent with the rejected corrector.")
    hp_share = _mae(tot_pred[~low], tot_actual[~low]) - _mae(tot_pred[low], tot_actual[low])
    emit(f"High-Daikin slots carry ~{hp_share:+.3f} kWh more MAE than residual slots → the bigger "
         "lever is heat-pump TIMING (#540), not the residual load model." if hp_share > 0.02 else
         "Residual and HP slots carry similar error.")

    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w") as f:
        f.write("# Load ML feasibility — findings\n\n")
        f.write(f"_Generated from a read-only copy of the prod DB. Window {day_lo}..{day_hi} "
                f"(HP-split bound). Chronological {int((1-args.test_frac)*100)}/{int(args.test_frac*100)} "
                "train/test. Directional — re-run as history accrues._\n\n```\n")
        f.write("\n".join(lines) + "\n```\n")
    print(f"\nReport written: {args.report}")
    conn.close()


def _db_load_bias_backtest():
    from src import load_bias
    bt = load_bias.backtest_load_recent_bias(21)
    ins = bt.get("in_sample") or {}
    oos = bt.get("out_of_sample") or {}
    def fmt(d):
        if not d:
            return "n/a"
        return (f"MAE {d.get('before', {}).get('mae_kwh')}->{d.get('after', {}).get('mae_kwh')} "
                f"({d.get('mae_reduction_pct')}%, n={d.get('n_slots')})")
    return f"in-sample {fmt(ins)} | out-of-sample {fmt(oos)}"


if __name__ == "__main__":
    main()
