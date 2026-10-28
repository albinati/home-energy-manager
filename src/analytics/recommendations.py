"""Insights "O que fazer" (#879 PR 2) — recommendations composed from REAL data.

``recommendations(end)`` returns ``{generated_at, week, items, week_summary, house}``.
Every item is built by its own small function and independently guarded: an item
whose data is missing or insufficient is OMITTED (never faked), and an item that
raises is dropped with a warning — the page keeps the rest. Read-only; cached
10 minutes per (DB, end day).

Item kinds: ``status`` (HEM already automates it), ``suggestion`` (depends on
people: a time and pence), ``setting`` (a runtime knob change; carries ``action``).
Money is signed pence, negative = saving; ``money_label`` is the display string.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 600.0
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()

MIN_PROFILE_DAYS = 21          # < 3 weeks of residual history -> no cooking advice
MIN_SCORECARD_DAYS = 14        # dinner coverage needs a few weeks of scorecards
PEAK_IMPORT_COVERED_KWH = 0.3  # peak-band import above this = the battery did not cover the dinner
MIN_SAVING_KWH = 0.15          # below this a cooking suggestion is noise
DRYING_MIN_SURPLUS_KWH = 1.0
DRYING_MAX_KWH = 2.0

_WEEKDAY_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


# ---------------------------------------------------------------- small utils
def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(str(config.BULLETPROOF_TIMEZONE))
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _z(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(iso: Any) -> datetime | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _r(v: Any, n: int = 2) -> float | None:
    try:
        return None if v is None else round(float(v), n)
    except (TypeError, ValueError):
        return None


def pt(n: float, d: int = 1) -> str:
    """pt-BR decimal: 6.2 -> '6,2'."""
    return f"{n:.{d}f}".replace(".", ",")


def pence_label(pence: float, suffix: str = "") -> str:
    """Signed money label, negative = saving: -12.4 -> '−12p', 5 -> '+5p'."""
    v = int(round(abs(pence)))
    sign = "−" if pence < 0 else "+"
    return f"{sign}{v}p{suffix}"


def _item(key: str, who: str, icon: str, headline: str, detail: str, *, kind: str,
          money_pence: float | None = None, money_label: str | None = None,
          action: dict[str, Any] | None = None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "key": key, "who": who, "icon": icon, "headline": headline, "detail": detail,
        "money_pence": None if money_pence is None else round(money_pence, 1),
        "money_label": money_label or ("ok" if kind == "status" else None),
        "kind": kind, "action": action, "evidence": evidence or {},
    }


def _tier(w: Any) -> str:
    k = str(getattr(w, "key", "")).lower()
    lab = str(getattr(w, "label", "")).lower()
    if "peak" in k or "peak" in lab:
        return "peak"
    if "cheap" in k or "cheap" in lab or "neg" in k:
        return "cheap"
    return "day"


def band_prices(windows: list[Any]) -> dict[str, float]:
    """Mean price per tier (``cheap`` / ``day`` / ``peak``) of a banded day."""
    acc: dict[str, list[float]] = {}
    for w in windows:
        acc.setdefault(_tier(w), []).append(float(w.price_p))
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


def price_at_local(windows: list[Any], h: int, m: int = 0) -> float | None:
    for w in windows:
        if (h, 30 if m >= 30 else 0) in w.local_slots:
            return float(w.price_p)
    return None


def _banded_today(day: date, tz: ZoneInfo) -> tuple[list[Any], dict[str, float]] | None:
    """(windows, tier prices) for a BANDED tariff, else None (cooking advice is
    meaningless on a dynamic tariff)."""
    from .load_expected import band_windows_for_day

    windows, structure = band_windows_for_day(day, tz)
    if structure != "banded" or not windows:
        return None
    prices = band_prices(windows)
    if "cheap" not in prices or "day" not in prices:
        return None
    return windows, prices


# ------------------------------------------------------------------- washer
def _rate_at(rates: list[tuple[datetime, datetime, float]], t: datetime) -> float | None:
    for a, b, p in rates:
        if a <= t < b:
            return p
    return None


def _load_rates(a: datetime, b: datetime) -> list[tuple[datetime, datetime, float]]:
    code = str(config.OCTOPUS_TARIFF_CODE or "")
    if not code:
        return []
    out = []
    for r in db.get_rates_for_period(code, a, b):
        s, e = _parse(r.get("valid_from")), _parse(r.get("valid_to"))
        if s and e:
            out.append((s, e, float(r["value_inc_vat"])))
    return out


def washer_item(end: date, tz: ZoneInfo) -> dict[str, Any] | None:
    apps = [a for a in db.list_appliances(enabled_only=True)]
    if not apps:
        return None
    start = end - timedelta(days=6)
    a_utc = datetime(start.year, start.month, start.day, tzinfo=tz).astimezone(UTC)
    b_utc = datetime(end.year, end.month, end.day, tzinfo=tz).astimezone(UTC) + timedelta(days=1)
    rates = _load_rates(a_utc - timedelta(days=1), b_utc + timedelta(days=1))
    tiers: dict[str, float] = {}
    bt = _banded_today(end, tz)
    if bt:
        tiers = bt[1]
    cheap_p = tiers.get("cheap")
    runs = 0
    cheap_runs = 0
    cost = 0.0
    counter = 0.0
    kwhs: list[float] = []
    names: list[str] = []
    for ap in apps:
        names.append(str(ap.get("name") or ap.get("device_type") or "aparelho"))
        learned = db.appliance_learned_typical_kw(int(ap["id"]))
        for j in db.get_appliance_jobs(from_utc=_z(a_utc), to_utc=_z(b_utc), appliance_id=int(ap["id"]),
                                       limit=100):
            if j.get("status") != "completed":
                continue
            st = _parse(j.get("actual_start_utc")) or _parse(j.get("planned_start_utc"))
            armed = _parse(j.get("armed_at_utc"))
            if st is None:
                continue
            dur_h = float(j.get("duration_minutes") or 0) / 60.0
            if j.get("actual_kwh") not in (None, 0):
                kwh = float(j["actual_kwh"])
            else:
                kw = learned[0] if learned else float(ap.get("typical_kw") or 0.5)
                kwh = kw * dur_h
            p_run = _rate_at(rates, st)
            p_req = _rate_at(rates, armed) if armed else None
            if p_run is None:
                continue
            runs += 1
            kwhs.append(kwh)
            cost += kwh * p_run
            counter += kwh * (p_req if p_req is not None else p_run)
            if cheap_p is not None and p_run <= cheap_p * 1.05:
                cheap_runs += 1
    # today's planned/scheduled cycle
    now = datetime.now(UTC)
    nxt = None
    for ap in apps:
        for j in db.get_appliance_jobs(from_utc=_z(now - timedelta(hours=1)), appliance_id=int(ap["id"]),
                                       limit=20):
            if j.get("status") in ("scheduled", "running"):
                st = _parse(j.get("planned_start_utc"))
                if st and (nxt is None or st < nxt[0]):
                    learned = db.appliance_learned_typical_kw(int(ap["id"]))
                    kw = learned[0] if learned else float(ap.get("typical_kw") or 0.5)
                    kwh = kw * float(j.get("duration_minutes") or 0) / 60.0
                    p = _rate_at(rates, st) if rates else None
                    nxt = (st, kwh, p)
    head = "Automática — o HEM liga no melhor horário"
    if nxt is not None:
        hhmm = nxt[0].astimezone(tz).strftime("%H:%M")
        cost_p = f" ({int(round(nxt[1] * nxt[2]))}p)" if nxt[2] is not None else ""
        head += f"; {'hoje' if nxt[0].astimezone(tz).date() == datetime.now(tz).date() else 'em breve'} {hhmm}{cost_p}"
    detail = "É só apertar Smart Control na máquina."
    ev: dict[str, Any] = {"runs": runs, "cheap_runs": cheap_runs, "appliances": names}
    if runs:
        each = sum(kwhs) / len(kwhs)
        detail += (f" Esta semana: {runs} {'lavagem' if runs == 1 else 'lavagens'}, "
                   f"{'todas' if cheap_runs == runs else str(cheap_runs) + ' de ' + str(runs)} na faixa barata, "
                   f"{pt(each, 2)} kWh cada — custaram {int(round(cost))}p no total")
        if counter > cost + 1:
            detail += f" (teriam custado {int(round(counter))}p sem o HEM)"
        detail += "."
        ev.update(cost_pence=round(cost, 1), counterfactual_pence=round(counter, 1), kwh_each=round(each, 3))
    else:
        detail += " Nenhuma lavagem registrada esta semana."
    return _item("washer", "Máquina de lavar", "washer", head, detail, kind="status", evidence=ev)


# ------------------------------------------------------------------ cooking
def _profile_sum(prof: dict[str, Any], dows: list[int], h0: int, m0: int, n_slots: int) -> float:
    """Mean over ``dows`` of the median residual kWh summed over ``n_slots`` half-hours."""
    tot = 0.0
    for dow in dows:
        s = 0.0
        h, m = h0, m0
        for _ in range(n_slots):
            s += float(db.lookup_residual_kwh(prof, dow, h, m))
            m += 30
            if m >= 60:
                m = 0
                h += 1
        tot += s
    return tot / len(dows)


def _cooking_items(end: date, tz: ZoneInfo) -> list[dict[str, Any]]:
    bt = _banded_today(end, tz)
    if bt is None:
        return []
    windows, prices = bt
    prof = db.residual_load_profile_v2(window_days=60)
    counts = prof.get("day_counts") or {}
    if int(counts.get("total") or 0) < MIN_PROFILE_DAYS:
        return []
    out: list[dict[str, Any]] = []
    cheap, day_p = prices["cheap"], prices["day"]

    # Breakfast: 07:00-08:30 vs the cheap band that ends at 07:00.
    p_before = price_at_local(windows, 6, 30)
    p_after = price_at_local(windows, 7, 0)
    if p_before is not None and p_after is not None and p_after - p_before > 1.0:
        wk = _profile_sum(prof, [0, 1, 2, 3, 4], 7, 0, 3)
        we = _profile_sum(prof, [5, 6], 7, 0, 3)
        per_day = (wk * 5 + we * 2) / 7
        if per_day >= MIN_SAVING_KWH:
            diff = p_after - p_before
            day_p_saving = per_day * diff
            week_p = day_p_saving * 7
            out.append(_item(
                "breakfast", "Café da manhã", "breakfast",
                "Chaleira e forno antes das 07:00 custam metade",
                f"04–07 é faixa barata ({pt(p_before)}p); às 07:00 começa o dia ({pt(p_after, 0)}p). "
                f"Vocês usam ~{pt(per_day)} kWh no café: {int(round(day_p_saving))}p/dia de diferença, "
                f"{int(round(week_p))}p/semana.",
                kind="suggestion", money_pence=-week_p, money_label=pence_label(-week_p, "/sem"),
                evidence={"kwh_per_day": round(per_day, 3), "cheap_p": round(p_before, 2),
                          "day_p": round(p_after, 2), "window": "07:00-08:30"}))

    # Weekend lunch: 12:00-13:00 (day band) vs the 13:00 cheap start.
    p12 = price_at_local(windows, 12, 0)
    p13 = price_at_local(windows, 13, 0)
    if p12 is not None and p13 is not None and p12 - p13 > 1.0:
        sat = _profile_sum(prof, [5], 12, 0, 2)
        sun = _profile_sum(prof, [6], 12, 0, 2)
        mean = (sat + sun) / 2
        if mean >= MIN_SAVING_KWH:
            lo, hi = min(sat, sun), max(sat, sun)
            per_lunch = mean * (p12 - p13)
            rng = f"{pt(lo)}–{pt(hi)} kWh" if abs(hi - lo) >= 0.05 else f"{pt(mean)} kWh"
            out.append(_item(
                "lunch_weekend", "Almoço (fim de semana)", "lunch",
                "Cozinhar a partir das 13:00, não ao meio-dia",
                f"Sábado e domingo 12–13 vocês usam {rng} (medido, 60 dias) na faixa dia; "
                f"às 13:00 começa a barata.",
                kind="suggestion", money_pence=-per_lunch, money_label=pence_label(-per_lunch, "/almoço"),
                evidence={"kwh_sat": round(sat, 3), "kwh_sun": round(sun, 3), "day_p": round(p12, 2),
                          "cheap_p": round(p13, 2), "week_pence": round(per_lunch * 2, 1)}))
    return out


def dinner_item(end: date, tz: ZoneInfo) -> dict[str, Any] | None:
    bt = _banded_today(end, tz)
    if bt is None:
        return None
    prices = bt[1]
    if "peak" not in prices:
        return None
    rows = db.get_cosy_scorecards(35, up_to=end.isoformat())
    rows = [r for r in rows if r.get("peak_import_kwh") is not None
            and (end - date.fromisoformat(r["date"])).days < 28]
    if len(rows) < MIN_SCORECARD_DAYS:
        return None
    week_start = end - timedelta(days=6)
    wk_rows = [r for r in rows if date.fromisoformat(r["date"]) >= week_start]
    covered_week = sum(1 for r in wk_rows if float(r["peak_import_kwh"]) <= PEAK_IMPORT_COVERED_KWH)
    by_dow: dict[int, list[float]] = {}
    for r in rows:
        by_dow.setdefault(date.fromisoformat(r["date"]).weekday(), []).append(float(r["peak_import_kwh"]))
    worst = None
    for dow, vals in by_dow.items():
        bad = [v for v in vals if v > PEAK_IMPORT_COVERED_KWH]
        if len(vals) >= 3 and len(bad) >= 2 and len(bad) / len(vals) >= 0.5:
            mean_bad = sum(bad) / len(bad)
            if worst is None or mean_bad * len(bad) > worst[1] * worst[2]:
                worst = (dow, mean_bad, len(bad), len(vals))
    n_wk = len(wk_rows)
    cover = f"{covered_week} de {n_wk} dias"
    ev: dict[str, Any] = {"covered_days_week": covered_week, "days_week": n_wk, "days_scored": len(rows),
                          "threshold_kwh": PEAK_IMPORT_COVERED_KWH}
    if worst is None:
        return _item("dinner", "Jantar", "dinner",
                     f"A bateria cobre o jantar {cover}",
                     "O pico (16–19) não comprou da rede nas últimas 4 semanas além do normal.",
                     kind="status", evidence=ev)
    dow, mean_bad, n_bad, n_tot = worst
    peak_p, cheap_p = prices["peak"], prices["cheap"]
    week_p = mean_bad * (peak_p - cheap_p)
    dname = _WEEKDAY_PT[dow]
    art = "a" if dow < 5 else "o"
    ev.update(weekday=dow, mean_peak_import_kwh=round(mean_bad, 3), bad_days=n_bad, of_days=n_tot,
              peak_p=round(peak_p, 2), cheap_p=round(cheap_p, 2))
    return _item(
        "dinner", "Jantar", "dinner",
        f"A bateria cobre o jantar {cover} — {art} {dname} é o problema",
        f"{dname.capitalize()} o pico comprou ~{pt(mean_bad)} kWh da rede a {pt(peak_p, 0)}p "
        f"({n_bad} de {n_tot} dias nas últimas 4 semanas). Opções: jantar depois das 19:00 "
        f"{'nas' if dow < 5 else 'nos'} {dname}s, ou o HEM compra +{pt(mean_bad)} kWh de manhã "
        f"(+{int(round(mean_bad * cheap_p))}p) nesse dia.",
        kind="suggestion", money_pence=-week_p, money_label=pence_label(-week_p, "/sem"), evidence=ev)


# ------------------------------------------------------------------- drying
def drying_item(end: date, tz: ZoneInfo) -> dict[str, Any] | None:
    from . import plan_fronts as pf

    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    rid = db.find_latest_optimizer_run_id()
    if rid is None:
        return None
    slots = pf.lp_slots_for_day(db.get_lp_solution_slots(rid), tomorrow, tz)
    band = [s for s in slots if 13 <= s["_start"].astimezone(tz).hour < 16]
    if len(band) < 4:
        return None
    surplus = sum(float(s.get("export_kwh") or 0.0) for s in band)
    pv = sum(float(s.get("pv_forecast_kwh") or 0.0) for s in band)
    if surplus < DRYING_MIN_SURPLUS_KWH:
        return None
    cap = float(getattr(config, "BATTERY_CAPACITY_KWH", 0.0) or 0.0)
    soc_end = float(band[-1].get("soc_kwh") or 0.0)
    full = bool(cap and soc_end >= 0.9 * cap)
    ex_rows = db.get_agile_export_rates_in_range(
        _z(datetime(tomorrow.year, tomorrow.month, tomorrow.day, 13, tzinfo=tz)),
        _z(datetime(tomorrow.year, tomorrow.month, tomorrow.day, 16, tzinfo=tz)))
    exp_p = (sum(float(r["value_inc_vat"]) for r in ex_rows) / len(ex_rows)) if ex_rows \
        else float(getattr(config, "EXPORT_RATE_PENCE", 15.0))
    bt = _banded_today(tomorrow, tz)
    day_p = bt[1].get("day") if bt else None
    if day_p is None:
        rs = _load_rates(datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz).astimezone(UTC),
                         datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz).astimezone(UTC)
                         + timedelta(days=1))
        day_p = (sum(p for *_, p in rs) / len(rs)) if rs else None
    if day_p is None:
        return None
    use = min(surplus, DRYING_MAX_KWH)
    saving = use * max(0.0, day_p - exp_p)
    return _item(
        "drying", "Secar roupa", "drying",
        f"Amanhã 13–16: {pt(pv)} kWh de sol previstos{' com bateria cheia' if full else ''}",
        f"Sobram ~{pt(surplus)} kWh nessa faixa; o que a secadora usar seria exportado a {pt(exp_p, 0)}p "
        f"em vez de comprado a {pt(day_p, 0)}p depois.",
        kind="suggestion", money_pence=-saving, money_label=pence_label(-saving),
        evidence={"surplus_kwh": round(surplus, 3), "pv_kwh": round(pv, 3), "export_p": round(exp_p, 2),
                  "day_p": round(day_p, 2), "battery_full": full, "date": tomorrow.isoformat()})


# ------------------------------------------------------------------- showers
def showers_item(end: date, tz: ZoneInfo) -> dict[str, Any] | None:
    from .. import dhw_demand, dhw_policy

    today = datetime.now(tz).date()
    rows = dhw_policy.dhw_schedule_rows_for_day(today, tz=tz, allow_past=True)
    warm = [r for r in rows if r.get("action_type") == "tank_warmup" and r.get("tank_temp_c")]
    setb = [r for r in rows if r.get("action_type") == "tank_setback" and r.get("tank_temp_c")]
    if not warm or not setb:
        return None

    def loc(iso: Any) -> str:
        t = _parse(iso)
        return t.astimezone(tz).strftime("%H:%M") if t else "?"

    w0 = min(warm, key=lambda r: str(r["start_utc"]))
    s0 = max(setb, key=lambda r: str(r["start_utc"]))
    target = float(w0["tank_temp_c"])
    setback = float(s0["tank_temp_c"])
    fits = 0
    for n in range(1, 9):
        try:
            if dhw_demand.required_tank_temp_for_n_showers(n) <= target:
                fits = n
        except Exception:  # noqa: BLE001
            break
    # next legionella cycle within a week
    leg = None
    for i in range(0, 8):
        d = today + timedelta(days=i)
        try:
            win = dhw_policy._legionella_standoff_window_utc(d)  # noqa: SLF001
        except Exception:  # noqa: BLE001
            win = None
        if win:
            a, b = win[0].astimezone(tz), win[1].astimezone(tz)
            if a.date() == d or b.date() == d:
                leg = (a, b)
                break
    head = (f"Água a {int(round(target))} °C das {loc(w0['start_utc'])} às {loc(s0['start_utc'])}; "
            f"depois cai para {int(round(setback))}")
    detail = ""
    if fits:
        detail = f"Cabem {fits} {'banho' if fits == 1 else 'banhos'} a {int(round(target))} °C."
    if leg:
        detail += (f" {_WEEKDAY_PT[leg[0].weekday()].capitalize()} {leg[0].strftime('%H:%M')}–"
                   f"{leg[1].strftime('%H:%M')} ciclo anti-legionella (60 °C).")
    return _item("showers", "Banhos", "drop", head, detail.strip(), kind="status",
                 evidence={"target_c": target, "setback_c": setback, "showers_fit": fits,
                           "legionella": None if not leg else [leg[0].isoformat(), leg[1].isoformat()]})


# -------------------------------------------------------------------- tuning
_TUNING_COPY: dict[str, tuple[str, str, str]] = {
    # key: (who, icon, label)
    "DHW_TEMP_NORMAL_C": ("Água quente", "drop", "o alvo do tanque"),
    "LP_W3_NIGHT_FLOOR_C": ("Aquecimento", "thermo", "o piso noturno"),
    "LP_W3_PEAK_COAST_DELTA_C": ("Aquecimento", "thermo", "a queda permitida no pico"),
    "INDOOR_SETPOINT_C": ("Aquecimento", "thermo", "a temperatura alvo"),
    "LP_LOAD_EXPENSIVE_BAND_QUANTILE": ("Bateria", "dinner", "a margem da bateria no jantar"),
}
_COMFORT_KEYS = {"LP_W3_NIGHT_FLOOR_C", "LP_W3_PEAK_COAST_DELTA_C", "INDOOR_SETPOINT_C"}


def _num(v: Any) -> Any:
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return v


def _fmt_val(v: Any) -> str:
    v = _num(v)
    return pt(v, 1) if isinstance(v, float) else str(v)


def tuning_items(end: date, tz: ZoneInfo) -> list[dict[str, Any]]:
    rows = db.list_tuning_suggestions(1)
    if not rows:
        return []
    week_start = rows[0]["week_start"]
    ws = _parse(f"{week_start}T00:00:00Z")
    fb = db.get_comfort_feedback(days=7, since_utc=_z(ws) if ws else None) if ws else []
    counts = {"cold": 0, "ok": 0, "hot": 0}
    last_cold = None
    for f in fb:
        v = str(f.get("verdict") or "")
        if v in counts:
            counts[v] += 1
        if v == "cold":
            last_cold = f
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["week_start"] != week_start or r["verdict"] not in ("recommended", "trade-off"):
            continue
        key = str(r["key"])
        if key in seen or key not in _TUNING_COPY:
            continue
        seen.add(key)
        who, icon, label = _TUNING_COPY[key]
        cur, new = _num(r["current_value"]), _num((r.get("payload") or {}).get("body", {}).get("value", r["suggested_value"]))
        pence = r.get("delta_pence_per_week")
        hrs = r.get("delta_comfort_hours")
        unit = "" if key == "LP_LOAD_EXPENSIVE_BAND_QUANTILE" else " °C"
        if key == "DHW_TEMP_NORMAL_C":
            verb = "Baixar" if float(new) < float(cur) else "Subir"
            head = f"{verb} {label} {_fmt_val(cur)} → {_fmt_val(new)}{unit}"
        elif key == "LP_LOAD_EXPENSIVE_BAND_QUANTILE":
            head = f"Ajustar {label}: {cur} → {new}"
        else:
            head = f"{label.capitalize()} {_fmt_val(cur)} → {_fmt_val(new)}{unit}"
        money = None if pence is None else float(pence)
        bits = []
        if pence is not None:
            bits.append(f"{pence_label(money, '/semana')}".replace("p/semana", "p/semana"))
        if hrs is not None and abs(float(hrs)) >= 0.05:
            bits.append(f"{pt(abs(float(hrs)))} h a {'mais' if float(hrs) > 0 else 'menos'} abaixo do piso")
        if bits:
            head += ": " + ", ".join(bits) if key in _COMFORT_KEYS else ""
        n_days = ((r.get("payload") or {}).get("replay_days") or (r.get("payload") or {}).get("n_days") or 7)
        if key in _COMFORT_KEYS and sum(counts.values()):
            det = f"você disse ok {counts['ok']}×" + (f" e frio {counts['cold']}×" if counts["cold"] else "")
            if last_cold:
                t = _parse(last_cold.get("at_utc"))
                det += f" ({(last_cold.get('room') or 'casa')}{', ' + t.astimezone(tz).strftime('%H:%M') if t else ''})"
        else:
            det = f"Revisão de domingo: {n_days} dias simulados"
            det += ", mesmo conforto" if (hrs is None or float(hrs) <= 0.05) else ""
            if pence is not None:
                det += f", {pence_label(money, '/semana')}"
        out.append(_item(
            f"tune_{key}", who, icon, head, det + ("." if not det.endswith(".") else ""), kind="setting",
            money_pence=money, money_label=None if money is None else pence_label(money, "/sem"),
            action={"setting_key": key, "from": cur, "to": new},
            evidence={"week_start": week_start, "verdict": r["verdict"], "delta_comfort_hours": hrs,
                      "feedback": counts}))
    return out


# --------------------------------------------------------------- week summary
def _week_bounds(end: date, tz: ZoneInfo) -> tuple[date, datetime, datetime]:
    start = end - timedelta(days=6)
    a = datetime(start.year, start.month, start.day, tzinfo=tz).astimezone(UTC)
    b = datetime(end.year, end.month, end.day, tzinfo=tz).astimezone(UTC) + timedelta(days=1)
    return start, a, b


def _net_gbp(start: date, end: date) -> tuple[float | None, int]:
    from . import pnl

    p = pnl.compute_period_pnl(start, end)
    n = int(p.get("n_days") or 0)
    v = p.get("realised_net_cost_gbp")
    return (None if v is None or n == 0 else float(v)), n


def _compare_rows(start: date, end: date) -> dict[str, Any]:
    from . import fair_compare
    from . import plan_fronts as pf

    res = fair_compare.cached_fair_comparison(start, end, 4)
    cur, rows = pf.compare_rows({"tariffs": res.get("tariffs") or []})
    cur_label = pf.short_tariff_label(cur.get("display_name"), cur.get("product_code")) if cur else None
    out = []
    for r in rows:
        if r["is_current"] or r["delta_vs_current_gbp"] is None:
            continue
        out.append({"short_label": pf.short_tariff_label(r["display_name"], r["product_code"]),
                    "delta_gbp": round(float(r["delta_vs_current_gbp"]), 2),
                    "approximate": bool(r["approximate"])})
    return {"current_label": cur_label, "rows": out}


def _energy_battery_solar(end: date, tz: ZoneInfo) -> dict[str, Any]:
    from .load_expected import _window_sum, band_windows_for_day

    start, a, b = _week_bounds(end, tz)
    cols = {"import": "grid_import_kw", "export": "grid_export_kw", "pv": "solar_power_kw",
            "dis": "battery_discharge_kw", "chg": "battery_charge_kw"}
    roll = {k: db.half_hourly_kwh_for_utc_range(a, b, c) for k, c in cols.items()}
    cap = float(getattr(config, "BATTERY_CAPACITY_KWH", 0.0) or 0.0)

    imp_band = {"cheap": 0.0, "day": 0.0, "peak": 0.0}
    arb_dis = 0.0
    arb_chg_penalty = 0.0
    cheap_bought = 0.0
    cheap_ref: float | None = None
    n_days = 0
    export_gbp = 0.0
    exp_rows = db.get_agile_export_rates_in_range(_z(a), _z(b))
    exp_rate = {_z(_parse(r["valid_from"])): float(r["value_inc_vat"]) for r in exp_rows if _parse(r["valid_from"])}
    flat = float(getattr(config, "EXPORT_RATE_PENCE", 15.0))
    for k, v in roll["export"].items():
        export_gbp += v * exp_rate.get(k, flat) / 100.0
    for i in range(7):
        d = start + timedelta(days=i)
        windows, _structure = band_windows_for_day(d, tz)
        if not windows:
            continue
        prices = band_prices(windows)
        cheap_p = prices.get("cheap", min(prices.values()))
        cheap_ref = cheap_p
        had = False
        for w in windows:
            tier = _tier(w)
            imp, n_imp = _window_sum(roll["import"], w, tz, day=d)
            if n_imp:
                had = True
            imp_band[tier] += imp
            dis, _ = _window_sum(roll["dis"], w, tz, day=d)
            chg, _ = _window_sum(roll["chg"], w, tz, day=d)
            if tier == "cheap":
                cheap_bought += min(chg, imp)
            else:
                arb_dis += dis * (float(w.price_p) - cheap_p)
                # charging from the grid outside the cheap band costs the premium
                arb_chg_penalty += min(chg, imp) * (float(w.price_p) - cheap_p)
        n_days += 1 if had else 0
    pv = sum(roll["pv"].values())
    exp_kwh = sum(roll["export"].values())
    chg_total = sum(roll["chg"].values())
    solar_used = max(0.0, pv - exp_kwh)
    grid = sum(imp_band.values())
    total = grid + solar_used
    share = (lambda x: round(x / total, 3) if total > 0 else None)  # noqa: E731
    energy = {
        "total_kwh": _r(total, 1),
        "by_band": {"cheap": _r(imp_band["cheap"], 1), "day": _r(imp_band["day"], 1),
                    "peak": _r(imp_band["peak"], 1), "solar": _r(solar_used, 1)},
        "shares": {"cheap": share(imp_band["cheap"]), "day": share(imp_band["day"]),
                   "peak": share(imp_band["peak"]), "solar": share(solar_used)},
        "peak_kwh": _r(imp_band["peak"], 2),
        "formula": "energia que entrou na casa = compra da rede por faixa + sol usado em casa (sol - exportado)",
    } if total > 0 else None

    # PV forecast accuracy: mean over days of |sum(actual)-sum(forecast)| / sum(actual)
    errs = db.get_pv_error_log_range(_z(a), _z(b))
    per_day: dict[date, list[float]] = {}
    for r in errs:
        t = _parse(r.get("slot_time_utc"))
        if t is None or r.get("forecast_kwh") is None or r.get("actual_kwh") is None:
            continue
        d_ = per_day.setdefault(t.astimezone(tz).date(), [0.0, 0.0])
        d_[0] += float(r["actual_kwh"])
        d_[1] += float(r["forecast_kwh"])
    apes = [abs(a_ - f_) / a_ for a_, f_ in per_day.values() if a_ > 0.5]
    mape = (sum(apes) / len(apes)) if apes else None
    solar = {
        "kwh": _r(pv, 1), "self_use_share": _r((solar_used / pv) if pv > 0.1 else None, 3),
        "export_kwh": _r(exp_kwh, 1), "export_gbp": _r(export_gbp, 2), "forecast_mape": _r(mape, 3),
    } if pv > 0.1 else None

    # peak coverage from the scorecards
    sc = [r for r in db.get_cosy_scorecards(14, up_to=end.isoformat())
          if r.get("peak_import_kwh") is not None and start <= date.fromisoformat(r["date"]) <= end]
    covered = sum(1 for r in sc if float(r["peak_import_kwh"]) <= PEAK_IMPORT_COVERED_KWH)
    arbitrage = arb_dis - arb_chg_penalty
    battery = {
        "arbitrage_gbp": _r(arbitrage / 100.0, 2),
        "cheap_bought_kwh": _r(cheap_bought, 1), "cheap_p": _r(cheap_ref, 2),
        "cycles_per_day": _r((chg_total / cap / max(n_days, 1)) if cap and n_days else None, 2),
        "peak_covered_days": covered, "days": len(sc),
        "formula": ("soma, nas faixas dia e pico, de kWh descarregados x (preço da faixa - preço da faixa barata) "
                    "menos kWh carregados da rede fora da faixa barata x o mesmo prêmio"),
    } if (sum(roll["dis"].values()) + chg_total) > 0.1 else None
    return {"energy": energy, "solar": solar, "battery": battery}


def week_summary(end: date, tz: ZoneInfo) -> dict[str, Any]:
    start = end - timedelta(days=6)
    out: dict[str, Any] = {"net_gbp": None, "usual_gbp": None, "compare": [], "current_label": None, "energy": None,
                           "solar": None, "battery": None}

    def safe(label: str, fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            logger.warning("recommendations: week_summary.%s failed", label, exc_info=True)
            return None

    net = safe("net", lambda: _net_gbp(start, end))
    if net and net[0] is not None:
        out["net_gbp"] = _r(net[0], 2)
    prev: list[float] = []
    for k in range(1, 5):
        r = safe("usual", lambda k=k: _net_gbp(start - timedelta(days=7 * k), end - timedelta(days=7 * k)))
        if r and r[0] is not None and r[1] >= 5:
            prev.append(r[0])
    if prev:
        out["usual_gbp"] = _r(sum(prev) / len(prev), 2)
    cmp_ = safe("compare", lambda: _compare_rows(start, end)) or {}
    out["compare"] = cmp_.get("rows") or []
    out["current_label"] = cmp_.get("current_label")
    rest = safe("energy", lambda: _energy_battery_solar(end, tz)) or {}
    for k in ("energy", "solar", "battery"):
        out[k] = rest.get(k)
    return out


# --------------------------------------------------------------------- house
def house_section(tz: ZoneInfo) -> dict[str, Any]:
    out: dict[str, Any] = {}
    daily = db.get_lwt_learning_daily(14)
    payloads = [d.get("payload") or {} for d in daily]

    joint = next((p["joint_fit"] for p in payloads if (p.get("joint_fit") or {}).get("identifiable")), None)
    first_joint = next((p["joint_fit"] for p in payloads if p.get("joint_fit")), None)
    tau = (joint or {}).get("tau_h") or (first_joint or {}).get("coast_tau_h")
    if tau and float(tau) > 1:
        now = datetime.now(UTC)
        rows = db.get_lwt_learning_rows(_z(now - timedelta(days=2)), _z(now))
        ind = [float(r["indoor_real_c"]) for r in rows if r.get("indoor_real_c") is not None]
        outd = [float(r["outdoor_real_c"]) for r in rows if r.get("outdoor_real_c") is not None]
        if ind and outd:
            i_c, o_c = sum(ind) / len(ind), sum(outd) / len(outd)
            out["cooling_c_per_h"] = {"value": round(max(0.0, (i_c - o_c) / float(tau)), 3),
                                      "outdoor_c": round(o_c, 1), "tau_h": round(float(tau), 0)}
    rises = []
    offs = []
    for p in payloads[:7]:
        for b in p.get("night_rise_per_band") or []:
            if b.get("measured_rise_c") is not None and (b.get("n_slots") or 0) >= 2:
                rises.append(float(b["measured_rise_c"]) / (int(b["n_slots"]) * 0.5))
                offs.append(float(b.get("mean_offset_c") or 0.0))
    if rises:
        lag = None
        for p in payloads:
            e = p.get("emitter_tau_fit") or {}
            if e.get("best_tau_min") is not None and e.get("confidence") == "ok":
                lag = int(e["best_tau_min"])
                break
        out["heating_c_per_h"] = {"value": round(sum(rises) / len(rises), 3),
                                  "offset_c": round(sum(offs) / len(offs), 1), "lag_min": lag}
    if joint and joint.get("solar_identifiable") and joint.get("solar_gain_kw_per_pv_kw") is not None:
        coef = float(joint["solar_gain_kw_per_pv_kw"])
        c = joint.get("c_kwh_per_k")
        val = round(coef * 3.0 * 3.0 / float(c), 2) if c else None  # 3 kW PV x 3 h
        out["solar_gain"] = {"coefficient": round(coef, 3), "value_c_per_sunny_afternoon": val,
                             "applied": float(getattr(config, "LP_W3_SOLAR_GAIN_KW_PER_PV_KW", 0.0) or 0.0) > 0}
    chk = db.get_dhw_calibration("coast_check")
    if chk and chk.get("payload") and chk["payload"].get("measured_c_per_h") is not None:
        pl = chk["payload"]
        out["tank"] = {"c_per_h_night": pl.get("measured_c_per_h"),
                       "ratio_median_recent": pl.get("ratio_median_recent")}
    return out


# ----------------------------------------------------------------------- main
def _guard(label: str, fn, *a: Any) -> list[dict[str, Any]]:
    try:
        r = fn(*a)
    except Exception:  # noqa: BLE001 - one item must never take the page down
        logger.warning("recommendations: item %s failed", label, exc_info=True)
        return []
    if r is None:
        return []
    return r if isinstance(r, list) else [r]


def _build(end: date) -> dict[str, Any]:
    tz = _tz()
    items: list[dict[str, Any]] = []
    items += _guard("washer", washer_item, end, tz)
    items += _guard("cooking", _cooking_items, end, tz)
    items += _guard("dinner", dinner_item, end, tz)
    items += _guard("drying", drying_item, end, tz)
    items += _guard("showers", showers_item, end, tz)
    items += _guard("tuning", tuning_items, end, tz)
    house: dict[str, Any] = {}
    try:
        house = house_section(tz)
    except Exception:  # noqa: BLE001
        logger.warning("recommendations: house failed", exc_info=True)
    return {
        "generated_at": _z(datetime.now(UTC)),
        "week": {"start": (end - timedelta(days=6)).isoformat(), "end": end.isoformat()},
        "items": items,
        "week_summary": week_summary(end, tz),
        "house": house,
    }


def recommendations(end: date | None = None, *, use_cache: bool = True) -> dict[str, Any]:
    tz = _tz()
    end = end or datetime.now(tz).date()
    key = (str(config.DB_PATH), end.isoformat())
    mono = time.monotonic()
    if use_cache:
        with _lock:
            hit = _cache.get(key)
            if hit is not None and mono - hit[0] < _CACHE_TTL_S:
                return hit[1]
    res = _build(end)
    if use_cache:
        with _lock:
            if len(_cache) > 16:
                _cache.clear()
            _cache[key] = (mono, res)
    return res


def clear_cache() -> None:
    with _lock:
        _cache.clear()
