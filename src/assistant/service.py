"""Assistant service: context building, rule-based and LLM suggestions, action validation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..config import config

# Allowed action types (must match OpenClawAction / OPENCLAW_CAPABILITIES)
ALLOWED_ACTIONS = {
    "daikin.power",
    "daikin.temperature",
    "daikin.lwt_offset",
    "daikin.mode",
    "daikin.tank_temperature",
    "daikin.tank_power",
    "foxess.mode",
    "foxess.charge_period",
}

# Parameter constraints per action (min/max/enum)
ACTION_PARAMS = {
    "daikin.power": {"on": ("bool", None, None)},
    "daikin.temperature": {"temperature": ("number", 15, 30), "mode": ("string", None, None)},
    "daikin.lwt_offset": {"offset": ("number", -10, 10), "mode": ("string", None, None)},
    "daikin.mode": {"mode": ("string", None, None)},  # enum: heating, cooling, auto, etc.
    "daikin.tank_temperature": {"temperature": ("number", 30, 60)},
    "daikin.tank_power": {"on": ("bool", None, None)},
    "foxess.mode": {"mode": ("string", None, None)},  # enum
    "foxess.charge_period": {
        "start_time": ("string", None, None),
        "end_time": ("string", None, None),
        "target_soc": ("integer", 10, 100),
        "period_index": ("integer", 0, 1),
    },
}

VALID_DAIKIN_MODES = {"heating", "cooling", "auto", "fan_only", "dry"}
VALID_FOXESS_MODES = {"Self Use", "Feed-in Priority", "Back Up", "Force charge", "Force discharge"}


@dataclass
class SuggestedAction:
    """A single suggested action with optional reason."""
    action: str
    parameters: dict[str, Any]
    reason: Optional[str] = None


def build_context(
    daikin_status_list: list[dict],
    foxess_status: Optional[dict],
    tariff: Optional[dict],
) -> dict:
    """Build a JSON-serializable context for the assistant."""
    ctx = {
        "daikin": [
            {
                "device_id": d.get("device_id"),
                "device_name": d.get("device_name"),
                "is_on": d.get("is_on"),
                "mode": d.get("mode"),
                "room_temp": d.get("room_temp"),
                "target_temp": d.get("target_temp"),
                "outdoor_temp": d.get("outdoor_temp"),
                "lwt": d.get("lwt"),
                "lwt_offset": d.get("lwt_offset"),
                "tank_temp": d.get("tank_temp"),
                "tank_target": d.get("tank_target"),
                "weather_regulation": d.get("weather_regulation"),
            }
            for d in daikin_status_list
        ],
        "foxess": None,
        "tariff": None,
    }
    if foxess_status:
        ctx["foxess"] = {
            "soc": foxess_status.get("soc"),
            "solar_power": foxess_status.get("solar_power"),
            "grid_power": foxess_status.get("grid_power"),
            "battery_power": foxess_status.get("battery_power"),
            "load_power": foxess_status.get("load_power"),
            "work_mode": foxess_status.get("work_mode"),
        }
    if tariff:
        ctx["tariff"] = {
            "import_rate_pence_per_kwh": tariff.get("import_rate"),
            "export_rate_pence_per_kwh": tariff.get("export_rate"),
            "tariff_name": tariff.get("tariff_name"),
        }
    return ctx


def get_suggestions(
    context: dict,
    preference: str,
    user_message: Optional[str] = None,
) -> tuple[str, list[SuggestedAction]]:
    """
    Get assistant reply and suggested actions.
    preference: "comfort" | "balanced" | "savings"
    """
    if config.ANTHROPIC_API_KEY and config.AI_ASSISTANT_PROVIDER == "anthropic":
        return _get_suggestions_anthropic(context, preference, user_message or "")
    if config.OPENAI_API_KEY and config.AI_ASSISTANT_PROVIDER == "openai":
        return _get_suggestions_llm(context, preference, user_message or "")
    return _get_suggestions_rule_based(context, preference, user_message or "")


def _get_suggestions_rule_based(
    context: dict,
    preference: str,
    user_message: str,
) -> tuple[str, list[SuggestedAction]]:
    """Rule-based suggestions when no LLM is configured."""
    actions: list[SuggestedAction] = []
    daikin = (context.get("daikin") or [])[:1]
    foxess = context.get("foxess")

    if preference == "comfort":
        if daikin and daikin[0].get("is_on"):
            target = daikin[0].get("target_temp")
            if target is not None and target < 21:
                actions.append(SuggestedAction(
                    action="daikin.temperature",
                    parameters={"temperature": 21.0},
                    reason="Raise target to 21°C for more comfort.",
                ))
        if foxess and foxess.get("work_mode") != "Self Use":
            actions.append(SuggestedAction(
                action="foxess.mode",
                parameters={"mode": "Self Use"},
                reason="Self Use keeps battery for your own consumption.",
            ))
        reply = "I've suggested settings that prioritise comfort: slightly higher heating target and Self Use for the battery. Review and apply below if you agree."
    elif preference == "savings":
        if daikin and daikin[0].get("is_on"):
            target = daikin[0].get("target_temp")
            if target is not None and target > 19:
                actions.append(SuggestedAction(
                    action="daikin.temperature",
                    parameters={"temperature": 19.0},
                    reason="Lower target to 19°C to save energy.",
                ))
            if daikin[0].get("weather_regulation") and daikin[0].get("lwt_offset") is not None:
                offset = daikin[0].get("lwt_offset", 0)
                if offset > -2:
                    actions.append(SuggestedAction(
                        action="daikin.lwt_offset",
                        parameters={"offset": max(-10, offset - 2)},
                        reason="Slightly lower LWT offset to reduce heating cost.",
                    ))
        if foxess:
            soc = foxess.get("soc") or 0
            solar = foxess.get("solar_power") or 0
            if soc >= 95 and solar > 0.5:
                actions.append(SuggestedAction(
                    action="foxess.mode",
                    parameters={"mode": "Feed-in Priority"},
                    reason="Battery is full and solar is generating; export surplus to grid.",
                ))
            elif soc < 30:
                actions.append(SuggestedAction(
                    action="foxess.charge_period",
                    parameters={
                        "start_time": "00:30",
                        "end_time": "04:30",
                        "target_soc": 80,
                        "period_index": 0,
                    },
                    reason="Charge battery during typical off-peak hours (adjust times to your tariff).",
            ))
        reply = "I've suggested settings to save cost: lower heating setpoint, optional LWT reduction, and battery charge/export suggestions. Review and apply below."
    else:
        # balanced
        if daikin and daikin[0].get("is_on"):
            target = daikin[0].get("target_temp")
            if target is not None and (target > 21 or target < 19):
                actions.append(SuggestedAction(
                    action="daikin.temperature",
                    parameters={"temperature": 20.0},
                    reason="Set to 20°C for a balance of comfort and cost.",
                ))
        if foxess and foxess.get("work_mode") not in ("Self Use", "Feed-in Priority"):
            actions.append(SuggestedAction(
                action="foxess.mode",
                parameters={"mode": "Self Use"},
                reason="Self Use is a good default for balanced operation.",
            ))
        reply = "I've suggested balanced settings: 20°C heating and Self Use for the battery. Review and apply below."
    return reply, actions



def _get_suggestions_anthropic(
    context: dict,
    preference: str,
    user_message: str,
) -> tuple[str, list[SuggestedAction]]:
    """Call Anthropic Claude and parse reply + JSON actions."""
    try:
        import anthropic as anthropic_sdk
    except ImportError:
        return _get_suggestions_rule_based(context, preference, user_message)

    client = anthropic_sdk.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    preference_instruction = {
        "comfort": "Prioritise comfort: higher or maintained temperatures, avoid aggressive setbacks.",
        "balanced": "Balance comfort and cost: moderate setpoints, one charge window on cheap rate if applicable.",
        "savings": "Prioritise cost savings: lower setpoints, setbacks, charge only in cheap/solar windows, suggest Feed-in when battery full and solar high.",
    }.get(preference, preference)

    system = """You are an expert home energy assistant. The user has a Daikin Altherma heat pump and a Fox ESS battery/inverter.

