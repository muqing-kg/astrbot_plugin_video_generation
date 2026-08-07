"""Prompt enhancement helpers."""

from __future__ import annotations


def build_enhanced_video_prompt(
    prompt: str,
    *,
    has_reference_image: bool,
    enabled: bool,
    common_enhancement: str,
    image_enhancement: str,
    text_enhancement: str,
) -> str:
    """Append configurable enhancement text after the user prompt."""
    base = (prompt or "").strip()
    if not enabled:
        return base

    parts: list[str] = []
    common = (common_enhancement or "").strip()
    if common:
        parts.append(common)

    extra = (
        (image_enhancement or "").strip()
        if has_reference_image
        else (text_enhancement or "").strip()
    )
    if extra:
        parts.append(extra)

    if not parts:
        return base
    if not base:
        return " ".join(parts)
    return f"{base}\n\n{' '.join(parts)}"
