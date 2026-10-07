"""Comfort feedback loop (#833): command parsing, Telegram inbound poller,
weekly aggregation + bounded proposals.

Nothing here changes a setting unless ``COMFORT_FEEDBACK_AUTO_TUNE=true``, and
even then only ``LP_W3_NIGHT_FLOOR_C`` / ``LP_W3_PEAK_COAST_DELTA_C`` by at most
0.5 per week (the proposal step itself is 0.5).
"""
from __future__ import annotations

import logging
import unicodedata
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config

logger = logging.getLogger(__name__)

OFFSET_KEY = "telegram_update_offset"

_VERDICT_ALIASES = {
    "frio": "cold", "cold": "cold", "f": "cold",
    "ok": "ok", "bem": "ok", "normal": "ok",
    "quente": "hot", "hot": "hot", "q": "hot",
}
_PT_VERDICT = {"cold": "frio", "ok": "ok", "hot": "quente"}

USAGE = ("Uso: /conforto frio|ok|quente [cômodo] [nota]  ·  "
         "/comfort cold|ok|hot [room] [note]")

NIGHT_FLOOR_KEY = "LP_W3_NIGHT_FLOOR_C"
PEAK_COAST_KEY = "LP_W3_PEAK_COAST_DELTA_C"
STEP_C = 0.5
FLOOR_CAP_C = 22.0
FLOOR_MIN_C = 14.0
COAST_MAX_C = 4.0


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


# ---------------------------------------------------------------- parsing
def parse_command(text: str, known_rooms: list[str] | None = None) -> dict[str, Any]:
    """``{"kind": "feedback", verdict, room, note}`` | ``{"kind": "usage"}`` |
    ``{"kind": "unknown"}`` (not a comfort command or bad verdict)."""
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return {"kind": "unknown"}
    cmd = parts[0].split("@", 1)[0].lower()
    if cmd not in ("/conforto", "/comfort"):
        return {"kind": "unknown"}
    if len(parts) == 1:
        return {"kind": "usage"}
    verdict = _VERDICT_ALIASES.get(_norm(parts[1]))
    if verdict is None:
        return {"kind": "unknown"}
    rest = parts[2:]
    room = None
    if rest and known_rooms:
        by_norm = {_norm(r): r for r in known_rooms}
        hit = by_norm.get(_norm(rest[0]))
        if hit:
            room, rest = hit, rest[1:]
    return {"kind": "feedback", "verdict": verdict, "room": room,
            "note": " ".join(rest) or None}


def _known_rooms() -> dict[str, float]:
    try:
        ind = db.get_latest_indoor_reading(max_age_minutes=int(getattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30) or 30))
        return dict((ind or {}).get("rooms_c") or {})
    except Exception:  # noqa: BLE001
        return {}


def _rooms_line(rooms: dict[str, float]) -> str:
    return " · ".join(f"{r} {t:.1f} °C" for r, t in sorted(rooms.items()))


def _ack(row: dict[str, Any]) -> str:
    parts = [f"Registrado: {_PT_VERDICT.get(row['verdict'], row['verdict'])}"]
    rooms = row.get("rooms_c") or {}
    if rooms:
        parts.append(_rooms_line(rooms))
    elif row.get("indoor_c") is not None:
        parts.append(f"{row['indoor_c']:.1f} °C")
    if row.get("room"):
        parts.insert(1, f"[{row['room']}]")
    if row.get("band"):
        parts.append(f"banda {row['band']}")
    if row.get("lwt_offset_c") is not None:
        parts.append(f"LWT {row['lwt_offset_c']:+.0f}".replace("-", "−"))
    return " · ".join(parts)


def handle_message(text: str, *, source: str = "telegram") -> str:
    """Process one owner message and return the reply text."""
    rooms = _known_rooms()
    cmd = parse_command(text, list(rooms))
    if cmd["kind"] == "usage":
        cur = _rooms_line(rooms) if rooms else "sem leitura recente"
        return f"{USAGE}\nAgora: {cur}"
    if cmd["kind"] != "feedback":
        return USAGE
    row = db.insert_comfort_feedback(verdict=cmd["verdict"], source=source,
                                     room=cmd["room"], note=cmd["note"])
    return _ack(row)


# ----------------------------------------------------------------- poller
def poll_telegram_once() -> int:
    """One getUpdates short-poll. Returns the number of owner commands handled.
    Never raises. Offset is persisted BEFORE replying (at-most-once reply)."""
    from .. import telegram_transport as tt

    try:
        if not getattr(config, "TELEGRAM_INBOUND_ENABLED", True) or not tt.is_configured():
            return 0
        raw = db.get_kv(OFFSET_KEY)
        offset = int(raw) if raw and raw.lstrip("-").isdigit() else None
        updates = tt.get_updates(offset, timeout_s=0)
        if not updates:
            return 0
        owner = str(config.TELEGRAM_CHAT_ID).strip()
        handled = 0
        for u in updates:
            try:
                uid = int(u["update_id"])
            except (KeyError, TypeError, ValueError):
                continue
            db.set_kv(OFFSET_KEY, str(uid + 1))
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            if chat != owner:
                logger.debug("telegram inbound: ignoring chat %s", chat)
                continue
            text = str(msg.get("text") or "")
            if not text.startswith("/"):
                continue
            try:
                reply = handle_message(text)
            except Exception:  # noqa: BLE001
                logger.warning("telegram inbound handler failed", exc_info=True)
                continue
            tt.send_message(reply, convert_markdown=False, parse_mode="")
            handled += 1
        return handled
    except Exception:  # noqa: BLE001
        logger.warning("telegram inbound poll failed", exc_info=True)
        return 0


