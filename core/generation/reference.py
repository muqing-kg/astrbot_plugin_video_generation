"""Reference image helpers with QQ/WeChat reply support and aspect detection."""

from __future__ import annotations

import base64
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger

try:
    import astrbot.api.message_components as Comp
except Exception:  # pragma: no cover
    Comp = None  # type: ignore

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore

from ..shared.constants import SUPPORTED_ASPECT_RATIOS
from ..shared.logging import log_prefix, safe_log_text
from ..shared.types import ImageData

LOG = log_prefix("Reference")


def guess_mime(filename: str | None, default: str = "image/jpeg") -> str:
    if not filename:
        return default
    mime, _ = mimetypes.guess_type(filename)
    return mime or default


def image_to_data_url(image: ImageData) -> str:
    encoded = base64.b64encode(image.data).decode("ascii")
    mime = image.mime_type or "image/jpeg"
    return f"data:{mime};base64,{encoded}"


def detect_image_aspect_ratio(image: ImageData) -> str | None:
    """Detect nearest supported video aspect ratio from image pixels."""
    if Image is None or not image or not image.data:
        return None
    try:
        with Image.open(BytesIO(image.data)) as img:
            width, height = img.size
    except Exception as exc:
        logger.warning(f"{LOG} 探测图片比例失败: {safe_log_text(exc)}")
        return None
    if width <= 0 or height <= 0:
        return None

    value = width / height
    best = None
    best_score = None
    for ratio in SUPPORTED_ASPECT_RATIOS:
        w_s, h_s = ratio.split(":")
        target = int(w_s) / int(h_s)
        score = abs(value - target) / target
        if best_score is None or score < best_score:
            best_score = score
            best = ratio
    # Only accept reasonably close matches; otherwise fall back to orientation.
    if best is not None and best_score is not None and best_score <= 0.18:
        return best
    if value >= 1.15:
        return "16:9"
    if value <= 0.87:
        return "9:16"
    return "1:1"


async def download_image(url: str, *, max_bytes: int, timeout: int = 30) -> ImageData | None:
    try:
        # local path
        path = Path(url)
        if path.exists() and path.is_file():
            data = path.read_bytes()
            if len(data) > max_bytes:
                return None
            return ImageData(data=data, mime_type=guess_mime(path.name), source_url=str(path))

        if url.startswith("file://"):
            local = url[7:]
            # Windows file:///C:/...
            if local.startswith("/") and len(local) > 3 and local[2] == ":":
                local = local[1:]
            path = Path(local)
            if path.exists() and path.is_file():
                data = path.read_bytes()
                if len(data) > max_bytes:
                    return None
                return ImageData(data=data, mime_type=guess_mime(path.name), source_url=str(path))

        if url.startswith("data:image") and ";base64," in url:
            header, b64 = url.split(",", 1)
            mime = header[5:].split(";")[0] or "image/jpeg"
            data = base64.b64decode(b64)
            if len(data) > max_bytes:
                return None
            return ImageData(data=data, mime_type=mime, source_url=None)

        if not (url.startswith("http://") or url.startswith("https://")):
            return None

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if len(data) > max_bytes:
                    return None
                mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
                if not mime.startswith("image/"):
                    mime = guess_mime(urlparse(url).path)
                # Keep the URL as source only when a remote video API could
                # fetch it itself; otherwise force the data-URL fallback.
                return ImageData(
                    data=data,
                    mime_type=mime,
                    source_url=url if _is_public_http_url(url) else None,
                )
    except Exception as exc:
        logger.warning(f"{LOG} 下载参考图失败: {safe_log_text(exc)}")
        return None


