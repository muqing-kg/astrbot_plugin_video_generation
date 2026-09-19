"""Pick the single proven video send form per platform.

NapCat (QQ) decodes base64:// into its own temp dir — no shared filesystem and
no global config needed. The WeChat bridge neither reads AstrBot's local paths
nor decodes base64: it only fetches http(s) URLs, which AstrBot's callback file
service provides when callback_api_base is configured. Fallback chains sent
duplicate videos when a bridge ack came back slow, so there is deliberately
exactly ONE form per situation here and no chain.
"""

from __future__ import annotations

import base64
from pathlib import Path

# Ceiling for the inline base64 payload; the encoded string must fit in one
# message body and is held in memory while encoding.
MAX_INLINE_VIDEO_BYTES = 512 * 1024 * 1024


def build_video_component(
    comp,
    path: Path,
    *,
    callback_configured: bool,
    is_qq: bool,
    max_inline_bytes: int = MAX_INLINE_VIDEO_BYTES,
) -> tuple[object, str]:
    """Return (video component, form name); (None, reason) when no form applies.

    - callback configured: local-path Video. Video.to_dict() registers it into
      the callback file service and hands the bridge an http URL it downloads
      itself. Proven on both NapCat and the WeChat bridge.
    - no callback + QQ: base64 inline Video; NapCat decodes it into its own
      temp dir, so it works without shared filesystem or any config.
    - no callback + non-QQ: no form works — the bridge cannot read local paths
      and does not decode base64. Caller should surface the config gap.
    """
    video_cls = getattr(comp, "Video", None)
    if video_cls is None:
        return None, "消息组件库不含 Video 类"
    abs_path = str(Path(path).resolve(strict=False))

    if callback_configured:
        if callable(getattr(video_cls, "fromFileSystem", None)):
            return video_cls.fromFileSystem(abs_path), "callback-url"
        return None, "消息组件库缺少 Video.fromFileSystem"

    if not is_qq:
        return (
            None,
            "未配置 AstrBot 全局 callback_api_base：微信桥既读不到本地路径也不解码 base64，"
            "请配置后重试",
        )

    if not callable(getattr(video_cls, "fromBase64", None)):
        return None, "消息组件库缺少 Video.fromBase64"
    try:
        size = Path(path).stat().st_size
        if size > max_inline_bytes:
            return None, f"视频 {size} 字节超过 base64 内联上限 {max_inline_bytes}"
        bs64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    except OSError as exc:
        return None, f"本地视频不可读: {exc}"
    return video_cls.fromBase64(bs64), "base64"
