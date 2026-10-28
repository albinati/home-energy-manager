"""Insights "O que fazer" (#879 PR 2) — recommendations composed from REAL data.

``recommendations(end, start=...)`` returns ``{generated_at, week, items, week_summary,
house}``. Every item is built by its own small function and independently guarded: an
item whose data is missing or insufficient is OMITTED (never faked), and an item that
raises is dropped with a warning — the page keeps the rest. Read-only; cached 10 minutes
per (DB, start, end), single-flight per key.

Item kinds: ``status`` (HEM already automates it), ``suggestion`` (depends on people: a
time and pence), ``setting`` (a runtime knob change; carries ``action``). Money is signed
pence, negative = saving; ``money_label`` is the display string. A £ is shown ONLY when
its counterfactual is knowable from realised data; otherwise the row states the facts.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 600.0
_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()
_key_locks: dict[tuple[str, str, str], threading.Lock] = {}

LOOKBACK_DAYS = 28             # realised-import window for the cooking items
MIN_SAMPLE_DAYS = 10           # breakfast needs this many days with the full window measured
MIN_WEEKEND_DAYS = 4
BASELINE_SLOTS = ((2, 0), (2, 30), (3, 0), (3, 30))   # 02-04: house baseload, no heating boost / charge
MIN_SCORECARD_DAYS = 14        # dinner coverage needs a few weeks of scorecards
PEAK_IMPORT_COVERED_KWH = 0.3  # peak-band import above this = the battery did not cover the peak
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


def _banded(day: date, tz: ZoneInfo) -> tuple[list[Any], dict[str, float]] | None:
    """(windows, tier prices) for a BANDED tariff, else None (the advice is
    meaningless on a dynamic tariff)."""
    from .load_expected import band_windows_for_day

    windows, structure = band_windows_for_day(day, tz)
    if structure != "banded" or not windows:
        return None
    prices = band_prices(windows)
    if "cheap" not in prices or "day" not in prices:
        return None
    return windows, prices


_banded_today = _banded  # back-compat alias


def _local_hm(t: datetime, tz: ZoneInfo) -> tuple[int, int]:
    lt = t.astimezone(tz)
    return lt.hour, lt.minute


def _hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _span(w: Any, tz: ZoneInfo) -> str:
    """'04–07' from a band window (minutes kept only when not on the hour)."""
    def f(t: datetime) -> str:
        h, m = _local_hm(t, tz)
        return f"{h:02d}" if m == 0 else f"{h:02d}:{m:02d}"
    return f"{f(w.start_utc)}–{f(w.end_utc)}"


def _slots_from(h: int, m: int, n: int) -> list[tuple[int, int]]:
    out = []
    for _ in range(n):
        out.append((h, m))
        m += 30
        if m >= 60:
            m = 0
            h += 1
    return out


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


def _run_cost(rates: list[tuple[datetime, datetime, float]], start: datetime, minutes: float,
              kwh: float) -> float | None:
    """Pence for ``kwh`` spread evenly over the run, priced slot by slot."""
    n = max(1, int(-(-float(minutes) // 30))) if minutes else 1
    tot = 0.0
    for i in range(n):
        p = _rate_at(rates, start + timedelta(minutes=30 * i))
        if p is None:
            return None
        tot += kwh / n * p
    return tot


def washer_item(end: date, tz: ZoneInfo, start: date | None = None) -> dict[str, Any] | None:
    apps = list(db.list_appliances(enabled_only=True))
    if not apps:
        return None
    start = start or (end - timedelta(days=6))
    a_utc = datetime(start.year, start.month, start.day, tzinfo=tz).astimezone(UTC)
    b_utc = datetime(end.year, end.month, end.day, tzinfo=tz).astimezone(UTC) + timedelta(days=1)
    rates = _load_rates(a_utc - timedelta(days=1), b_utc + timedelta(days=2))
    bt = _banded(end, tz)
    cheap_p = bt[1].get("cheap") if bt else None
    runs = cheap_runs = 0
    cost = counter = 0.0
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
            dur_min = float(j.get("duration_minutes") or 0)
            if j.get("actual_kwh") not in (None, 0):
                kwh = float(j["actual_kwh"])
            else:
                kw = learned[0] if learned else float(ap.get("typical_kw") or 0.5)
                kwh = kw * dur_min / 60.0
            c_run = _run_cost(rates, st, dur_min, kwh)
            if c_run is None:
                continue
            # counterfactual: the same cycle started the moment it was requested
            c_req = _run_cost(rates, armed, dur_min, kwh) if armed else None
            runs += 1
            kwhs.append(kwh)
            cost += c_run
            counter += c_req if c_req is not None else c_run
            p_start = _rate_at(rates, st)
            if cheap_p is not None and p_start is not None and p_start <= cheap_p * 1.05:
                cheap_runs += 1
    now = datetime.now(UTC)
    nxt = None
    for ap in apps:
        for j in db.get_appliance_jobs(from_utc=_z(now - timedelta(hours=1)), appliance_id=int(ap["id"]),
                                       limit=20):
            if j.get("status") in ("scheduled", "running"):
                st = _parse(j.get("planned_start_utc"))
                if st and (nxt is None or st < nxt):
                    nxt = st
    head = "Automática — o HEM liga no melhor horário"
    if nxt is not None:
        day_word = "hoje" if nxt.astimezone(tz).date() == datetime.now(tz).date() else "em breve"
        head += f"; {day_word} {nxt.astimezone(tz).strftime('%H:%M')}"
    detail = "É só apertar Smart Control na máquina."
    ev: dict[str, Any] = {"runs": runs, "cheap_runs": cheap_runs, "appliances": names}
    if runs:
        each = sum(kwhs) / len(kwhs)
        detail += (f" Esta semana: {runs} {'lavagem' if runs == 1 else 'lavagens'}, "
                   f"{'todas' if cheap_runs == runs else str(cheap_runs) + ' de ' + str(runs)} na faixa barata, "
                   f"{pt(each, 2)} kWh cada — custaram {int(round(cost))}p no total")
        if counter > cost + 1:
            detail += f" (ligando na hora do pedido: {int(round(counter))}p)"
        detail += "."
        ev.update(cost_pence=round(cost, 1), counterfactual_pence=round(counter, 1), kwh_each=round(each, 3))
    else:
        detail += " Nenhuma lavagem registrada esta semana."
    return _item("washer", "Máquina de lavar", "washer", head, detail, kind="status", evidence=ev)


# ------------------------------------------------------------------ cooking
def _import_by_local(first: date, last: date, tz: ZoneInfo) -> dict[tuple[date, int, int], float]:
    """Realised grid import (kWh) per LOCAL half-hour slot over [first, last]."""
    a = datetime(first.year, first.month, first.day, tzinfo=tz).astimezone(UTC)
    b = datetime(last.year, last.month, last.day, tzinfo=tz).astimezone(UTC) + timedelta(days=1)
    roll = db.half_hourly_kwh_for_utc_range(a, b, "grid_import_kw")
    out: dict[tuple[date, int, int], float] = {}
    for k, v in roll.items():
        t = _parse(k)
        if t is None:
            continue
        lt = t.astimezone(tz)
        out[(lt.date(), lt.hour, 30 if lt.minute >= 30 else 0)] = float(v)
    return out


def _baseline_per_slot(imp: dict[tuple[date, int, int], float], days: list[date]) -> float | None:
    vals = [imp[(d, h, m)] for d in days for (h, m) in BASELINE_SLOTS if (d, h, m) in imp]
    return float(median(vals)) if len(vals) >= 10 else None


def _excess_per_day(imp: dict[tuple[date, int, int], float], days: list[date],
                    slots: list[tuple[int, int]], baseline: float) -> list[float]:
    """Per day: realised import in ``slots`` above the 02-04 baseload (never more than
    the import itself). Days with any slot unmeasured are skipped."""
    out = []
    for d in days:
        vals = [imp.get((d, h, m)) for (h, m) in slots]
        if any(v is None for v in vals):
            continue
        total = float(sum(vals))
        out.append(max(0.0, min(total, total - baseline * len(slots))))
    return out


def _cooking_edges(windows: list[Any], tz: ZoneInfo) -> dict[str, Any]:
    """Morning cheap->dearer edge (breakfast) and the pre-cheap midday edge (lunch),
    derived from the day's real band windows."""
    ws = sorted(windows, key=lambda w: w.start_utc)
    out: dict[str, Any] = {}
    for i in range(len(ws) - 1):
        a, b = ws[i], ws[i + 1]
        eh, em = _local_hm(a.end_utc, tz)
        if b.start_utc != a.end_utc:
            continue
        if "breakfast" not in out and _tier(a) == "cheap" and _tier(b) != "cheap" \
                and 5 <= eh <= 11 and float(b.price_p) - float(a.price_p) > 1.0:
            out["breakfast"] = {"cheap": a, "after": b, "edge": (eh, em)}
        if "lunch" not in out and _tier(a) != "cheap" and _tier(b) == "cheap" \
                and 12 <= eh <= 14 and float(a.price_p) - float(b.price_p) > 1.0:
            out["lunch"] = {"before": a, "cheap": b, "edge": (eh, em)}
    return out


