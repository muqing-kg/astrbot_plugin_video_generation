"""Logging helpers."""

from __future__ import annotations

from typing import Any


def log_prefix(scope: str, task_id: str | None = None) -> str:
    if task_id:
        return f"[VideoGen:{scope}:{task_id}]"
    return f"[VideoGen:{scope}]"


def safe_log_text(value: Any, limit: int = 200) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def mask_sensitive(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "****"
    return text[:4] + "****" + text[-4:]