def _candidate_from_obj(obj: Any) -> list[str]:
    refs: list[str] = []
    for attr in ("url", "file", "path", "src", "image_url", "file_path"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value.strip():
            refs.append(value.strip())
        if isinstance(value, dict):
            for key in ("url", "file", "path"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    refs.append(nested.strip())
    # dict-like component
    if isinstance(obj, dict):
        for key in ("url", "file", "path", "src"):
            nested = obj.get(key)
            if isinstance(nested, str) and nested.strip():
                refs.append(nested.strip())
            if isinstance(nested, dict):
                for sub in ("url", "file", "path"):
                    val = nested.get(sub)
                    if isinstance(val, str) and val.strip():
                        refs.append(val.strip())
    return refs


def _is_public_http_url(url: str) -> bool:
    """True for http(s) URLs a remote video API can plausibly fetch itself.

    Loopback / LAN addresses (NapCat local file servers etc.) are excluded:
    the upstream would not be able to reach them, and base64 data URLs are
    rejected by many video backends (Seedance/Volcengine among them).
    """
    if not url.startswith(("http://", "https://")):
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".local"):
        return False
    if host.startswith(("127.", "10.", "192.168.")):
        return False
    if host.startswith("172."):
        second = host.split(".")[1] if host.count(".") >= 3 else ""
        if second.isdigit() and 16 <= int(second) <= 31:
            return False
    return True


async def _image_from_component(component: Any, *, max_bytes: int) -> ImageData | None:
    candidates = _candidate_from_obj(component)

    # Prefer publicly fetchable http(s) URLs: the upstream video API fetches
    # the image itself and typically rejects base64/data URLs. convert_to_
    # file_path yields a LOCAL path which would force a data URL.
    for candidate in candidates:
        if _is_public_http_url(candidate):
            image = await download_image(candidate, max_bytes=max_bytes)
            if image:
                return image

    convert = getattr(component, "convert_to_file_path", None)
    if callable(convert):
        try:
            path = await convert()
            if isinstance(path, str) and path.strip():
                image = await download_image(path.strip(), max_bytes=max_bytes)
                if image:
                    return image
        except Exception as exc:
            logger.debug(f"{LOG} convert_to_file_path 失败: {safe_log_text(exc)}")

    for candidate in candidates:
        image = await download_image(candidate, max_bytes=max_bytes)
        if image:
            return image
    return None


async def _images_from_reply_component(component: Any, *, max_bytes: int) -> list[ImageData]:
    images: list[ImageData] = []
    for attr in ("chain", "message", "origin", "content"):
        payload = getattr(component, attr, None)
        if not isinstance(payload, list):
            continue
        for sub in payload:
            is_image = Comp is not None and isinstance(sub, Comp.Image)
            if not is_image:
                # duck type
                if not any(hasattr(sub, key) for key in ("url", "file", "path", "convert_to_file_path")):
                    continue
            image = await _image_from_component(sub, max_bytes=max_bytes)
            if image:
                images.append(image)
        if images:
            break
    return images


async def _images_from_quoted_parser(event: Any, *, max_bytes: int) -> list[ImageData]:
    try:
        from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images
    except Exception:
        return []
    try:
        refs = await extract_quoted_message_images(event)
    except Exception as exc:
        logger.warning(f"{LOG} 引用图解析失败: {safe_log_text(exc)}")
        return []
    images: list[ImageData] = []
    for ref in refs or []:
        if not isinstance(ref, str) or not ref.strip():
            continue
        image = await download_image(ref.strip(), max_bytes=max_bytes)
        if image:
            images.append(image)
    return images


async def collect_event_images(
    event: Any,
    *,
    max_images: int,
    max_size_mb: int,
) -> list[ImageData]:
    """Collect direct and quoted/reply images for image-to-video."""
    max_bytes = max(1, max_size_mb) * 1024 * 1024
    images: list[ImageData] = []
    seen: set[str] = set()

    def _add(image: ImageData | None) -> None:
        nonlocal images
        if image is None or len(images) >= max_images:
            return
        digest = f"{len(image.data)}:{image.mime_type}:{image.data[:32]!r}"
        if digest in seen:
            return
        seen.add(digest)
        images.append(image)

    message_list = []
    try:
        if getattr(event, "message_obj", None) and getattr(event.message_obj, "message", None):
            message_list = list(event.message_obj.message or [])
        elif hasattr(event, "get_messages"):
            message_list = list(event.get_messages() or [])
    except Exception:
        message_list = []

    has_reply = False
    reply_found = False
    direct_found = False

    for component in message_list:
        try:
            is_image = Comp is not None and isinstance(component, Comp.Image)
            is_reply = Comp is not None and isinstance(component, Comp.Reply)
            if not is_image and not is_reply:
                # duck typing fallback
                class_name = type(component).__name__.lower()
                if "image" in class_name:
                    is_image = True
                elif "reply" in class_name or "quote" in class_name:
                    is_reply = True

            if is_image:
                image = await _image_from_component(component, max_bytes=max_bytes)
                if image:
                    direct_found = True
                    _add(image)
            elif is_reply:
                has_reply = True
                reply_images = await _images_from_reply_component(component, max_bytes=max_bytes)
                if reply_images:
                    reply_found = True
                    for image in reply_images:
                        _add(image)
        except Exception as exc:
            logger.warning(f"{LOG} 提取消息图片失败: {safe_log_text(exc)}")

    # QQ/aiocqhttp often needs quoted parser fallback.
    if len(images) < max_images and (has_reply and not reply_found and not direct_found or not images):
        for image in await _images_from_quoted_parser(event, max_bytes=max_bytes):
            _add(image)

    # platform helper fallback
    if len(images) < max_images and hasattr(event, "get_image_urls"):
        try:
            for ref in event.get_image_urls() or []:
                if isinstance(ref, str) and ref.strip():
                    _add(await download_image(ref.strip(), max_bytes=max_bytes))
        except Exception:
            pass

    logger.info(
        f"{LOG} 参考图收集完成: count={len(images)} direct={direct_found} reply={reply_found}"
    )
    return images[:max_images]