def _cooking_items(end: date, tz: ZoneInfo) -> list[dict[str, Any]]:
    bt = _banded(end, tz)
    if bt is None:
        return []
    windows, _prices = bt
    edges = _cooking_edges(windows, tz)
    if not edges:
        return []
    today = datetime.now(tz).date()
    last = min(end, today - timedelta(days=1))          # complete days only
    days = [last - timedelta(days=k) for k in range(LOOKBACK_DAYS)]
    imp = _import_by_local(days[-1], last, tz)
    out: list[dict[str, Any]] = []

    # Breakfast: realised import in the 90 min after the cheap band ends, above baseload.
    bf = edges.get("breakfast")
    if bf:
        base = _baseline_per_slot(imp, days)
        eh, em = bf["edge"]
        slots = _slots_from(eh, em, 3)
        if base is not None:
            ex = _excess_per_day(imp, days, slots, base)
            if len(ex) >= MIN_SAMPLE_DAYS:
                per_day = sum(ex) / len(ex)
                if per_day >= MIN_SAVING_KWH:
                    p_c, p_d = float(bf["cheap"].price_p), float(bf["after"].price_p)
                    week_p = per_day * (p_d - p_c) * 7
                    edge_s = _hhmm(eh, em)
                    out.append(_item(
                        "breakfast", "Café da manhã", "breakfast",
                        f"Chaleira e forno antes das {edge_s}",
                        f"{_span(bf['cheap'], tz)} é faixa barata ({pt(p_c)}p); às {edge_s} começa a faixa dia "
                        f"({pt(p_d)}p). Nas últimas {len(ex)} manhãs a rede forneceu ~{pt(per_day)} kWh por dia "
                        f"além do consumo de base entre {_hhmm(*slots[0])} e {_hhmm(*_slots_from(eh, em, 4)[-1])}.",
                        kind="suggestion", money_pence=-week_p, money_label=pence_label(-week_p, "/sem"),
                        evidence={"excess_kwh_per_day": round(per_day, 3), "baseline_kwh_per_slot": round(base, 3),
                                  "n_days": len(ex), "cheap_p": round(p_c, 2), "day_p": round(p_d, 2),
                                  "slots": [_hhmm(*s) for s in slots]}))

    # Weekend lunch: realised import in the hour before the cheap band starts.
    lu = edges.get("lunch")
    if lu:
        base = _baseline_per_slot(imp, days)
        eh, em = lu["edge"]
        slots = _slots_from(eh - 1, em, 2)
        wk_days = [d for d in days if d.weekday() >= 5]
        if base is not None:
            ex = _excess_per_day(imp, wk_days, slots, base)
            if len(ex) >= MIN_WEEKEND_DAYS:
                per = sum(ex) / len(ex)
                if per >= MIN_SAVING_KWH:
                    p_b, p_c = float(lu["before"].price_p), float(lu["cheap"].price_p)
                    saving = per * (p_b - p_c)
                    edge_s = _hhmm(eh, em)
                    out.append(_item(
                        "lunch_weekend", "Almoço (fim de semana)", "lunch",
                        f"Cozinhar a partir das {edge_s}",
                        f"Nos últimos {len(ex)} dias de fim de semana, {_hhmm(*slots[0])}–{edge_s} comprou da rede "
                        f"~{pt(per)} kWh além do consumo de base, a {pt(p_b)}p; às {edge_s} a faixa barata "
                        f"({pt(p_c)}p) começa.",
                        kind="suggestion", money_pence=-saving, money_label=pence_label(-saving, "/almoço"),
                        evidence={"excess_kwh_per_lunch": round(per, 3), "baseline_kwh_per_slot": round(base, 3),
                                  "n_days": len(ex), "week_pence": round(saving * 2, 1)}))
    return out


