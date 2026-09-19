"""Video send-plan builder shared by QQ and WeChat bridges.

The bridge on the other side cannot read AstrBot's container-local paths
(WeChat answered 「视频/文件内容无效或下载失败」 for a plain /AstrBot/...
path), so every candidate here either embeds the video bytes itself or
points at a URL the bridge can fetch. None of them proxies the video as
an image or a voice segment.
"""

from __future__ import annotations

import base64
from pathlib import Path

# Ceiling for inline base64 payloads; bridges reject huge media anyway and
# the encoded string must fit in one message body.
MAX_INLINE_VIDEO_BYTES = 50 * 1024 * 1024


def build_video_candidates(
    comp,
    path: Path,
    result_url: str = "",
    *,
    callback_configured: bool = False,
    allow_inline: bool = True,
    max_inline_bytes: int = MAX_INLINE_VIDEO_BYTES,
) -> list:
    """Return ordered components that each carry the video itself.

    Order: base64 inline Video -> file URI Video -> abs path Video ->
    http(s) URL Video -> File attachment. Construction failures of single
    candidates are skipped so one broken component class cannot empty the
    whole plan.

    callback_configured: AstrBot callback_api_base is set, so Video.to_dict()
    registers local files into the callback file service and must NOT receive
    base64:// (it would treat it as a path).
    allow_inline: False when upstream MP4 hygiene failed (e.g. C2PA boxes
    could not be stripped); dirty bytes inline get rejected by bridges.
    """
    candidates: list = []
    video_cls = getattr(comp, "Video", None)

    def try_append(builder) -> None:
        try:
            candidates.append(builder())
        except Exception:
            pass

    if video_cls is not None:
        abs_path = str(Path(path).resolve(strict=False))
        if allow_inline and not callback_configured:
            try:
                size = Path(path).stat().st_size
                if 0 < size <= max_inline_bytes:
                    bs64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
                    try_append(lambda: video_cls.fromBase64(bs64))
            except OSError:
                pass
        if callable(getattr(video_cls, "fromFileSystem", None)):
            try_append(lambda: video_cls.fromFileSystem(abs_path))
        try_append(lambda: video_cls(file=abs_path, path=abs_path))
        if result_url and result_url.startswith(("http://", "https://")):
            if callable(getattr(video_cls, "fromURL", None)):
                try_append(lambda: video_cls.fromURL(result_url))

    file_cls = getattr(comp, "File", None)
    if file_cls is not None:
        try_append(
            lambda: file_cls(
                name=Path(path).name, file=str(Path(path).resolve(strict=False))
            )
        )

    return candidates
