"""Pure media helpers (stdlib-only, safe to self-check without AstrBot)."""

from __future__ import annotations

import re


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