# ------------------------------------------------------------------- dinner
def dinner_item(end: date, tz: ZoneInfo) -> dict[str, Any] | None:
    bt = _banded(end, tz)
    if bt is None:
        return None
    windows, prices = bt
    peak_w = next((w for w in sorted(windows, key=lambda w: w.start_utc) if _tier(w) == "peak"), None)
    if peak_w is None:
        return None
    span = _span(peak_w, tz)
    eh, em = _local_hm(peak_w.end_utc, tz)
    p_after = price_at_local(windows, eh, em) or prices["day"]
    rows = db.get_cosy_scorecards(35, up_to=end.isoformat())
    rows = [r for r in rows if r.get("peak_import_kwh") is not None
            and (end - date.fromisoformat(r["date"])).days < 28]
    if len(rows) < MIN_SCORECARD_DAYS:
        return None
    week_start = end - timedelta(days=6)
    wk_rows = [r for r in rows if date.fromisoformat(r["date"]) >= week_start]
    covered_week = sum(1 for r in wk_rows if float(r["peak_import_kwh"]) <= PEAK_IMPORT_COVERED_KWH)

    def under_forecast(r: dict[str, Any]) -> bool:
        for b in ((r.get("payload") or {}).get("bands") or []):
            if isinstance(b, dict) and b.get("is_peak") and b.get("under_forecast"):
                return True
        return False

    by_dow: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_dow.setdefault(date.fromisoformat(r["date"]).weekday(), []).append(r)
    worst = None
    for dow, rs in by_dow.items():
        bad = [r for r in rs if float(r["peak_import_kwh"]) > PEAK_IMPORT_COVERED_KWH]
        if len(rs) >= 3 and len(bad) >= 2 and len(bad) / len(rs) >= 0.5:
            mean_bad = sum(float(r["peak_import_kwh"]) for r in bad) / len(bad)
            if worst is None or mean_bad * len(bad) > worst[1] * worst[2]:
                worst = (dow, mean_bad, len(bad), len(rs), sum(1 for r in bad if under_forecast(r)))
    n_wk = len(wk_rows)
    cover = f"{covered_week} de {n_wk} dias"
    ev: dict[str, Any] = {"covered_days_week": covered_week, "days_week": n_wk, "days_scored": len(rows),
                          "threshold_kwh": PEAK_IMPORT_COVERED_KWH, "peak_span": span}
    if worst is None:
        n_bad_all = sum(1 for r in rows if float(r["peak_import_kwh"]) > PEAK_IMPORT_COVERED_KWH)
        detail = (f"Sem compra da rede no pico ({span}) nas últimas 4 semanas." if n_bad_all == 0 else
                  f"Nas últimas 4 semanas o pico ({span}) comprou da rede em {n_bad_all} de {len(rows)} dias, "
                  f"sem um dia da semana que se repita.")
        return _item("dinner", "Pico", "dinner", f"A bateria cobriu o pico {cover}", detail,
                     kind="status", evidence=ev)
    dow, mean_bad, n_bad, n_tot, n_under = worst
    peak_p = prices.get("peak", float(peak_w.price_p))
    dname = _WEEKDAY_PT[dow]
    art = "a" if dow < 5 else "o"
    attributed = n_under >= max(1, n_bad / 2)            # the peak load ran above forecast on those days
    ev.update(weekday=dow, mean_peak_import_kwh=round(mean_bad, 3), bad_days=n_bad, of_days=n_tot,
              peak_p=round(peak_p, 2), day_p=round(p_after, 2), under_forecast_days=n_under)
    base = (f"{dname.capitalize()}: nas últimas 4 semanas o pico ({span}) comprou ~{pt(mean_bad)} kWh da rede "
            f"a {pt(peak_p, 0)}p em {n_bad} de {n_tot} dias.")
    if not attributed:
        return _item("dinner", "Pico", "dinner",
                     f"A bateria cobre o pico {cover} — {art} {dname} compra da rede", base,
                     kind="status", evidence=ev)
    week_p = mean_bad * (peak_p - p_after)
    return _item(
        "dinner", "Jantar", "dinner",
        f"A bateria cobre o jantar {cover} — {art} {dname} é o problema",
        f"{base} O consumo no pico veio acima do previsto. Jantar depois das {_hhmm(eh, em)} nesse dia "
        f"custa {pt(p_after, 0)}p em vez de {pt(peak_p, 0)}p por kWh.",
        kind="suggestion", money_pence=-week_p, money_label=pence_label(-week_p, "/sem"), evidence=ev)


