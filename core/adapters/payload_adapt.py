"""Adapt video-create payloads from upstream 4xx validation feedback.

OpenAI-style video gateways disagree on field shapes: grok2api wants
``image`` as an object ``{"url": ...}`` while Go-based gateways (new-api and
similar Seedance/Kling/Sora relays) want it as a plain string; Sora-style APIs
use ``seconds`` instead of ``duration`` and lack ``aspect_ratio``. Instead of
hardcoding providers, the adapter re-sends a mutated payload when the upstream
response body explicitly names the offending field.

This module is stdlib-only so it can be self-checked without AstrBot.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Go gateways: json: cannot unmarshal object into Go struct field .Alias.image of type string
FIELD_TYPE_ERROR_RE = re.compile(
    r"cannot unmarshal \S+ into Go struct field (?:[\w.]*?\.)?(\w+) of type (\w+)"
)
# Go gateways: json: unknown field "aspect_ratio"
UNKNOWN_FIELD_RE = re.compile(r"unknown field \"(\w+)\"")
# Generic fallbacks: 'image' must be a string / `duration` must be a number
SIMPLE_FIELD_TYPE_RE = re.compile(
    r"[`'\"](\w+)[`'\"]\s+(?:must|should)\s+be\s+(?:a |an )?(string|number|integer|float|double)"
)

# Rename hints for common field-name disagreements: field -> (new_name, coerce_to)
RENAME_HINTS: dict[str, tuple[str, str | None]] = {
    "aspect_ratio": ("ratio", None),
    "duration": ("seconds", "string"),
}

# Params the plugin may DROP (falling back to the model default) when the
# upstream rejects their value, e.g. "duration must be one of [5, 10]".
DROPPABLE_FIELDS = (
    "duration",
    "seconds",
    "aspect_ratio",
    "ratio",
    "resolution",
    "size",
)
# Keywords indicating a value/validation complaint (substring match).
_CONSTRAINT_KEYWORDS = (
    "invalid",
    "unsupported",
    "not support",
    "must be",
    "should be",
    "allowed",
    "supported",
    "valid",
    "expect",
    "不支持",
    "无效",
    "非法",
    "只能是",
    "必须",
)


def _error_text(body: str) -> str:
    """Pull nested error messages out of a JSON body, keeping the raw text."""
    text = body or ""
    try:
        data = json.loads(text)
    except Exception:
        return text
    if not isinstance(data, dict):
        return text
    parts = [text]

    def collect(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for key in ("message", "error", "msg", "detail"):
                if key in value:
                    collect(value[key])

    collect(data)
    return " | ".join(parts)


def _flatten_image(value: Any) -> str | None:
    """{"url": X} / {"image_url": X} -> X; None when not flattenable."""
    if isinstance(value, dict):
        for key in ("url", "image_url", "data"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _coerce(value: Any, type_name: str) -> Any:
    """Coerce a payload value to the type the upstream demanded, or None."""
    lowered = (type_name or "").lower()
    if "string" in lowered:
        if isinstance(value, str):
            return None  # already correct; error is about something else
        if isinstance(value, dict):
            return _flatten_image(value)
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (int, float)):
            return str(value)
        return None
    if "int" in lowered or "float" in lowered or "double" in lowered or "number" in lowered:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return None  # already numeric
        if isinstance(value, str):
            try:
                return float(value) if ("float" in lowered or "double" in lowered) else int(
                    float(value)
                )
            except ValueError:
                return None
        if isinstance(value, dict):
            return None
    return None


def aspect_to_size(aspect: str, resolution: str) -> str | None:
    """Map W:H + resolution to a Sora-style "WxH" size string.

    720p is the default base; 480p/1080p/2k/4k adjust it. Returns None when
    no usable ratio is present. Even dimensions keep encoders happy.
    """
    match = re.fullmatch(r"(\d{1,3}):(\d{1,3})", (aspect or "").strip())
    if not match:
        return None
    ratio_w, ratio_h = int(match.group(1)), int(match.group(2))
    if ratio_w <= 0 or ratio_h <= 0:
        return None
    res = (resolution or "").strip().lower()
    if res.startswith("480"):
        base = 480
    elif res.startswith("1080"):
        base = 1080
    elif res.startswith("1440") or res == "2k":
        base = 1440
    elif res.startswith("2160") or res == "4k":
        base = 2160
    else:
        base = 720
    if ratio_w >= ratio_h:
        height = base
        width = int(round(base * ratio_w / ratio_h / 2.0)) * 2
    else:
        width = base
        height = int(round(base * ratio_h / ratio_w / 2.0)) * 2
    return f"{width}x{height}"


def adapt_payload(payload: dict[str, Any], body: str) -> dict[str, Any] | None:
    """Return a mutated copy of ``payload`` per the validation error, else None.

    Only one rule is applied per call; the caller retries against the same
    create URL and may apply further rules when the next attempt still fails.
    """
    text = _error_text(body)

    match = FIELD_TYPE_ERROR_RE.search(text) or SIMPLE_FIELD_TYPE_RE.search(text)
    if match:
        field, type_name = match.group(1), match.group(2)
        if field in payload:
            new_value = _coerce(payload[field], type_name)
            if new_value is not None and new_value != payload[field]:
                adapted = dict(payload)
                adapted[field] = new_value
                return adapted
        return None

    unknown = UNKNOWN_FIELD_RE.search(text)
    if unknown:
        field = unknown.group(1)
        if field in payload:
            rename = RENAME_HINTS.get(field)
            if rename and rename[0] not in payload:
                adapted: dict[str, Any] = {}
                for key, value in payload.items():
                    if key == field:
                        adapted[rename[0]] = (
                            str(value) if rename[1] == "string" else value
                        )
                    else:
                        adapted[key] = value
                return adapted
            return {k: v for k, v in payload.items() if k != field}

    # Value rejected (type/shape is fine but the value is not supported):
    # drop the named param field so the model applies its own default.
    lowered = text.lower()
    if any(keyword in lowered for keyword in _CONSTRAINT_KEYWORDS):
        for field in DROPPABLE_FIELDS:
            if field in payload and re.search(
                rf"\b{re.escape(field)}\b", text, re.IGNORECASE
            ):
                return {k: v for k, v in payload.items() if k != field}
    return None