Your task: given the current system state (JSON), suggest a short list of concrete actions to optimize for the user's preference (comfort vs cost). Only suggest actions that change something (e.g. if target_temp is already 21, do not suggest setting it to 21).

Allowed actions and parameters (use exactly these keys):
- daikin.power: {"on": true|false}
- daikin.temperature: {"temperature": 15-30} (only if weather_regulation is false)
- daikin.lwt_offset: {"offset": -10 to 10} (when weather regulation is active)
- daikin.mode: {"mode": "heating"|"cooling"|"auto"|"fan_only"|"dry"}
- daikin.tank_temperature: {"temperature": 30-60}
- daikin.tank_power: {"on": true|false}
- foxess.mode: {"mode": "Self Use"|"Feed-in Priority"|"Back Up"|"Force charge"|"Force discharge"}
- foxess.charge_period: {"start_time": "HH:MM", "end_time": "HH:MM", "target_soc": 10-100, "period_index": 0|1}

Respond with:
1. A short friendly explanation (1-3 sentences) for the user.
2. A JSON array of suggested actions, each of the form: {"action": "<action_type>", "parameters": {...}, "reason": "<short reason>"}.

Put the JSON array in a fenced code block with language "json". If no changes are needed, return an empty array [] in the JSON block. Suggest at most 4 actions."""

    user_content = f"Preference: {preference_instruction}\n\nCurrent state:\n{json.dumps(context, indent=2)}\n\n"
    if user_message.strip():
        user_content += f"User message: {user_message}\n\n"
    user_content += "Reply with your explanation and a json code block of suggested actions."

    try:
        response = client.messages.create(
            model=config.AI_ASSISTANT_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        text = (response.content[0].text if response.content else "").strip()
    except Exception as e:
        return f"I couldn't reach the AI service ({e}). Here are rule-based suggestions instead.", _get_suggestions_rule_based(context, preference, user_message)[1]

    actions = _parse_actions_from_response(text)
    reply = _strip_json_block_from_reply(text)
    validated = validate_suggested_actions(actions)
    return reply, [SuggestedAction(a.action, a.parameters, a.reason) for a in validated]


def _get_suggestions_llm(
    context: dict,
    preference: str,
    user_message: str,
) -> tuple[str, list[SuggestedAction]]:
    """Call OpenAI and parse reply + JSON actions."""
    try:
        from openai import OpenAI
    except ImportError:
        return _get_suggestions_rule_based(context, preference, user_message)

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    preference_instruction = {
        "comfort": "Prioritise comfort: higher or maintained temperatures, avoid aggressive setbacks.",
        "balanced": "Balance comfort and cost: moderate setpoints, one charge window on cheap rate if applicable.",
        "savings": "Prioritise cost savings: lower setpoints, setbacks, charge only in cheap/solar windows, suggest Feed-in when battery full and solar high.",
    }.get(preference, preference)

    system = """You are an expert home energy assistant. The user has a Daikin Altherma heat pump and a Fox ESS battery/inverter.