# ------------------------------------------------------------------- drying
def drying_item(end: date, tz: ZoneInfo) -> dict[str, Any] | None:
    from . import plan_fronts as pf

    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    bt = _banded(tomorrow, tz)
    if bt is None:
        return None
    windows, prices = bt
    # the cheap band that covers the afternoon sun
    cheap_w = next((w for w in sorted(windows, key=lambda w: w.start_utc)
                    if _tier(w) == "cheap" and 11 <= _local_hm(w.start_utc, tz)[0] <= 15), None)
    if cheap_w is None:
        return None
    rid = db.find_latest_optimizer_run_id()
    if rid is None:
        return None
    slots = pf.lp_slots_for_day(db.get_lp_solution_slots(rid), tomorrow, tz)
    band = [s for s in slots if (_local_hm(s["_start"], tz)[0], 30 if _local_hm(s["_start"], tz)[1] >= 30 else 0)
            in cheap_w.local_slots]
    if len(band) < 4 or any(s.get("pv_use_kwh") is None for s in band):
        return None
    # PV that nothing in the plan uses: forecast minus what the house + battery take from it
    surplus = sum(max(0.0, float(s.get("pv_forecast_kwh") or 0.0) - float(s.get("pv_use_kwh") or 0.0))
                  for s in band)
    pv = sum(float(s.get("pv_forecast_kwh") or 0.0) for s in band)
    if surplus < DRYING_MIN_SURPLUS_KWH:
        return None
    s0 = _local_hm(band[0]["_start"], tz)
    ex_rows = db.get_agile_export_rates_in_range(_z(cheap_w.start_utc), _z(cheap_w.end_utc))
    exp_p = (sum(float(r["value_inc_vat"]) for r in ex_rows) / len(ex_rows)) if ex_rows \
        else float(getattr(config, "EXPORT_RATE_PENCE", 15.0))
    cheap_p = float(cheap_w.price_p)
    # Drying in the sun displaces CHEAP-band grid energy; exporting the PV instead earns exp_p.
    # Only a saving when exporting pays LESS than the cheapest grid energy.
    if exp_p >= cheap_p:
        return None
    use = min(surplus, DRYING_MAX_KWH)
    saving = use * (cheap_p - exp_p)
    return _item(
        "drying", "Secar roupa", "drying",
        f"Amanhã {_span(cheap_w, tz)}: {pt(surplus)} kWh de sol sobrando",
        f"O plano prevê {pt(pv)} kWh de sol nessa faixa e ~{pt(surplus)} kWh que ninguém usa — seriam "
        f"exportados a {pt(exp_p, 0)}p, abaixo dos {pt(cheap_p, 0)}p da rede na faixa barata.",
        kind="suggestion", money_pence=-saving, money_label=pence_label(-saving),
        evidence={"surplus_kwh": round(surplus, 3), "pv_kwh": round(pv, 3), "export_p": round(exp_p, 2),
                  "cheap_p": round(cheap_p, 2), "date": tomorrow.isoformat(),
                  "from": _hhmm(*s0)})


