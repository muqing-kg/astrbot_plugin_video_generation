"""OpenAI-style video gateway adapter (grok2api / new-api / one-api relays)."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from astrbot.api import logger

from ..generation.reference import image_to_data_url
from ..shared.constants import POLL_INTERVAL_SECONDS, UNSPECIFIED_TOKENS
from ..shared.logging import log_prefix, safe_log_text
from ..shared.types import AdapterConfig, ImageData, VideoRequest, VideoResult
from .payload_adapt import adapt_payload, aspect_to_size

LOG = log_prefix("Adapter")
API_STATUS_RE = re.compile(r"API 错误\s*\((\d{3})\)")


class VideoAPIAdapter:
    """Talk to OpenAI-style video endpoints across gateways.

    Covers grok2api and Go-gateway relays (new-api etc.) for Grok, Seedance,
    Kling, Sora and similar models. Field-shape disagreements (image object vs
    string, duration vs seconds, aspect_ratio vs ratio) are resolved by
    re-sending a mutated payload when the upstream 4xx body names the field.
    """

    def __init__(self, config: AdapterConfig):
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        # Fields the upstream explicitly rejected (unknown/unsupported value);
        # remembered so later payloads omit them without another roundtrip.
        self._unsupported_fields: set[str] = set()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _root_base(self) -> str:
        raw = (self.config.base_url or "").strip()
        if not raw:
            raise ValueError("未配置 API 地址")
        raw = raw.rstrip("/")
        lower = raw.lower()
        # If user pasted a full create path, strip back to service root-ish base.
        for suffix in (
            "/v1/videos/generations",
            "/v1/video/generations",
            "/v1/videos",
            "/videos/generations",
            "/video/generations",
        ):
            if lower.endswith(suffix):
                return raw[: -len(suffix)].rstrip("/") or raw
        if lower.endswith("/v1"):
            return raw[: -len("/v1")].rstrip("/") or raw
        return raw

    def _candidate_create_urls(self) -> list[str]:
        """Prefer grok2api path, then common gateway aliases."""
        raw = (self.config.base_url or "").strip().rstrip("/")
        lower = raw.lower()
        if lower.endswith("/videos/generations") or lower.endswith("/video/generations") or lower.endswith("/videos"):
            return [raw]

        root = self._root_base()
        candidates = [
            f"{root}/v1/videos/generations",  # grok2api
            f"{root}/v1/videos",              # OpenAI videos style / some gateways
            f"{root}/v1/video/generations",   # new-api style
        ]
        # de-dup keep order
        seen: set[str] = set()
        ordered: list[str] = []
        for item in candidates:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def _status_urls(self, create_url: str, request_id: str) -> list[str]:
        rid = request_id.strip()
        parsed = urlparse(create_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        root = origin
        for suffix in (
            "/v1/videos/generations",
            "/v1/video/generations",
            "/v1/videos",
            "/videos/generations",
            "/video/generations",
        ):
            if path.endswith(suffix):
                root = origin + path[: -len(suffix)]
                break
        urls = [
            f"{root}/v1/videos/{rid}",
            f"{root}/v1/video/generations/{rid}",
        ]
        if create_url.endswith("/videos/generations"):
            urls.insert(0, create_url[: -len("/generations")] + f"/{rid}")
        elif create_url.endswith("/videos"):
            urls.insert(0, f"{create_url}/{rid}")
        elif create_url.endswith("/video/generations"):
            urls.insert(0, f"{create_url}/{rid}")
        # de-dup
        seen: set[str] = set()
        ordered: list[str] = []
        for item in urls:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def _content_urls(self, status_url: str, request_id: str) -> list[str]:
        rid = request_id.strip()
        parsed = urlparse(status_url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        root = self._root_base()
        urls: list[str] = []
        # Prefer grok2api official content endpoint first.
        if root:
            urls.append(f"{root.rstrip('/')}/v1/videos/{rid}/content")
        if origin:
            urls.append(f"{origin}/v1/videos/{rid}/content")
        if status_url:
            if status_url.startswith("http://") or status_url.startswith("https://"):
                urls.append(status_url.rstrip("/") + "/content")
            elif origin:
                urls.append(origin.rstrip("/") + "/" + status_url.lstrip("/"))
                if not status_url.rstrip("/").endswith("/content"):
                    urls.append(origin.rstrip("/") + "/" + status_url.lstrip("/").rstrip("/") + "/content")
        # de-dup
        seen: set[str] = set()
        ordered: list[str] = []
        for item in urls:
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def _headers(self, *, json_body: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _is_retryable(self, error: str) -> bool:
        match = API_STATUS_RE.search(error or "")
        if match:
            code = int(match.group(1))
            return code not in set(self.config.non_retryable_status_codes)
        lowered = (error or "").lower()
        return not any(k.lower() in lowered for k in self.config.non_retryable_error_keywords)

    @staticmethod
    def _image_ref(image: ImageData) -> str:
        """Remote http(s) URL as-is; anything else becomes a data URL."""
        if image.source_url and image.source_url.startswith(("http://", "https://")):
            return image.source_url
        return image_to_data_url(image)

    def _build_multipart_payload(
        self, payload: dict[str, Any], image: ImageData
    ) -> aiohttp.FormData:
        """Build the Sora-style multipart create form.

        Fields are sourced from the (possibly adapted) payload dict so that
        later adapt rules (drop/convert) also apply to the multipart retry.
        The image travels as the `input_reference` file part; aiohttp sets the
        multipart Content-Type with boundary, so no manual Content-Type.
        """
        form = aiohttp.FormData()
        form.add_field("model", str(payload.get("model") or self.config.model))
        form.add_field("prompt", str(payload.get("prompt") or ""))
        seconds_value = payload.get("duration", payload.get("seconds"))
        if seconds_value:
            try:
                form.add_field("seconds", str(int(seconds_value)))
            except (TypeError, ValueError):
                form.add_field("seconds", str(seconds_value))
        size = aspect_to_size(
            str(payload.get("aspect_ratio") or ""), str(payload.get("resolution") or "")
        )
        if size:
            form.add_field("size", size)
        mime = (image.mime_type or "").split(";")[0].strip().lower()
        ext = ".png"
        if mime in ("image/jpeg", "image/jpg"):
            ext = ".jpg"
        elif mime == "image/gif":
            ext = ".gif"
        elif mime == "image/webp":
            ext = ".webp"
        form.add_field(
            "input_reference",
            image.data,
            filename=f"reference{ext}",
            content_type=mime or "image/png",
        )
        return form

    def _build_payload(self, request: VideoRequest) -> dict[str, Any]:
        """Build a video create body for OpenAI-style video gateways.

        grok2api rejects unknown fields (DisallowUnknownFields), so extra
        convenience fields are omitted once the upstream has rejected them.
        `mode` declares the input style for unified-media gateways; strict gateways drop it via the adapt rules on first contact.
        Unspecified params ("不指定"/0/empty) are omitted so the model applies
        its own defaults. `image` is sent as an object first (grok2api form);
        gateways that require other shapes are corrected by the adapt rules
        from upstream 400 feedback.
        """
        payload: dict[str, Any] = {
            "model": request.model or self.config.model,
            "mode": "image-to-video" if request.images else "text-to-video",
            "prompt": request.prompt,
        }
        if (
            request.duration
            and request.duration > 0
            and "duration" not in self._unsupported_fields
        ):
            payload["duration"] = int(request.duration)
        if (
            (request.aspect_ratio or "").lower() not in UNSPECIFIED_TOKENS
            and "aspect_ratio" not in self._unsupported_fields
        ):
            payload["aspect_ratio"] = request.aspect_ratio
        if (
            (request.resolution or "").lower() not in UNSPECIFIED_TOKENS
            and "resolution" not in self._unsupported_fields
        ):
            payload["resolution"] = request.resolution
        if request.images and "image" not in self._unsupported_fields:
            payload["image"] = {"url": self._image_ref(request.images[0])}
            if len(request.images) > 1 and "images" not in self._unsupported_fields:
                payload["image_urls"] = [
                    self._image_ref(image) for image in request.images
                ]
        return payload

    def _build_media_payload(
        self, request: VideoRequest, urls: list[str]
    ) -> dict[str, Any]:
        """Unified-media create body: uploaded URLs in
        an `images` array plus an explicit `mode`."""
        payload: dict[str, Any] = {
            "model": request.model or self.config.model,
            "mode": "image-to-video",
            "prompt": request.prompt,
            "images": urls,
        }
        if request.duration and request.duration > 0:
            payload["duration"] = int(request.duration)
        if (request.aspect_ratio or "").lower() not in UNSPECIFIED_TOKENS:
            payload["aspect_ratio"] = request.aspect_ratio
        if (request.resolution or "").lower() not in UNSPECIFIED_TOKENS:
            payload["resolution"] = request.resolution
        return payload

    async def _upload_reference_media(
        self, images: list[ImageData]
    ) -> list[str] | None:
        """Upload reference images to the gateway's media upload endpoint
        (POST /v1/videos/uploads) and return the protected URLs.

        Returns None when the endpoint is unavailable (e.g. real OpenAI), so
        the caller can fall back to the multipart form.
        """
        session = self._session_get()
        prefix = log_prefix("Adapter")
        upload_url = f"{self._root_base().rstrip('/')}/v1/videos/uploads"
        timeout = aiohttp.ClientTimeout(total=min(300, max(60, self.config.timeout)))
        urls: list[str] = []
        for index, image in enumerate(images):
            mime = (image.mime_type or "").split(";")[0].strip().lower() or "image/png"
            ext = ".png"
            if mime in ("image/jpeg", "image/jpg"):
                ext = ".jpg"
            elif mime == "image/gif":
                ext = ".gif"
            elif mime == "image/webp":
                ext = ".webp"
            form = aiohttp.FormData()
            form.add_field(
                "file",
                image.data,
                filename=f"reference{index}{ext}",
                content_type=mime,
            )
            try:
                async with session.post(
                    upload_url,
                    data=form,
                    headers=self._headers(json_body=False),
                    proxy=self.config.proxy,
                    timeout=timeout,
                ) as resp:
                    text = await resp.text()
                    if resp.status in {404, 405}:
                        logger.info(f"{prefix} 网关无媒体上传接口 ({resp.status})")
                        return None
                    if resp.status >= 400:
                        logger.warning(
                            f"{prefix} 参考媒体上传失败 status={resp.status} "
                            f"body={safe_log_text(text, 160)}"
                        )
                        return None
                    data = await self._safe_json(resp, text)
                    url = self._extract_video_url(data)
                    if not url and text.strip().startswith(("http://", "https://")):
                        url = text.strip()
                    if not url:
                        logger.warning(
                            f"{prefix} 上传响应缺少 URL: {safe_log_text(text, 160)}"
                        )
                        return None
                    urls.append(url)
            except Exception as exc:
                logger.warning(f"{prefix} 参考媒体上传异常: {safe_log_text(exc)}")
                return None
        return urls

    async def generate(self, request: VideoRequest, *, should_cancel=None) -> VideoResult:
        if not self.config.api_key:
            return VideoResult(error="未配置 API Key")
        last_error = "生成失败"
        attempts = max(1, self.config.max_retry_attempts)
        for attempt in range(attempts):
            if should_cancel and should_cancel():
                return VideoResult(error="任务已取消")
            result = await self._generate_once(request, should_cancel=should_cancel)
            if result.error is None:
                return result
            last_error = result.error
            if not self._is_retryable(last_error):
                return result
            if attempt < attempts - 1:
                await asyncio.sleep(min(2 ** attempt, 8))
        return VideoResult(error=f"重试失败: {last_error}")

    async def _generate_once(self, request: VideoRequest, *, should_cancel=None) -> VideoResult:
        payload = self._build_payload(request)
        adapt_budget = 4
        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)
        prefix = log_prefix("Adapter", request.task_id)
        start = time.time()

        # Diagnostic: how the reference image travels. Upstream video APIs
        # usually require a publicly fetchable URL and reject base64.
        if request.images:
            first = request.images[0]
            if first.source_url and first.source_url.startswith(("http://", "https://")):
                image_desc = f"url={safe_log_text(first.source_url, 100)}"
            else:
                image_desc = f"base64(data_len={len(first.data)})"
            logger.info(
                f"{prefix} 参考图发送形态: {image_desc} images={len(request.images)}"
            )

        create_urls = self._candidate_create_urls()
        last_create_error = "创建视频任务失败"
        create_data: dict[str, Any] | None = None
        used_create_url = ""
        use_media_variant = False
        use_multipart = False

        try:
            for create_url in create_urls:
                if should_cancel and should_cancel():
                    return VideoResult(error="任务已取消")
                if self.config.debug_request_logging:
                    logger.debug(
                        f"{prefix} 尝试创建视频 url={create_url} model={payload.get('model')} "
                        f"duration={payload.get('duration')} images={len(request.images)} "
                        f"media={use_media_variant} multipart={use_multipart}"
                    )
                if use_multipart and request.images:
                    post_kwargs: dict[str, Any] = dict(
                        data=self._build_multipart_payload(payload, request.images[0]),
                        headers=self._headers(json_body=False),
                    )
                else:
                    post_kwargs = dict(json=payload, headers=self._headers())
                async with session.post(
                    create_url,
                    proxy=self.config.proxy,
                    timeout=timeout,
                    **post_kwargs,
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        last_create_error = self._format_error(resp.status, text)
                        # Path not found on this gateway: try next candidate.
                        if resp.status in {404, 405}:
                            logger.warning(
                                f"{prefix} 创建路径不可用 ({resp.status}): {create_url}"
                            )
                            continue
                        # Unified-media gateways reject base64 /
                        # JSON-shaped image references: upload the media to
                        # /v1/videos/uploads and retry with an `images` array.
                        body_lower = text.lower()
                        if (
                            resp.status in {400, 422}
                            and not use_media_variant
                            and request.images
                            and (
                                "input_reference" in body_lower
                                or "multipart" in body_lower
                                or "unsupported_reference_format" in body_lower
                                or ("base64" in body_lower and "not allowed" in body_lower)
                            )
                        ):
                            use_media_variant = True
                            uploaded_urls = await self._upload_reference_media(
                                request.images
                            )
                            if uploaded_urls:
                                payload = self._build_media_payload(
                                    request, uploaded_urls
                                )
                                logger.warning(
                                    f"{prefix} 参考媒体已上传，切换 images 数组格式重试: {create_url}"
                                )
                                continue
                            # Upload endpoint unavailable (e.g. real OpenAI):
                            # fall back to the Sora multipart form.
                            use_multipart = True
                            logger.warning(
                                f"{prefix} 媒体上传接口不可用，切换 multipart 表单重试: {create_url}"
                            )
                            continue
                        # Field-shape feedback (e.g. image object vs string):
                        # mutate the payload and retry the same create URL.
                        if resp.status in {400, 422} and adapt_budget > 0:
                            adapted = adapt_payload(payload, text)
                            if adapted is not None:
                                adapt_budget -= 1
                                # Remember removed fields so later payloads
                                # (incl. fresh outer retries) omit them.
                                self._unsupported_fields.update(
                                    set(payload) - set(adapted)
                                )
                                logger.warning(
                                    f"{prefix} 按上游校验反馈调整请求字段后重试: {create_url}"
                                )
                                payload = adapted
                                continue
                        return VideoResult(error=last_create_error)
                    create_data = await self._safe_json(resp, text)
                    used_create_url = create_url
                    break
            else:
                hint = (
                    "当前地址没有可用的视频创建接口。"
                    "请确认 API 地址指向 grok2api（支持 /v1/videos/generations），"
                    "而不是只支持 chat 的中转。"
                )
                return VideoResult(error=f"{last_create_error}；{hint}")

            request_id = self._extract_request_id(create_data)
            if not request_id:
                # Some gateways may return final result directly.
                direct_url = self._extract_video_url(create_data)
                if direct_url:
                    video_bytes, content_type = await self._download_video(direct_url)
                    return VideoResult(
                        video_bytes=video_bytes,
                        content_type=content_type or "video/mp4",
                        video_url=direct_url,
                        upstream_request_id="",
                    )
                return VideoResult(error="上游未返回 request_id")

            status_urls = self._status_urls(used_create_url, request_id)
            deadline = start + max(1, self.config.timeout)
            active_status_url = status_urls[0]
            while True:
                if should_cancel and should_cancel():
                    return VideoResult(error="任务已取消", upstream_request_id=request_id)
                if time.time() >= deadline:
                    return VideoResult(error="等待视频生成超时", upstream_request_id=request_id)

                data = None
                last_status_error = ""
                for status_url in status_urls:
                    async with session.get(
                        status_url,
                        headers=self._headers(),
                        proxy=self.config.proxy,
                        timeout=timeout,
                    ) as resp:
                        text = await resp.text()
                        if resp.status >= 400:
                            last_status_error = self._format_error(resp.status, text)
                            if resp.status in {404, 405}:
                                continue
                            return VideoResult(
                                error=last_status_error,
                                upstream_request_id=request_id,
                            )
                        data = await self._safe_json(resp, text)
                        active_status_url = status_url
                        break
                if data is None:
                    # all status urls failed with 404; keep polling a bit in case job not visible yet
                    if last_status_error:
                        logger.debug(f"{prefix} 状态查询暂不可用: {last_status_error}")
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                data_obj = data or {}
                status = str(
                    data_obj.get("status")
                    or data_obj.get("task_status")
                    or data_obj.get("state")
                    or ""
                ).lower()
                progress = data_obj.get("progress")
                try:
                    progress_num = int(progress) if progress is not None else None
                except (TypeError, ValueError):
                    progress_num = None
                has_video = bool(self._extract_video_url(data))
                finished = status in {"done", "succeeded", "completed", "success", "ready"} or (
                    progress_num == 100 and has_video
                )
                if finished:
                    video_url = self._extract_video_url(data)
                    video_bytes = None
                    content_type = None
                    # Normalize relative content paths to absolute grok2api URLs.
                    if video_url and video_url.startswith("/"):
                        root = self._root_base().rstrip("/")
                        video_url = f"{root}{video_url}"
                    download_candidates: list[str] = []
                    if video_url:
                        download_candidates.append(video_url)
                    download_candidates.extend(self._content_urls(active_status_url, request_id))
                    seen_dl: set[str] = set()
                    for candidate in download_candidates:
                        candidate = (candidate or "").strip()
                        if not candidate or candidate in seen_dl:
                            continue
                        seen_dl.add(candidate)
                        video_bytes, content_type = await self._download_video(candidate)
                        if video_bytes is not None:
                            video_url = candidate
                            logger.info(
                                f"{prefix} 视频内容下载成功: bytes={len(video_bytes)} url={safe_log_text(candidate, 120)}"
                            )
                            break
                        logger.warning(
                            f"{prefix} 视频内容下载失败，尝试下一个: {safe_log_text(candidate, 120)}"
                        )
                    if video_bytes is None and not video_url:
                        return VideoResult(
                            error="视频已完成但未获取到内容",
                            upstream_request_id=request_id,
                        )
                    if video_bytes is None:
                        logger.warning(
                            f"{prefix} 仅拿到视频URL，未能下载二进制，将降级发链接: {safe_log_text(video_url, 160)}"
                        )
                    return VideoResult(
                        video_bytes=video_bytes,
                        content_type=content_type or "video/mp4",
                        video_url=video_url,
                        upstream_request_id=request_id,
                    )
                if status in {"failed", "error", "cancelled", "canceled"}:
                    message = str(
                        (data or {}).get("error")
                        or (data or {}).get("message")
                        or "上游视频生成失败"
                    )
                    if isinstance((data or {}).get("error"), dict):
                        message = str(
                            data["error"].get("message")
                            or data["error"].get("code")
                            or message
                        )
                    return VideoResult(error=message, upstream_request_id=request_id)
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except Exception as exc:
            logger.error(f"{prefix} 视频请求异常: {safe_log_text(exc)}", exc_info=True)
            msg = str(exc)
            if "Cannot connect to host" in msg or "Connect call failed" in msg:
                return VideoResult(
                    error=(
                        f"无法连接 grok2api（{safe_log_text(self.config.base_url or '', 80)}）。"
                        "请确认 grok2api 容器/服务已启动，且 AstrBot 能访问该地址。"
                        f" 原始错误: {safe_log_text(msg, 180)}"
                    )
                )
            return VideoResult(error=f"请求异常: {exc}")

    def _extract_request_id(self, data: dict[str, Any] | None) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ("request_id", "id", "task_id", "video_id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # nested
        for nest_key in ("data", "result", "video"):
            nested = data.get(nest_key)
            if isinstance(nested, dict):
                found = self._extract_request_id(nested)
                if found:
                    return found
        return ""

    async def _download_video(self, url: str) -> tuple[bytes | None, str | None]:
        session = self._session_get()
        timeout = aiohttp.ClientTimeout(total=min(300, max(30, self.config.timeout)))
        try:
            # Relative path cannot be downloaded directly.
            if url.startswith("/"):
                root = self._root_base().rstrip("/")
                url = f"{root}{url}"
            async with session.get(
                url,
                headers=self._headers(),
                proxy=self.config.proxy,
                timeout=timeout,
            ) as resp:
                content_type = (resp.headers.get("Content-Type") or "video/mp4").split(";")[0].strip().lower()
                data = await resp.read()
                if resp.status >= 400:
                    logger.warning(
                        f"{LOG} 下载视频HTTP失败 status={resp.status} type={content_type} url={safe_log_text(url, 120)}"
                    )
                    return None, None
                if not data:
                    return None, content_type
                # Avoid treating JSON error body as video.
                if content_type.startswith("application/json") or data[:1] in (b"{", b"["):
                    logger.warning(
                        f"{LOG} 下载结果不是视频二进制 type={content_type} url={safe_log_text(url, 120)}"
                    )
                    return None, content_type
                if content_type in {"application/octet-stream", "binary/octet-stream", ""}:
                    content_type = "video/mp4"
                return data, content_type
        except Exception as exc:
            logger.warning(f"{LOG} 下载视频失败: {safe_log_text(exc)}")
            return None, None

    def _extract_video_url(self, data: dict[str, Any] | None) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("url", "video_url", "content_url"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for nest_key in ("video", "videos", "result", "data", "output"):
            nested = data.get(nest_key)
            if isinstance(nested, dict):
                found = self._extract_video_url(nested)
                if found:
                    return found
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
                    found = self._extract_video_url(item if isinstance(item, dict) else None)
                    if found:
                        return found
        return None

    async def _safe_json(self, resp: aiohttp.ClientResponse, text: str) -> dict[str, Any] | None:
        try:
            data = await resp.json(content_type=None)
            return data if isinstance(data, dict) else None
        except Exception:
            if self.config.debug_request_logging:
                logger.debug(f"{LOG} 非 JSON 响应: {safe_log_text(text, 300)}")
            return None

    def _format_error(self, status: int, body: str) -> str:
        message = f"API 错误 ({status})"
        if self.config.show_user_error_details and body:
            detail = re.sub(r"\s+", " ", body).strip()
            if len(detail) > 300:
                detail = detail[:297] + "..."
            if detail:
                return f"{message}: {detail}"
        return message


# Backward-compatible alias for existing imports.
Grok2APIVideoAdapter = VideoAPIAdapter