Your task: given the current system state (JSON), suggest a short list of concrete actions to optimize for the user's preference (comfort vs cost). Only suggest actions that change something (e.g. if target_temp is already 21, do not suggest setting it to 21).

Allowed actions and parameters (use exactly these keys):
- daikin.power: {"on": true|false}
- daikin.temperature: {"temperature": 15-30} (only if weather_regulation is false)
- daikin.lwt_offset: {"offset": -10 to 10} (when weather regulation is active)
- daikin.mode: {"mode": "heating"|"cooling"|"auto"|"fan_only"|"dry"}
- daikin.tank_temperature: {"temperature": 30-60}
- daikin.tank_power: {"on": true|false}
- foxess.mode: {"mode": "Self Use"|"Feed-in Priority"|"Back Up"|"Force charge"|"Force discharge"}
- foxess.charge_period: {"start_time": "HH:MM", "end_time": "HH:MM", "target_soc": 10-100, "period_index": 0|1}

Respond with:
1. A short friendly explanation (1-3 sentences) for the user.
2. A JSON array of suggested actions, each of the form: {"action": "<action_type>", "parameters": {...}, "reason": "<short reason>"}.

Put the JSON array in a fenced code block with language "json", e.g.:
```json
[
  {"action": "daikin.temperature", "parameters": {"temperature": 19}, "reason": "Lower at night to save."}
]
```