# ------------------------------------------------------------------- showers
def _predicted_evening_tank(today: date, tz: ZoneInfo) -> float | None:
    """Honest tank °C when the evening showers start (coast from the warmup target
    through the setback — not the warmup target itself)."""
    from .. import dhw_policy
    from ..dhw.comfort import shower_windows
    from ..dhw.params import resolve_tank_params
    from . import plan_fronts as pf

    preset = str(getattr(config, "OPTIMIZATION_PRESET", "normal") or "normal")
    warm_h = dhw_policy.read_warmup_hour(today)
    sws = [w for w in shower_windows(preset=preset) if float(w.start_hour) >= warm_h]
    if not sws:
        return None
    sw = min(sws, key=lambda w: float(w.start_hour))
    res = pf.predicted_shower_tank(sw, today, preset, resolve_tank_params(), tz, [])
    v = res.get("predicted_tank_c")
    return None if v is None else float(v)


def showers_item(end: date, tz: ZoneInfo) -> dict[str, Any] | None:
    from .. import dhw_demand, dhw_policy

    today = datetime.now(tz).date()
    rows = dhw_policy.dhw_schedule_rows_for_day(today, tz=tz, allow_past=True)

    def loc(iso: Any) -> str:
        t = _parse(iso)
        return t.astimezone(tz).strftime("%H:%M") if t else "?"

    leg_row = next((r for r in rows if r.get("action_type") == "legionella_cycle"), None)
    warm = [r for r in rows if r.get("action_type") == "tank_warmup" and r.get("tank_temp_c")]
    setb = [r for r in rows if r.get("action_type") == "tank_setback" and r.get("tank_temp_c")]
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
    if leg_row is not None and not (warm and setb):
        # Legionella day: the firmware owns the tank, the normal warmup/setback rows are replaced.
        a, b = loc(leg_row["start_utc"]), loc(leg_row["end_utc"])
        return _item("showers", "Banhos", "drop", f"Hoje a água sobe a 60 °C entre {a} e {b}",
                     "Ciclo anti-legionella do aparelho; depois o tanque volta ao programa normal.",
                     kind="status", evidence={"legionella": [a, b], "today": True})
    if not warm or not setb:
        return None
    w0 = min(warm, key=lambda r: str(r["start_utc"]))
    s0 = max(setb, key=lambda r: str(r["start_utc"]))
    target = float(w0["tank_temp_c"])
    setback = float(s0["tank_temp_c"])
    try:
        pred = _predicted_evening_tank(today, tz)
    except Exception:  # noqa: BLE001
        pred = None
    fits = 0
    if pred is not None:
        for n in range(1, 9):
            try:
                if dhw_demand.required_tank_temp_for_n_showers(n) <= pred:
                    fits = n
            except Exception:  # noqa: BLE001
                break
    head = (f"Água a {int(round(target))} °C das {loc(w0['start_utc'])} às {loc(s0['start_utc'])}; "
            f"depois cai para {int(round(setback))}")
    detail = ""
    if pred is not None and fits:
        detail = (f"Na hora dos banhos da noite a água estará a ~{int(round(pred))} °C: "
                  f"cabem {fits} {'banho' if fits == 1 else 'banhos'}.")
    if leg:
        detail += (f" {_WEEKDAY_PT[leg[0].weekday()].capitalize()} {leg[0].strftime('%H:%M')}–"
                   f"{leg[1].strftime('%H:%M')} ciclo anti-legionella (60 °C).")
    return _item("showers", "Banhos", "drop", head, detail.strip(), kind="status",
                 evidence={"target_c": target, "setback_c": setback, "showers_fit": fits,
                           "predicted_evening_c": pred,
                           "legionella": None if not leg else [leg[0].isoformat(), leg[1].isoformat()]})