# ------------------------------------------------------------- aggregation
def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London") or "Europe/London"))


def _bucket(h: int) -> str:
    if h >= 22 or h < 7:
        return "night"
    if h < 12:
        return "morning"
    if h < 16:
        return "afternoon"
    if h < 19:
        return "peak"
    return "evening"


def _count(rows: list[dict[str, Any]], key) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        k = key(r)
        if k is None:
            continue
        out.setdefault(k, {"cold": 0, "ok": 0, "hot": 0})[r["verdict"]] += 1
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tz = _tz()
    for r in rows:
        at = datetime.fromisoformat(str(r["at_utc"]).replace("Z", "+00:00"))
        r["_local_h"] = at.astimezone(tz).hour
    n = len(rows)
    cnt = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("cold", "ok", "hot")}
    cold_in = [float(r["indoor_c"]) for r in rows if r["verdict"] == "cold" and r.get("indoor_c") is not None]
    return {
        "n": n, **cnt,
        "by_room": _count(rows, lambda r: r.get("room")),
        "by_band": _count(rows, lambda r: r.get("band")),
        "by_hour_bucket": _count(rows, lambda r: _bucket(r["_local_h"])),
        "mean_indoor_when_cold": round(sum(cold_in) / len(cold_in), 2) if cold_in else None,
        "proposal": _proposal(rows),
    }


def _proposal(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    from .. import runtime_settings as rs

    floor = float(rs.get_setting(NIGHT_FLOOR_KEY))
    coast = float(rs.get_setting(PEAK_COAST_KEY))
    cold = [r for r in rows if r["verdict"] == "cold"]
    hot = [r for r in rows if r["verdict"] == "hot"]
    night_cold = [r for r in cold if _bucket(r["_local_h"]) == "night"
                  and r.get("indoor_c") is not None and float(r["indoor_c"]) <= floor + 0.3]
    peak_cold = [r for r in cold if r.get("band") == "peak"]
    if len(night_cold) >= 2 and floor + STEP_C <= FLOOR_CAP_C + 1e-9:
        return {"key": NIGHT_FLOOR_KEY, "current": floor, "suggested": round(floor + STEP_C, 2),
                "reason": f"{len(night_cold)} 'cold' at night with indoor at/below floor+0.3 C"}
    if len(peak_cold) >= 2 and coast - STEP_C >= 0:
        return {"key": PEAK_COAST_KEY, "current": coast, "suggested": round(coast - STEP_C, 2),
                "reason": f"{len(peak_cold)} 'cold' in the peak band"}
    if len(hot) >= 3 and not cold:
        night_hot = sum(1 for r in hot if _bucket(r["_local_h"]) == "night")
        if night_hot * 2 >= len(hot) and floor - STEP_C >= FLOOR_MIN_C:
            return {"key": NIGHT_FLOOR_KEY, "current": floor, "suggested": round(floor - STEP_C, 2),
                    "reason": f"{len(hot)} 'hot', no 'cold' (mostly night)"}
        if coast + STEP_C <= COAST_MAX_C:
            return {"key": PEAK_COAST_KEY, "current": coast, "suggested": round(coast + STEP_C, 2),
                    "reason": f"{len(hot)} 'hot', no 'cold'"}
    return None


def weekly_summary(week_start: date) -> dict[str, Any]:
    """Aggregate the 7 LOCAL days starting ``week_start``."""
    tz = _tz()
    a = datetime(week_start.year, week_start.month, week_start.day, tzinfo=tz).astimezone(UTC)
    b = (datetime(week_start.year, week_start.month, week_start.day, tzinfo=tz) + timedelta(days=7)).astimezone(UTC)
    rows = db.get_comfort_feedback(since_utc=a.isoformat().replace("+00:00", "Z"),
                                   until_utc=b.isoformat().replace("+00:00", "Z"))
    out = summarize(rows)
    out["week_start"] = week_start.isoformat()
    return out


def external_comfort_signal(week_start: date) -> dict[str, Any] | None:
    """The week's bounded proposal (or None) — consumed by the suggestions story."""
    return weekly_summary(week_start)["proposal"]


def weekly_job(now: datetime | None = None) -> dict[str, Any] | None:
    """Sunday 08:45 local: log the trailing-7-day summary; apply the proposal
    ONLY when ``COMFORT_FEEDBACK_AUTO_TUNE`` is true. Never raises."""
    try:
        today = (now or datetime.now(UTC)).astimezone(_tz()).date()
        summ = weekly_summary(today - timedelta(days=6))
        db.log_action(device="comfort", action="weekly_summary", params=summ,
                      result="success", trigger="cron")
        prop = summ["proposal"]
        if prop and getattr(config, "COMFORT_FEEDBACK_AUTO_TUNE", False):
            from .. import runtime_settings as rs

            cur = float(rs.get_setting(prop["key"]))
            new = max(cur - STEP_C, min(cur + STEP_C, float(prop["suggested"])))
            rs.set_setting(prop["key"], new, actor="comfort_feedback_auto_tune")
            db.log_action(device="comfort", action="auto_tune",
                          params={**prop, "applied": new}, result="success", trigger="cron",
                          actor="comfort_feedback_auto_tune")
        return summ
    except Exception:  # noqa: BLE001
        logger.warning("comfort weekly job failed", exc_info=True)
        return None