If no changes are needed, return an empty array [] in the JSON block. Suggest at most 4 actions."""

    user_content = f"Preference: {preference_instruction}\n\nCurrent state:\n{json.dumps(context, indent=2)}\n\n"
    if user_message.strip():
        user_content += f"User message: {user_message}\n\n"
    user_content += "Reply with your explanation and a json code block of suggested actions."

    try:
        response = client.chat.completions.create(
            model=config.AI_ASSISTANT_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
        )
        text = (response.choices[0].message.content or "").strip()
    except Exception as e:
        return f"I couldn't reach the AI service ({e}). Here are rule-based suggestions instead.", _get_suggestions_rule_based(context, preference, user_message)[1]

    actions = _parse_actions_from_response(text)
    reply = _strip_json_block_from_reply(text)
    validated = validate_suggested_actions(actions)
    return reply, [SuggestedAction(a.action, a.parameters, a.reason) for a in validated]


def _parse_actions_from_response(text: str) -> list[SuggestedAction]:
    """Extract JSON array of actions from markdown code block."""
    actions: list[SuggestedAction] = []
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if not match:
        return actions
    try:
        data = json.loads(match.group(1).strip())
        if not isinstance(data, list):
            return actions
        for item in data:
            if not isinstance(item, dict):
                continue
            action = item.get("action")
            params = item.get("parameters")
            if action and isinstance(params, dict):
                actions.append(SuggestedAction(
                    action=str(action),
                    parameters=params,
                    reason=item.get("reason"),
                ))
    except json.JSONDecodeError:
        pass
    return actions


def _strip_json_block_from_reply(text: str) -> str:
    """Remove the json code block from the reply so we show only the explanation."""
    return re.sub(r"\s*```(?:json)?\s*[\s\S]*?```\s*", "", text).strip() or "Here are my suggestions."


def validate_suggested_actions(actions: list[SuggestedAction]) -> list[SuggestedAction]:
    """Validate and normalize actions; return only allowed ones with valid parameters."""
    result: list[SuggestedAction] = []
    for a in actions:
        if a.action not in ALLOWED_ACTIONS:
            continue
        params_spec = ACTION_PARAMS.get(a.action, {})
        normalized: dict[str, Any] = {}
        for key, spec in params_spec.items():
            if key not in a.parameters and key != "mode":
                if key == "period_index":
                    normalized["period_index"] = a.parameters.get("period_index", 0)
                continue
            val = a.parameters.get(key)
            if val is None and key in ("mode",):
                continue
            typ, lo, hi = spec
            if typ == "bool":
                normalized[key] = bool(val)
            elif typ == "number":
                try:
                    v = float(val)
                    if lo is not None and v < lo:
                        v = lo
                    if hi is not None and v > hi:
                        v = hi
                    normalized[key] = v
                except (TypeError, ValueError):
                    continue
            elif typ == "integer":
                try:
                    v = int(val)
                    if lo is not None and v < lo:
                        v = lo
                    if hi is not None and v > hi:
                        v = hi
                    normalized[key] = v
                except (TypeError, ValueError):
                    continue
            elif typ == "string":
                s = str(val).strip()
                if key == "mode" and a.action == "daikin.mode":
                    if s not in VALID_DAIKIN_MODES:
                        continue
                elif key == "mode" and a.action == "foxess.mode":
                    if s not in VALID_FOXESS_MODES:
                        continue
                elif key in ("start_time", "end_time"):
                    if not re.match(r"^\d{2}:\d{2}$", s):
                        continue
                normalized[key] = s
        if a.action == "daikin.temperature" and "temperature" not in normalized:
            continue
        if a.action == "daikin.lwt_offset" and "offset" not in normalized:
            continue
        if a.action == "foxess.charge_period":
            if not all(k in normalized for k in ("start_time", "end_time", "target_soc")):
                continue
            normalized.setdefault("period_index", 0)
        result.append(SuggestedAction(action=a.action, parameters=normalized, reason=a.reason))
    return result