# -------------------------------------------------------------------- tuning
_TUNING_COPY: dict[str, tuple[str, str, str]] = {
    # key: (who, icon, label)
    "DHW_TEMP_NORMAL_C": ("Água quente", "drop", "o alvo do tanque"),
    "LP_W3_NIGHT_FLOOR_C": ("Aquecimento", "thermo", "o piso noturno"),
    "LP_W3_PEAK_COAST_DELTA_C": ("Aquecimento", "thermo", "a queda permitida no pico"),
    "INDOOR_SETPOINT_C": ("Aquecimento", "thermo", "a temperatura alvo"),
    "LP_LOAD_EXPENSIVE_BAND_QUANTILE": ("Bateria", "dinner", "a reserva da bateria para o pico"),
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


def _live_value(key: str) -> Any:
    try:
        return getattr(config, key)
    except Exception:  # noqa: BLE001
        return None


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
        if r["week_start"] != week_start or r["verdict"] not in ("recommended", "trade-off", "comfort-first"):
            continue
        key = str(r["key"])
        if key in seen or key not in _TUNING_COPY:
            continue
        pl = r.get("payload") or {}
        new = _num(pl.get("body", {}).get("value", r["suggested_value"]))
        live = _live_value(key)
        cur = _num(live) if live is not None else _num(r["current_value"])
        if live is not None and str(_num(live)) == str(new):
            continue                                   # already applied: the row is stale
        seen.add(key)
        who, icon, label = _TUNING_COPY[key]
        pence = r.get("delta_pence_per_week")
        hrs = r.get("delta_comfort_hours")
        sh_cur, sh_new = pl.get("shower_days_below_current"), pl.get("shower_days_below_suggested")
        d_shower = (int(sh_new) - int(sh_cur)) if (sh_cur is not None and sh_new is not None) else None
        saves = r["verdict"] != "comfort-first" and pence is not None and float(pence) < 0
        unit = "" if key == "LP_LOAD_EXPENSIVE_BAND_QUANTILE" else " °C"
        if key == "DHW_TEMP_NORMAL_C":
            verb = "Baixar" if float(new) < float(cur) else "Subir"
            head = f"{verb} {label} {_fmt_val(cur)} → {_fmt_val(new)}{unit}"
        elif key == "LP_LOAD_EXPENSIVE_BAND_QUANTILE":
            head = f"Mudar {label}: {cur} → {new}"
        else:
            head = f"{label.capitalize()} {_fmt_val(cur)} → {_fmt_val(new)}{unit}"
        n_days = pl.get("replay_days") or pl.get("n_days") or 7
        bits = [f"Revisão de domingo: {n_days} dias simulados"]
        same_comfort = (hrs is None or float(hrs) <= 0.05) and (d_shower is None or d_shower <= 0)
        if hrs is not None and float(hrs) > 0.05:
            bits.append(f"{pt(float(hrs))} h a mais abaixo do piso")
        if d_shower is not None and d_shower > 0:
            bits.append(f"+{d_shower} {'dia' if d_shower == 1 else 'dias'} com banho abaixo do mínimo")
        if same_comfort and d_shower is not None and (hrs is not None):
            bits.append("mesmo conforto")
        if r["verdict"] == "comfort-first":
            bits.append("sugerido para proteger o conforto")
        det = ", ".join(bits)
        if key in _COMFORT_KEYS and sum(counts.values()):
            det += f". Você disse ok {counts['ok']}×" + (f" e frio {counts['cold']}×" if counts["cold"] else "")
            if last_cold:
                t = _parse(last_cold.get("at_utc"))
                det += f" ({(last_cold.get('room') or 'casa')}{', ' + t.astimezone(tz).strftime('%H:%M') if t else ''})"
        money = float(pence) if saves else None
        out.append(_item(
            f"tune_{key}", who, icon, head, det + ("" if det.endswith(".") else "."), kind="setting",
            money_pence=money, money_label=None if money is None else pence_label(money, "/sem"),
            action={"setting_key": key, "from": cur, "to": new},
            evidence={"week_start": week_start, "verdict": r["verdict"], "delta_comfort_hours": hrs,
                      "delta_shower_days": d_shower, "feedback": counts}))
    return out


# --------------------------------------------------------------- week summary
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


def _local_map(roll: dict[str, float], tz: ZoneInfo) -> dict[tuple[date, int, int], float]:
    out: dict[tuple[date, int, int], float] = {}
    for k, v in roll.items():
        t = _parse(k)
        if t is None:
            continue
        lt = t.astimezone(tz)
        out[(lt.date(), lt.hour, 30 if lt.minute >= 30 else 0)] = float(v)
    return out


def _energy_battery_solar(start: date, end: date, tz: ZoneInfo, *, banded: bool = True) -> dict[str, Any]:
    """Energy by band, solar and battery arbitrage over the COMPLETE days start..end.

    Everything is computed per half-hour slot, then summed:
      grid_charge = min(import, max(0, charge - pv))     (PV is assumed to charge first)
      pv_charge   = charge - grid_charge
      arbitrage   = sum(discharge x slot price) - sum(grid_charge x slot price)
                    - sum(pv_charge x export rate)
    (kWh discharged displace import at the slot's band price; grid charge costs the slot
    price; PV that charged the battery would otherwise have been exported.)"""
    from .load_expected import band_windows_for_day

    a = datetime(start.year, start.month, start.day, tzinfo=tz).astimezone(UTC)
    b = datetime(end.year, end.month, end.day, tzinfo=tz).astimezone(UTC) + timedelta(days=1)
    cols = {"imp": "grid_import_kw", "exp": "grid_export_kw", "pv": "solar_power_kw",
            "dis": "battery_discharge_kw", "chg": "battery_charge_kw"}
    roll = {k: _local_map(db.half_hourly_kwh_for_utc_range(a, b, c), tz) for k, c in cols.items()}
    cap = float(getattr(config, "BATTERY_CAPACITY_KWH", 0.0) or 0.0)
    flat = float(getattr(config, "EXPORT_RATE_PENCE", 15.0))
    exp_rate: dict[tuple[date, int, int], float] = {}
    for r in db.get_agile_export_rates_in_range(_z(a), _z(b)):
        t = _parse(r.get("valid_from"))
        if t:
            lt = t.astimezone(tz)
            exp_rate[(lt.date(), lt.hour, 30 if lt.minute >= 30 else 0)] = float(r["value_inc_vat"])

    imp_band = {"cheap": 0.0, "day": 0.0, "peak": 0.0}
    dis_val = gc_cost = pvc_cost = 0.0
    cheap_bought = pv_total = exp_total = chg_total = dis_total = exp_gbp = 0.0
    cheap_ref: float | None = None
    n_days = 0
    d = start
    while d <= end:
        windows, _structure = band_windows_for_day(d, tz)
        d_next = d + timedelta(days=1)
        if windows:
            had = False
            prices = band_prices(windows)
            cheap_ref = prices.get("cheap", cheap_ref)
            for w in windows:
                tier, price = _tier(w), float(w.price_p)
                for (h, m) in w.local_slots:
                    key = (d, h, m)
                    imp = roll["imp"].get(key)
                    if imp is not None:
                        had = True
                    imp = imp or 0.0
                    ex = roll["exp"].get(key, 0.0)
                    pv = roll["pv"].get(key, 0.0)
                    dis = roll["dis"].get(key, 0.0)
                    chg = roll["chg"].get(key, 0.0)
                    gc = min(imp, max(0.0, chg - pv))
                    pvc = max(0.0, chg - gc)
                    er = exp_rate.get(key, flat)
                    imp_band[tier] += imp
                    pv_total += pv
                    exp_total += ex
                    exp_gbp += ex * er / 100.0
                    chg_total += chg
                    dis_total += dis
                    dis_val += dis * price
                    gc_cost += gc * price
                    pvc_cost += pvc * er
                    if tier == "cheap":
                        cheap_bought += gc
            n_days += 1 if had else 0
        d = d_next
    solar_used = max(0.0, pv_total - exp_total)
    grid = sum(imp_band.values())
    total = grid + solar_used
    shares = {k: (round(v / total, 3) if total > 0 else None)
              for k, v in {**imp_band, "solar": solar_used}.items()}
    energy = {
        "total_kwh": _r(total, 1),
        "by_band": {"cheap": _r(imp_band["cheap"], 1), "day": _r(imp_band["day"], 1),
                    "peak": _r(imp_band["peak"], 1), "solar": _r(solar_used, 1)},
        "shares": shares, "peak_kwh": _r(imp_band["peak"], 2),
        "formula": "energia que entrou na casa = compra da rede por faixa + sol usado em casa (sol - exportado)",
    } if (banded and total > 0) else None

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
        "kwh": _r(pv_total, 1), "self_use_share": _r((solar_used / pv_total) if pv_total > 0.1 else None, 3),
        "export_kwh": _r(exp_total, 1), "export_gbp": _r(exp_gbp, 2), "forecast_mape": _r(mape, 3),
    } if pv_total > 0.1 else None

    sc = [r for r in db.get_cosy_scorecards(20, up_to=end.isoformat())
          if r.get("peak_import_kwh") is not None and start <= date.fromisoformat(r["date"]) <= end]
    covered = sum(1 for r in sc if float(r["peak_import_kwh"]) <= PEAK_IMPORT_COVERED_KWH)
    arbitrage = (dis_val - gc_cost - pvc_cost) / 100.0
    battery = {
        "arbitrage_gbp": _r(arbitrage, 2),
        "cheap_bought_kwh": _r(cheap_bought, 1), "cheap_p": _r(cheap_ref, 2),
        "cycles_per_day": _r((chg_total / cap / n_days) if cap and n_days else None, 2),
        "peak_covered_days": covered, "days": len(sc),
        "formula": ("soma, por meia hora, de kWh descarregados x preço da faixa, menos kWh carregados da rede x "
                    "preço da faixa, menos kWh carregados pelo sol x taxa de exportação; "
                    "não ajusta a variação de carga da bateria entre o início e o fim do período"),
    } if (banded and (dis_total + chg_total) > 0.1) else None
    return {"energy": energy, "solar": solar, "battery": battery, "n_days": n_days}


