"""Command and prompt parameter parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..shared.constants import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_DURATION_SECONDS,
    DEFAULT_RESOLUTION,
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    SUPPORTED_ASPECT_RATIOS,
)

DURATION_TOKEN_RE = re.compile(
    r"^(?:(\d{1,2})\s*(?:s|sec|secs|second|seconds|秒)?)$",
    re.IGNORECASE,
)
INLINE_DURATION_RE = re.compile(
    r"(?i)(?:时长\s*[:=]?\s*)?(\d{1,2})\s*(?:s|sec|secs|second|seconds|秒)\b|(?:duration\s*[:=]?\s*)(\d{1,2})\b"
)
INLINE_ASPECT_RE = re.compile(
    r"(?i)(\d{1,2})\s*[:xX/]\s*(\d{1,2})|(横屏|竖屏|方屏)"
)


@dataclass
class ParsedVideoCommand:
    prompt: str
    duration: int
    aspect_ratio: str
    resolution: str
    aspect_explicit: bool
    errors: list[str]


def normalize_aspect(raw: str) -> str | None:
    text = (raw or "").strip()
    mapping = {"横屏": "16:9", "竖屏": "9:16", "方屏": "1:1"}
    if text in mapping:
        return mapping[text]
    match = re.fullmatch(r"(\d{1,2})[:xX/](\d{1,2})", text)
    if not match:
        return None
    value = f"{int(match.group(1))}:{int(match.group(2))}"
    if value in SUPPORTED_ASPECT_RATIOS:
        return value
    return None


def _parse_duration_token(token: str) -> int | None:
    match = DURATION_TOKEN_RE.fullmatch(token.strip())
    if not match:
        return None
    value = int(match.group(1))
    if MIN_DURATION_SECONDS <= value <= MAX_DURATION_SECONDS:
        return value
    return -1


def parse_video_command(
    message: str,
    *,
    default_duration: int = DEFAULT_DURATION_SECONDS,
    default_aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    default_resolution: str = DEFAULT_RESOLUTION,
) -> ParsedVideoCommand:
    text = (message or "").strip()
    for prefix in ("/视频", "／视频", "视频"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    duration = default_duration
    aspect_ratio = (
        default_aspect_ratio
        if default_aspect_ratio in SUPPORTED_ASPECT_RATIOS
        else DEFAULT_ASPECT_RATIO
    )
    resolution = default_resolution
    aspect_explicit = False
    errors: list[str] = []

    tokens = text.split()
    consumed = 0
    if tokens:
        maybe_duration = _parse_duration_token(tokens[0])
        if maybe_duration == -1:
            errors.append(
                f"秒数必须在 {MIN_DURATION_SECONDS} 到 {MAX_DURATION_SECONDS} 之间"
            )
            consumed = 1
        elif maybe_duration is not None:
            duration = maybe_duration
            consumed = 1
            if len(tokens) > 1:
                maybe_aspect = normalize_aspect(tokens[1])
                if maybe_aspect:
                    aspect_ratio = maybe_aspect
                    aspect_explicit = True
                    consumed = 2
    prompt = " ".join(tokens[consumed:]).strip() if tokens else text

    inline_duration = None
    for match in INLINE_DURATION_RE.finditer(prompt):
        raw = match.group(1) or match.group(2)
        if not raw:
            continue
        value = int(raw)
        if MIN_DURATION_SECONDS <= value <= MAX_DURATION_SECONDS:
            inline_duration = value
        else:
            errors.append(
                f"提示词中的秒数必须在 {MIN_DURATION_SECONDS} 到 {MAX_DURATION_SECONDS} 之间"
            )
    if inline_duration is not None and consumed == 0:
        duration = inline_duration
        prompt = INLINE_DURATION_RE.sub(" ", prompt)

    inline_aspect = None
    for match in INLINE_ASPECT_RE.finditer(prompt):
        if match.group(3):
            inline_aspect = normalize_aspect(match.group(3))
        elif match.group(1) and match.group(2):
            inline_aspect = normalize_aspect(f"{match.group(1)}:{match.group(2)}")
        if inline_aspect:
            break
    if inline_aspect and not aspect_explicit:
        aspect_ratio = inline_aspect
        aspect_explicit = True
        prompt = INLINE_ASPECT_RE.sub(" ", prompt)

    prompt = re.sub(r"\s+", " ", prompt).strip()
    if not prompt:
        errors.append("请提供视频提示词，图生视频也需要提示词")

    return ParsedVideoCommand(
        prompt=prompt,
        duration=duration,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        aspect_explicit=aspect_explicit,
        errors=errors,
    )
