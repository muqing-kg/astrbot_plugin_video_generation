"""Prompt presets: parse config entries, match names, combine prompts.

Mirrors the preset conventions of astrbot_plugin_image_generation:
- Simple format: ``名称:提示词`` (English or Chinese colon)
- Advanced format: ``名称:{"prompt":"...","aspect_ratio":"16:9","resolution":"720p","duration":6,"model":"..."}``
- ``/视频`` matches preset names consecutively from the start of the prompt,
  longest name first, each name applied at most once.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..shared.constants import UNSPECIFIED_TOKENS

_PRESET_SPLIT_RE = re.compile(r"[:：]")


@dataclass
class VideoPreset:
    name: str
    prompt: str
    aspect_ratio: str = ""
    resolution: str = ""
    duration: int | None = None
    model: str = ""

    @property
    def params_label(self) -> str:
        parts = []
        if self.aspect_ratio:
            parts.append(f"比例 {self.aspect_ratio}")
        if self.resolution:
            parts.append(f"分辨率 {self.resolution}")
        if self.duration:
            parts.append(f"{self.duration}s")
        if self.model:
            parts.append(f"模型 {self.model}")
        return " | ".join(parts)


def _clean(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in UNSPECIFIED_TOKENS else text


def _parse_advanced(content: str) -> tuple[str, str, str, int | None, str]:
    """Parse a JSON preset body -> (prompt, aspect, resolution, duration, model)."""
    try:
        data = json.loads(content)
    except Exception:
        return content, "", "", None, ""
    if not isinstance(data, dict):
        return content, "", "", None, ""
    prompt = str(data.get("prompt") or "").strip()
    try:
        duration = int(data.get("duration"))
    except (TypeError, ValueError):
        duration = None
    return (
        prompt,
        _clean(data.get("aspect_ratio")),
        _clean(data.get("resolution")),
        duration,
        _clean(data.get("model")),
    )


def parse_presets(entries: object) -> dict[str, VideoPreset]:
    """Parse a config list into {name: preset}; later entries overwrite."""
    if isinstance(entries, str):
        items: list[object] = [entries]
    elif isinstance(entries, (list, tuple)):
        items = list(entries)
    else:
        return {}

    presets: dict[str, VideoPreset] = {}
    for entry in items:
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("key") or "").strip()
            content = str(
                entry.get("prompt") or entry.get("content") or entry.get("value") or ""
            ).strip()
        else:
            text = str(entry or "").strip()
            if not text:
                continue
            parts = _PRESET_SPLIT_RE.split(text, maxsplit=1)
            if len(parts) != 2:
                continue
            name, content = parts[0].strip(), parts[1].strip()
        if not name or not content:
            continue
        prompt, aspect, resolution, duration, model = _parse_advanced(content)
        if not prompt:
            continue
        presets[name] = VideoPreset(
            name=name,
            prompt=prompt,
            aspect_ratio=aspect,
            resolution=resolution,
            duration=duration,
            model=model,
        )
    return presets


def match_presets(
    prompt: str, presets: dict[str, VideoPreset]
) -> tuple[list[VideoPreset], str]:
    """Match leading consecutive preset names; return (matched, remaining prompt)."""
    remaining = (prompt or "").strip()
    matched: list[VideoPreset] = []
    used: set[str] = set()
    while remaining:
        hit: str | None = None
        rest = ""
        for name in sorted(
            (n for n in presets if n not in used), key=len, reverse=True
        ):
            if remaining == name:
                hit, rest = name, ""
                break
            if remaining.startswith(name + " "):
                hit, rest = name, remaining[len(name) :].strip()
                break
        if hit is None:
            break
        matched.append(presets[hit])
        used.add(hit)
        remaining = rest
    return matched, remaining


def combine_prompt(matched: list[VideoPreset], extra: str) -> str:
    """Combine preset prompts and extra text.

    A single source keeps its original text; multiple sources get lightweight
    block labels so the model can tell the sources apart.
    """
    extra = (extra or "").strip()
    preset_parts = [p.prompt for p in matched if p.prompt.strip()]
    if not preset_parts:
        return extra
    if not extra:
        if len(preset_parts) == 1:
            return preset_parts[0]
        return "[预设提示词]\n" + "\n".join(preset_parts)
    if len(preset_parts) == 1:
        return f"{preset_parts[0]}\n\n{extra}"
    return "[预设提示词]\n" + "\n".join(preset_parts) + f"\n\n[附加提示词]\n{extra}"