def week_summary(start: date, end: date, tz: ZoneInfo) -> dict[str, Any]:
    """Figures over the COMPLETE days of start..end (today's partial day is left out, so
    the week is comparable with ``usual`` = the per-day mean of the previous 4 weeks
    times the same number of days)."""
    from . import plan_fronts as pf

    today = datetime.now(tz).date()
    last = min(end, today - timedelta(days=1))
    out: dict[str, Any] = {"net_gbp": None, "usual_gbp": None, "compare": [], "current_label": None,
                           "complete_days": 0, "energy": None, "solar": None, "battery": None}
    if last < start:
        return out
    out["complete_days"] = (last - start).days + 1

    def safe(label: str, fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            logger.warning("recommendations: week_summary.%s failed", label, exc_info=True)
            return None

    cur = safe("net", lambda: pf._period(start, last))
    if cur and int(cur.get("n_days") or 0) > 0:
        out["net_gbp"] = _r(cur["net_cost_gbp"], 2)
        prev = safe("usual", lambda: pf._period(start - timedelta(days=28), start - timedelta(days=1)))
        if prev and int(prev.get("n_days") or 0) >= 14 and prev.get("per_day_gbp") is not None:
            out["usual_gbp"] = _r(float(prev["per_day_gbp"]) * out["complete_days"], 2)
    cmp_ = safe("compare", lambda: _compare_rows(start, last)) or {}
    out["compare"] = cmp_.get("rows") or []
    out["current_label"] = cmp_.get("current_label")
    banded = bool(safe("banded", lambda: _banded(last, tz)))
    rest = safe("energy", lambda: _energy_battery_solar(start, last, tz, banded=banded)) or {}
    for k in ("energy", "solar", "battery"):
        out[k] = rest.get(k)
    return out


# --------------------------------------------------------------------- house
SUNNY_PV_KW = 3.0
SUNNY_HOURS = 3.0


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
            n = int(b.get("n_slots") or 0)
            if b.get("measured_rise_c") is not None and n >= 3:
                # n slots of 30 min bracket n-1 intervals between the first and last reading
                rises.append(float(b["measured_rise_c"]) / ((n - 1) * 0.5))
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
        c = (joint.get("solar_fit") or {}).get("c_kwh_per_k") or joint.get("c_kwh_per_k")
        val = round(coef * SUNNY_PV_KW * SUNNY_HOURS / float(c), 2) if c else None
        out["solar_gain"] = {"coefficient": round(coef, 3), "value_c_per_sunny_afternoon": val,
                             "assumed_pv_kw": SUNNY_PV_KW, "assumed_hours": SUNNY_HOURS,
                             "applied": float(getattr(config, "LP_W3_SOLAR_GAIN_KW_PER_PV_KW", 0.0) or 0.0) > 0}
    chk = db.get_dhw_calibration("coast_check")
    if chk and chk.get("payload") and chk["payload"].get("measured_c_per_h") is not None:
        pl = chk["payload"]
        out["tank"] = {"c_per_h_night": pl.get("measured_c_per_h"),
                       "ratio_median_recent": pl.get("ratio_median_recent"),
                       "n_recent": pl.get("n_recent")}
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


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _build(end: date, start: date | None = None) -> dict[str, Any]:
    tz = _tz()
    start = start or _monday(end)
    items: list[dict[str, Any]] = []
    items += _guard("washer", lambda e, t: washer_item(e, t, start), end, tz)
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
    ws = week_summary(start, end, tz)
    last = min(end, datetime.now(tz).date() - timedelta(days=1))
    return {
        "generated_at": _z(datetime.now(UTC)),
        "week": {"start": start.isoformat(), "end": end.isoformat(),
                 "complete_days": ws.get("complete_days", 0),
                 "last_complete": last.isoformat() if last >= start else None},
        "items": items,
        "week_summary": ws,
        "house": house,
    }


def recommendations(end: date | None = None, *, start: date | None = None,
                    use_cache: bool = True) -> dict[str, Any]:
    tz = _tz()
    end = end or datetime.now(tz).date()
    start = start or _monday(end)
    key = (str(config.DB_PATH), start.isoformat(), end.isoformat())
    if use_cache:
        with _lock:
            hit = _cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_S:
                return hit[1]
    with _lock:
        flight = _key_locks.setdefault(key, threading.Lock())
    with flight:                                      # single-flight per key
        if use_cache:
            with _lock:
                hit = _cache.get(key)
                if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_S:
                    return hit[1]
        res = _build(end, start)
        with _lock:
            if len(_cache) > 16:
                _cache.clear()
            _cache[key] = (time.monotonic(), res)
            if len(_key_locks) > 64:
                _key_locks.clear()
    return res


def clear_cache() -> None:
    with _lock:
        _cache.clear()
