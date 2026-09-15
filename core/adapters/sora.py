"""Sora 线路适配器：POST /v1/videos。

文生为 JSON（seconds 字符串、size 由比例换算）；图生为 multipart 表单，
参考图以内嵌文件 `input_reference` 提交。
"""

from __future__ import annotations

from typing import Any

import aiohttp

from ..shared.media import aspect_to_size
from ..shared.types import VideoCapability, VideoRequest
from .base import CreateError, VideoAdapterBase


class SoraAdapter(VideoAdapterBase):
    async def create(self, request: VideoRequest) -> dict:
        caps = self.config.capabilities
        root = self._root_base().rstrip("/")
        url = f"{root}/v1/videos"
        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=max(30, self.config.timeout))

        seconds = (
            str(int(request.duration))
            if request.duration and request.duration > 0
            else ""
        )
        size = aspect_to_size(request.aspect_ratio, request.resolution)

        if request.images:
            if not (caps & VideoCapability.IMAGE_TO_VIDEO):
                raise CreateError("该线路未启用图生视频能力，请在供应商配置中勾选")
            image = request.images[0]
            mime = (image.mime_type or "").split(";")[0].strip().lower() or "image/png"
            ext = ".png"
            if mime in ("image/jpeg", "image/jpg"):
                ext = ".jpg"
            elif mime == "image/gif":
                ext = ".gif"
            elif mime == "image/webp":
                ext = ".webp"
            form = aiohttp.FormData()
            form.add_field("model", request.model)
            form.add_field("prompt", request.prompt)
            if seconds:
                form.add_field("seconds", seconds)
            if size:
                form.add_field("size", size)
            form.add_field(
                "input_reference",
                image.data,
                filename=f"reference{ext}",
                content_type=mime,
            )
            async with session.post(
                url,
                data=form,
                headers=self._headers(json_body=False),
                proxy=self.config.proxy,
                timeout=timeout,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise CreateError(text, status=resp.status)
                data = await self._safe_json(resp, text)
                return data or {}

        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": request.prompt,
        }
        if seconds:
            payload["seconds"] = seconds
        if size:
            payload["size"] = size
        async with session.post(
            url,
            json=payload,
            headers=self._headers(),
            proxy=self.config.proxy,
            timeout=timeout,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise CreateError(text, status=resp.status)
            data = await self._safe_json(resp, text)
            return data or {}
