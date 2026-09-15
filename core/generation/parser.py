"""Command and prompt parameter parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..shared.constants import (
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
)

DURATION_TOKEN_RE = re.compile(
    r"^(?:(\d{1,2})\s*(?:s|sec|secs|second|seconds|秒)?)$",
    re.IGNORECASE,
)
RESOLUTION_TOKEN_RE = re.compile(r"^(\d{3,4}p|\d{1,2}k)$", re.IGNORECASE)
INLINE_DURATION_RE = re.compile(
    r"(?i)(?:时长\s*[:=]?\s*)?(\d{1,2})\s*(?:s|sec|secs|second|seconds|秒)\b|(?:duration\s*[:=]?\s*)(\d{1,2})\b"
)
INLINE_ASPECT_RE = re.compile(
    r"(?i)(\d{1,2})\s*[:xX/]\s*(\d{1,2})|(横屏|竖屏|方屏)"
)
INLINE_RESOLUTION_RE = re.compile(
    r"(?i)(?:分辨率\s*[:：]?\s*|resolution\s*[:=]?\s*)?(\d{3,4}p|\d{1,2}k)(?![a-z0-9])"
)


@dataclass
class ParsedVideoCommand:
    prompt: str
    duration: int
    aspect_ratio: str
    resolution: str
    aspect_explicit: bool
    duration_explicit: bool
    resolution_explicit: bool
    errors: list[str]


def normalize_aspect(raw: str) -> str | None:
    """Normalize a ratio token; any positive W:H pair is accepted (gateway-validated)."""
    text = (raw or "").strip()
    mapping = {"横屏": "16:9", "竖屏": "9:16", "方屏": "1:1"}
    if text in mapping:
        return mapping[text]
    match = re.fullmatch(r"(\d{1,3})[:xX/](\d{1,3})", text)
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return f"{width}:{height}"


def normalize_resolution(raw: str) -> str | None:
    """Normalize a resolution token like 720p / 1080P / 4k (gateway-validated)."""
    text = (raw or "").strip().lower()
    if RESOLUTION_TOKEN_RE.fullmatch(text):
        return text
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
    default_duration: int = 0,
    default_aspect_ratio: str = "",
    default_resolution: str = "",
) -> ParsedVideoCommand:
    text = (message or "").strip()
    for prefix in ("/视频", "／视频", "视频"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    duration = default_duration
    aspect_ratio = (default_aspect_ratio or "").strip()
    resolution = (default_resolution or "").strip()
    aspect_explicit = False
    duration_explicit = False
    resolution_explicit = False
    errors: list[str] = []

    # Leading tokens in any order may be duration / aspect / resolution.
    # The first token matching none of them ends the scan.
    tokens = text.split()
    consumed = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        maybe_duration = _parse_duration_token(token)
        if maybe_duration == -1:
            errors.append(
                f"秒数必须在 {MIN_DURATION_SECONDS} 到 {MAX_DURATION_SECONDS} 之间"
            )
            duration_explicit = True
            index += 1
            consumed = index
            continue
        if maybe_duration is not None:
            duration = maybe_duration
            duration_explicit = True
            index += 1
            consumed = index
            continue
        maybe_resolution = normalize_resolution(token)
        if maybe_resolution:
            resolution = maybe_resolution
            resolution_explicit = True
            index += 1
            consumed = index
            continue
        maybe_aspect = normalize_aspect(token)
        if maybe_aspect:
            aspect_ratio = maybe_aspect
            aspect_explicit = True
            index += 1
            consumed = index
            continue
        break
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
        duration_explicit = True
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

    if not resolution_explicit:
        match = INLINE_RESOLUTION_RE.search(prompt)
        if match:
            resolution = match.group(1).lower()
            resolution_explicit = True
            prompt = INLINE_RESOLUTION_RE.sub(" ", prompt)

    prompt = re.sub(r"\s+", " ", prompt).strip()
    if not prompt:
        errors.append("请提供视频提示词，图生视频也需要提示词")

    return ParsedVideoCommand(
        prompt=prompt,
        duration=duration,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        aspect_explicit=aspect_explicit,
        duration_explicit=duration_explicit,
        resolution_explicit=resolution_explicit,
        errors=errors,
    )
