"""AstrBot video generation plugin entrypoint."""

from __future__ import annotations

import asyncio
import base64
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .core.adapters.video_api import VideoAPIAdapter
from .core.config.manager import ConfigManager
from .core.formatting.result import (
    format_result_info,
    format_start_task_message,
    format_task_detail,
    format_task_list,
)
from .core.generation.parser import parse_video_command
from .core.generation.presets import combine_prompt, match_presets, parse_presets
from .core.generation.prompt import build_enhanced_video_prompt
from .core.generation.reference import collect_event_images, detect_image_aspect_ratio
from .core.shared.logging import log_prefix, safe_log_text
from .core.shared.types import VideoRequest
from .core.tasks.ids import new_task_id
from .core.tasks.manager import TaskManager
from .core.tasks.models import VideoTaskRecord, VideoTaskStatus

LOG = log_prefix("Plugin")


@register(
    "astrbot_plugin_video_generation",
    "muqing-kg",
    "通用视频生成插件",
    "v0.4.6",
)
class VideoGenerationPlugin(Star):
    _QQ_BASE64_VIDEO_MAX_BYTES = 50 * 1024 * 1024

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.raw_config = config
        self.data_dir = Path(StarTools.get_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = Path(get_astrbot_temp_path()) / "astrbot_plugin_video_generation"
        self._ensure_temp_dir()

        self.config_manager = ConfigManager(config)
        self.task_manager = TaskManager(
            max_running_tasks=self.config_manager.generation.max_running_tasks,
            max_queued_tasks=self.config_manager.generation.max_queued_tasks,
            enable_history=False,
            persistence_file=None,
        )
        self.adapter = VideoAPIAdapter(self.config_manager.adapter)
        self._rate_limit_until: dict[str, float] = {}
        self._daily_usage: dict[str, dict[str, int]] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    async def initialize(self):
        self._ensure_temp_dir()
        self.task_manager.start_workers()
        logger.info(
            f"{LOG} 插件已加载 model={safe_log_text(self.config_manager.generation.model)} "
            f"timeout={self.config_manager.generation.timeout_seconds}s"
        )

    async def terminate(self):
        try:
            await self.task_manager.stop()
            await self.adapter.close()
            for task in list(self._bg_tasks):
                task.cancel()
            logger.info(f"{LOG} 插件已卸载")
        except Exception as exc:
            logger.error(f"{LOG} 卸载清理失败: {safe_log_text(exc)}", exc_info=True)

    def _ensure_temp_dir(self) -> Path:
        """Ensure the plugin temp dir exists.

        Only create when missing. AstrBot/Docker cleaners may remove it after
        plugin load, so check again before writing video bytes.
        """
        self.temp_dir = Path(get_astrbot_temp_path()) / "astrbot_plugin_video_generation"
        if not self.temp_dir.exists():
            self.temp_dir.mkdir(parents=True, exist_ok=True)
        return self.temp_dir

    def _reload_runtime(self) -> None:
        self.config_manager.reload(self.raw_config)
        self.adapter = VideoAPIAdapter(self.config_manager.adapter)
        self.task_manager.configure(
            max_running_tasks=self.config_manager.generation.max_running_tasks,
            max_queued_tasks=self.config_manager.generation.max_queued_tasks,
            enable_history=False,
        )
        self.task_manager.start_workers()

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _usage_day_key(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _check_limits(self, event: AstrMessageEvent) -> str | None:
        usage = self.config_manager.usage
        umo = event.unified_msg_origin
        is_admin = self._is_admin(event)
        bypass = is_admin and usage.admin_bypass_limits
        in_whitelist = umo in set(usage.umo_whitelist)

        if umo in set(usage.umo_blacklist) and not bypass and not in_whitelist:
            return usage.blacklist_block_message
        if bypass or in_whitelist:
            return None

        now = time.time()
        if usage.rate_limit_seconds > 0:
            until = self._rate_limit_until.get(umo, 0)
            if until > now:
                wait = int(until - now) + 1
                return f"❌ 请求太频繁，请 {wait} 秒后再试"

        if usage.enable_daily_limit:
            day = self._usage_day_key()
            used = self._daily_usage.get(day, {}).get(umo, 0)
            if used >= usage.daily_limit_count:
                return f"❌ 今日视频额度已用完（{usage.daily_limit_count}）"
        return None

    def _mark_usage(self, event: AstrMessageEvent) -> None:
        usage = self.config_manager.usage
        umo = event.unified_msg_origin
        if usage.rate_limit_seconds > 0:
            self._rate_limit_until[umo] = time.time() + usage.rate_limit_seconds
        if usage.enable_daily_limit:
            day = self._usage_day_key()
            bucket = self._daily_usage.setdefault(day, {})
            bucket[umo] = bucket.get(umo, 0) + 1

    def _resolve_task_ref(self, event: AstrMessageEvent, task_ref: str, active_only: bool = False):
        task_ref = (task_ref or "").strip()
        if not task_ref:
            return None
        records = (
            self.task_manager.list_active(event.unified_msg_origin)
            if active_only
            else self.task_manager.list_for_umo(event.unified_msg_origin)
        )
        if task_ref.isdigit():
            index = int(task_ref)
            if 1 <= index <= len(records):
                return records[index - 1]
        for record in records:
            if record.task_id == task_ref:
                return record
        return self.task_manager.get(task_ref)

    async def _send_text(self, event: AstrMessageEvent, text: str) -> None:
        await self.context.send_message(
            event.unified_msg_origin,
            MessageChain().message(text),
        )

    def _platform_kind(self, event: AstrMessageEvent) -> str:
        """Return 'qq', 'wechat', or 'other' for this event.

        QQ and WeChat may both be attached through the aiocqhttp adapter, and the
        AstrBot platform instance name is user-changeable. Therefore:

        1. Config-pinned bot self_ids (platform.qq_self_ids / wechat_self_ids)
           are authoritative when present -- they survive instance renames.
        2. Fallback heuristics on stable id shapes (wxid_/@chatroom/gh_) and
           adapter name (aiocqhttp/onebot/qq:).
        """
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""

        # Defensive access: older deployments may lack the platform config
        # section/property; fall back to heuristic detection instead of
        # breaking the whole send chain.
        try:
            platform_cfg = getattr(self.config_manager, "platform", None)
            qq_ids = {
                str(x).strip()
                for x in (getattr(platform_cfg, "qq_self_ids", None) or [])
                if str(x).strip()
            }
            wx_ids = {
                str(x).strip()
                for x in (getattr(platform_cfg, "wechat_self_ids", None) or [])
                if str(x).strip()
            }
        except Exception:
            qq_ids = set()
            wx_ids = set()

        if self_id:
            if self_id in qq_ids:
                return "qq"
            if self_id in wx_ids:
                return "wechat"

        candidates: list[str] = []
        for attr in (
            "get_self_id",
            "get_sender_id",
            "get_group_id",
            "get_session_id",
            "get_platform_id",
            "get_platform_name",
        ):
            try:
                value = str(getattr(event, attr)() or "")
                if value:
                    candidates.append(value)
            except Exception:
                pass
        try:
            meta = getattr(event, "platform_meta", None)
            if meta is not None:
                for attr in ("id", "name"):
                    value = str(getattr(meta, attr, "") or "")
                    if value:
                        candidates.append(value)
        except Exception:
            pass
        try:
            value = str(getattr(event, "unified_msg_origin", "") or "")
            if value:
                candidates.append(value)
        except Exception:
            pass
        try:
            candidates.append(type(event).__module__ or "")
        except Exception:
            pass

        joined = " ".join(candidates).lower()
        wechat_markers = ("@chatroom", "wxid", "gh_", "wechat", "weixin", "微信")
        if any(marker in joined for marker in wechat_markers):
            return "wechat"
        if "aiocqhttp" in joined or "onebot" in joined or joined.startswith("qq:"):
            return "qq"
        return "other"

    def _is_qq_platform(self, event: AstrMessageEvent) -> bool:
        return self._platform_kind(event) == "qq"

    def _build_generic_video_candidates(self, comp: Any, path_obj: Path) -> list[Any]:
        """Keep the original non-QQ candidate chain intact."""
        candidates: list[Any] = []
        for cls_name in ("Video", "File", "Record"):
            cls = getattr(comp, cls_name, None)
            if cls is None:
                continue
            for kwargs in (
                {"file": str(path_obj)},
                {"path": str(path_obj)},
                {"url": str(path_obj)},
            ):
                try:
                    candidates.append(cls(**kwargs))
                    break
                except Exception:
                    continue
            for builder_name in ("fromFileSystem", "from_file", "from_path"):
                builder = getattr(cls, builder_name, None)
                if not callable(builder):
                    continue
                try:
                    candidates.append(builder(str(path_obj)))
                    break
                except TypeError:
                    for kw in ({"file": str(path_obj)}, {"path": str(path_obj)}):
                        try:
                            candidates.append(builder(**kw))
                            break
                        except Exception:
                            continue
                except Exception:
                    continue
        return candidates

    def _build_qq_video_candidates(
        self, comp: Any, path_obj: Path, result_url: str, allow_base64: bool = True
    ) -> list[Any]:
        """Build QQ playable-video candidates only. File is appended by the sender.

        Notes on AstrBot v4.27.2 + aiocqhttp + NapCat:
        - Image/Record are auto-converted to base64 by the platform adapter.
        - Video is NOT; Video.to_dict() either keeps self.file as-is, or registers it
          through callback_api_base. base64:// + callback_api_base raises FileNotFoundError.
        - Comp.Video => OneBot type=video (playable bubble)
        - Comp.File  => OneBot type=file (download card)
        - NapCat uriToLocalFile() accepts base64://, file:///, absolute paths, http(s);
          base64:// is decoded into NapCat's own temp dir, so it works without
          callback_api_base and without a NapCat-visible filesystem.
        """
        candidates: list[Any] = []
        abs_path = str(path_obj.resolve(strict=False))
        video_cls = getattr(comp, "Video", None)
        if video_cls is None:
            return candidates

        def try_append(builder) -> None:
            try:
                candidates.append(builder())
            except Exception as exc:
                logger.warning(
                    f"{LOG} 构造 Video 候选失败: {safe_log_text(exc, 160)}"
                )

        # base64:// inline payload: best when AstrBot and NapCat do not share FS.
        # Must run after _prepare_qq_playable_mp4 (C2PA uuid stripped); raw Grok MP4
        # base64 still fails NT EventChecker. Skipped when callback_api_base is set
        # (Video.to_dict() would treat base64:// as a path and raise FileNotFoundError).
        if allow_base64 and not self._callback_api_base_configured():
            try:
                size = path_obj.stat().st_size
                if 0 < size <= self._QQ_BASE64_VIDEO_MAX_BYTES:
                    bs64 = base64.b64encode(path_obj.read_bytes()).decode("ascii")
                    try_append(lambda: video_cls.fromBase64(bs64))
            except Exception as exc:
                logger.warning(
                    f"{LOG} 构造 base64 Video 候选失败: {safe_log_text(exc, 160)}"
                )

        # Official / most compatible: local file URI via fromFileSystem.
        if callable(getattr(video_cls, "fromFileSystem", None)):
            try_append(lambda: video_cls.fromFileSystem(abs_path))

        # Absolute path form (FileTokenService accepts real paths).
        try_append(lambda: video_cls(file=abs_path, path=abs_path))

        # Public URL only if truly http(s). grok2api content URLs need Bearer and
        # usually cannot be fetched anonymously by NapCat.
        if result_url and result_url.startswith(("http://", "https://")):
            if callable(getattr(video_cls, "fromURL", None)):
                try_append(lambda: video_cls.fromURL(result_url))

        return candidates

    @staticmethod
    def _callback_api_base_configured() -> bool:
        """True when AstrBot global callback_api_base is set, so Video.to_dict()
        registers local files into the callback file service instead of passing
        base64:// through."""
        try:
            from astrbot.core import astrbot_config

            return bool(astrbot_config.get("callback_api_base"))
        except Exception:
            return False



    async def _register_video_callback_url(self, path_obj: Path) -> str:
        """Register local video to AstrBot file callback if available."""
        try:
            import astrbot.api.message_components as Comp

            video_cls = getattr(Comp, "Video", None)
            if video_cls is None or not callable(
                getattr(video_cls, "fromFileSystem", None)
            ):
                return ""
            if not self._callback_api_base_configured():
                return ""
            video = video_cls.fromFileSystem(str(path_obj.resolve(strict=False)))
            if not callable(getattr(video, "register_to_file_service", None)):
                return ""
            url = await video.register_to_file_service()
            return str(url or "")
        except Exception as exc:
            logger.warning(
                f"{LOG} 注册视频回调 URL 失败: {safe_log_text(exc, 160)}"
            )
            return ""

    @staticmethod
    def _iter_mp4_boxes(data: bytes, start: int = 0, end: int | None = None):
        """Yield (offset, size, typ, hdr) for ISO-BMFF boxes in [start, end)."""
        import struct

        if end is None:
            end = len(data)
        off = start
        while off + 8 <= end:
            size = struct.unpack(">I", data[off : off + 4])[0]
            typ = data[off + 4 : off + 8]
            hdr = 8
            if size == 1:
                if off + 16 > end:
                    break
                size = struct.unpack(">Q", data[off + 8 : off + 16])[0]
                hdr = 16
            elif size == 0:
                size = end - off
            if size < hdr or off + size > end:
                break
            yield off, size, typ, hdr
            off += size

    @classmethod
    def _sanitize_mp4_for_qq(cls, src: Path, dst: Path) -> tuple[Path, list[str]]:
        """Write a QQ-safer MP4 without ffmpeg.

        Pipeline goal: local downloaded mp4 -> cleaned local mp4 -> send as Video card.
        Grok/xAI MP4s embed a top-level `uuid` C2PA content credential and a large
        moov/udta. QQ/NT Video elements reject such files even after base64 inline.

        IMPORTANT: we never REMOVE boxes that precede `mdat` -- removing them would
        shift `mdat` and invalidate every `stco`/`co64` chunk offset (unplayable).
        Instead each dropped box is replaced by a SAME-SIZE `free` box, preserving
        all absolute byte offsets. moov children are replaced in place for the same
        reason (moov size must not change). File size is unchanged; only box types
        (and zeroed payloads) differ. Never rewrites src in-place.
        """
        import struct

        data = src.read_bytes()
        if len(data) < 16:
            return src, []

        drop_top = {b"uuid", b"free", b"skip"}
        drop_moov = {b"udta", b"uuid", b"free", b"skip"}

        def same_size_free_box(original: bytes, hdr: int, size: int) -> bytes:
            out = bytearray(original)
            if len(out) < 8:
                out.extend(b"\x00" * (8 - len(out)))
            out[4:8] = b"free"
            for i in range(hdr, len(out)):
                out[i] = 0
            return bytes(out)

        kept: list[bytes] = []
        removed: list[str] = []
        for off, size, typ, hdr in cls._iter_mp4_boxes(data):
            chunk = data[off : off + size]
            typ_s = typ.decode("latin-1", errors="replace")
            if typ in drop_top:
                removed.append(f"{typ_s}:{size}")
                kept.append(same_size_free_box(chunk, hdr, size))
                continue

            if typ == b"moov":
                body = data[off + hdr : off + size]
                kids: list[bytes] = []
                kid_off = 0
                changed = False
                while kid_off + 8 <= len(body):
                    ksize = struct.unpack(">I", body[kid_off : kid_off + 4])[0]
                    ktyp = body[kid_off + 4 : kid_off + 8]
                    khdr = 8
                    if ksize == 1:
                        if kid_off + 16 > len(body):
                            kids.append(body[kid_off:])
                            break
                        ksize = struct.unpack(">Q", body[kid_off + 8 : kid_off + 16])[0]
                        khdr = 16
                    elif ksize == 0:
                        ksize = len(body) - kid_off
                    if ksize < khdr or kid_off + ksize > len(body):
                        kids.append(body[kid_off:])
                        break
                    if ktyp in drop_moov:
                        removed.append(
                            f"moov/{ktyp.decode('latin-1', errors='replace')}:{ksize}"
                        )
                        changed = True
                        kids.append(same_size_free_box(body[kid_off : kid_off + ksize], khdr, ksize))
                    else:
                        kids.append(body[kid_off : kid_off + ksize])
                    kid_off += ksize
                if changed:
                    # Keep the original box header form (32-bit or 64-bit size).
                    new_body = b"".join(kids)
                    new_size = hdr + len(new_body)
                    chunk = bytearray(chunk[:hdr])
                    if hdr == 16:
                        struct.pack_into(">Q", chunk, 8, new_size)
                    else:
                        struct.pack_into(">I", chunk, 0, new_size)
                    chunk = bytes(chunk) + new_body
            kept.append(chunk)

        if not removed:
            return src, []
        if not kept:
            return src, removed

        out = b"".join(kept)
        if len(out) < 32 or len(out) != len(data):
            # Sanity: size must match source so offsets stay valid.
            return src, removed

        # Ensure destination is a different path from source.
        if dst.resolve(strict=False) == src.resolve(strict=False):
            dst = src.with_name(f"{src.stem}_qqstrip{src.suffix or '.mp4'}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(out)
        return dst, removed

    async def _prepare_qq_playable_mp4(self, path_obj: Path) -> tuple[Path, bool]:
        """Local mp4 -> QQ-playable local mp4 (separate temp files, never in-place).

        Only used by the QQ branch. WeChat keeps the original downloaded file.
        Returns (send_path, sanitized_ok); sanitized_ok=False when local cleaning
        failed, in which case callers should avoid raw-base64 Video candidates.

        Steps:
        1) sanitize to `*_qqstrip.mp4` (C2PA uuid -> same-size free; no ffmpeg)
        2) ffmpeg remux to `*_qqplay.mp4` with metadata wiped
        3) ffmpeg re-encode H.264/AAC if remux fails
        """
        import shutil
        import subprocess

        suffix = path_obj.suffix or ".mp4"
        strip_path = path_obj.with_name(f"{path_obj.stem}_qqstrip{suffix}")
        play_path = path_obj.with_name(f"{path_obj.stem}_qqplay{suffix}")
        work_path = path_obj
        sanitized_ok = True

        try:
            stripped_path, removed = self._sanitize_mp4_for_qq(path_obj, strip_path)
            if removed:
                logger.info(
                    f"{LOG} QQ 视频已清洗本地文件: {stripped_path.name} removed={','.join(removed)}"
                )
                work_path = stripped_path
        except Exception as exc:
            sanitized_ok = False
            logger.warning(f"{LOG} QQ 视频本地清洗失败: {safe_log_text(exc, 120)}")

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            # No ffmpeg: still return cleaned local mp4 for QQ Video card attempts.
            return work_path, sanitized_ok

        def _run_ffmpeg(cmd: list[str], timeout: int) -> tuple[bool, str]:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                ok = (
                    proc.returncode == 0
                    and play_path.exists()
                    and play_path.stat().st_size > 0
                    and play_path.resolve(strict=False) != Path(cmd[cmd.index("-i") + 1]).resolve(strict=False)
                )
                err = (proc.stderr or proc.stdout or "")[-300:]
                return ok, err
            except Exception as exc:  # noqa: BLE001 - surfaced to caller log
                return False, str(exc)

        # Input and output MUST differ; ffmpeg refuses in-place edits.
        if play_path.resolve(strict=False) == work_path.resolve(strict=False):
            play_path = path_obj.with_name(f"{path_obj.stem}_qqplay2{suffix}")

        remux_cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(work_path),
            "-map_metadata",
            "-1",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(play_path),
        ]
        ok, err = await asyncio.to_thread(_run_ffmpeg, remux_cmd, 120)
        if ok:
            logger.info(f"{LOG} QQ 视频已 remux 为可播放文件: {play_path.name}")
            return play_path, True
        if err:
            logger.warning(f"{LOG} ffmpeg remux 失败: {safe_log_text(err, 200)}")

        encode_cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(work_path),
            "-map_metadata",
            "-1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(play_path),
        ]
        ok, err = await asyncio.to_thread(_run_ffmpeg, encode_cmd, 300)
        if ok:
            logger.info(f"{LOG} QQ 视频已转码为可播放文件: {play_path.name}")
            return play_path, True
        if err:
            logger.warning(f"{LOG} ffmpeg 转码失败: {safe_log_text(err, 200)}")

        return work_path, sanitized_ok

    async def _send_video_result(
        self,
        event: AstrMessageEvent,
        *,
        result_path: str,
        result_url: str,
        info: str,
        task_id: str,
    ) -> bool:
        """Send generated video with multiple platform fallbacks.

        Returns True when a non-text video/file component is sent.
        """
        errors: list[str] = []
        path_obj = Path(result_path) if result_path else None
        has_local = bool(
            path_obj and path_obj.exists() and path_obj.is_file() and path_obj.stat().st_size > 0
        )
        if result_path and not has_local:
            logger.warning(f"{LOG} 本地视频不存在或为空: {result_path}")

        if has_local:
            try:
                import astrbot.api.message_components as Comp

                is_qq = self._is_qq_platform(event)
                plain_cls = getattr(Comp, "Plain", None)
                # Two separate pipelines:
                # - QQ: local download -> sanitize/convert -> Video card candidates
                # - WeChat/others: original local mp4 only (do NOT run QQ convert)
                send_path = path_obj
                qq_sanitized_ok = True
                if is_qq:
                    send_path, qq_sanitized_ok = await self._prepare_qq_playable_mp4(path_obj)

                candidates = (
                    self._build_qq_video_candidates(
                        Comp, send_path, result_url, allow_base64=qq_sanitized_ok
                    )
                    if is_qq
                    else self._build_generic_video_candidates(Comp, path_obj)
                )

                # QQ: prefer callback HTTP URL for Video when available. This avoids
                # base64:// + callback_api_base FileNotFoundError and helps remote NapCat.
                if is_qq:
                    callback_url = await self._register_video_callback_url(send_path)
                    video_cls = getattr(Comp, "Video", None)
                    if callback_url and video_cls is not None:
                        if callable(getattr(video_cls, "fromURL", None)):
                            try:
                                candidates.insert(0, video_cls.fromURL(callback_url))
                            except Exception as exc:
                                errors.append(
                                    f"VideoCallback:{safe_log_text(exc, 120)}"
                                )

                    # File only as the final fallback for QQ.
                    file_cls = getattr(Comp, "File", None)
                    if file_cls is not None:
                        try:
                            candidates.append(
                                file_cls(
                                    name=send_path.name,
                                    file=str(send_path.resolve(strict=False)),
                                )
                            )
                        except Exception:
                            pass

                for component in candidates:
                    try:
                        if is_qq:
                            await self.context.send_message(
                                event.unified_msg_origin,
                                MessageChain(chain=[component]),
                            )
                            if info and plain_cls is not None:
                                try:
                                    await self._send_text(event, info)
                                except Exception as exc:
                                    logger.warning(
                                        f"{LOG} 视频已发送，但附加信息发送失败: {safe_log_text(exc, 120)}"
                                    )
                        else:
                            chain_list = [component]
                            if info and plain_cls is not None:
                                chain_list.append(plain_cls(info))
                            await self.context.send_message(
                                event.unified_msg_origin,
                                MessageChain(chain=chain_list),
                            )
                        comp_type = type(component).__name__
                        if is_qq and comp_type == "File":
                            logger.warning(
                                f"{LOG} QQ 仅以 File 附件发送成功（非可播放视频气泡）: path={send_path.name} "
                                f"errors={'; '.join(errors) if errors else 'none'}"
                            )
                        else:
                            logger.info(
                                f"{LOG} 已通过组件发送本地视频: type={comp_type} path={send_path.name} "
                                f"platform={self._platform_kind(event)}"
                            )
                        return True
                    except Exception as exc:
                        err = f"{type(component).__name__}:{safe_log_text(exc, 200)}"
                        errors.append(err)
                        logger.warning(
                            f"{LOG} 候选组件发送失败: type={type(component).__name__} "
                            f"platform={self._platform_kind(event)} err={safe_log_text(exc, 200)}"
                        )
            except Exception as exc:
                errors.append(f"CompImport:{safe_log_text(exc, 120)}")
                logger.warning(f"{LOG} 组件发送初始化失败: {safe_log_text(exc, 160)}")

            for method_name in ("video", "file", "file_image"):
                try:
                    chain = MessageChain()
                    method = getattr(chain, method_name, None)
                    if not callable(method):
                        continue
                    method(str(path_obj))
                    if info:
                        chain.message("\n" + info)
                    await self.context.send_message(event.unified_msg_origin, chain)
                    logger.info(
                        f"{LOG} 已通过 MessageChain.{method_name} 发送本地视频: {path_obj.name}"
                    )
                    return True
                except Exception as exc:
                    errors.append(f"{method_name}:{safe_log_text(exc, 120)}")

        if result_url:
            text_msg = f"✅ 视频已生成\n{result_url}"
            if info:
                text_msg += f"\n{info}"
            if errors:
                logger.warning(
                    f"{LOG} 本地视频发送失败，降级为URL: {'; '.join(errors)}"
                )
            await self._send_text(event, text_msg)
            return False

        if has_local:
            text_msg = f"✅ 视频已生成\n文件: {path_obj}"
            if info:
                text_msg += f"\n{info}"
            if errors:
                logger.warning(
                    f"{LOG} 本地视频发送失败，降级为路径文本: {'; '.join(errors)}"
                )
            await self._send_text(event, text_msg)
            return False

        await self._send_text(event, f"❌ 视频完成但没有可发送内容 [{task_id}]")
        return False

    def _maybe_delete_local_video(self, result_path: str) -> None:
        """Delete local temp video after successful send when enabled."""
        if not self.config_manager.generation.auto_delete_after_send:
            return
        if not result_path:
            return
        try:
            path = Path(result_path)
            if path.exists() and path.is_file():
                path.unlink()
                logger.debug(f"{LOG} 已删除本地视频: {path.name}")
            suffix = path.suffix or ".mp4"
            for extra in (
                path.with_name(f"{path.stem}_qqstrip{suffix}"),
                path.with_name(f"{path.stem}_qqplay{suffix}"),
                path.with_name(f"{path.stem}_qqplay2{suffix}"),
            ):
                if extra.exists() and extra.is_file():
                    extra.unlink()
                    logger.debug(f"{LOG} 已删除 QQ 临时视频: {extra.name}")
        except Exception as exc:
            logger.warning(f"{LOG} 删除本地视频失败: {safe_log_text(exc)}")

    @filter.command("视频任务")
    async def video_task_command(self, event: AstrMessageEvent, task_id: str = ""):
        self._reload_runtime()
        task_id = (task_id or "").strip()
        if task_id:
            record = self._resolve_task_ref(event, task_id, active_only=False)
            if not record:
                yield event.plain_result(f"❌ 任务不存在或已清理: {task_id}")
                return
            yield event.plain_result(format_task_detail(record))
            return
        records = self.task_manager.list_active(event.unified_msg_origin)
        yield event.plain_result(format_task_list(records))

    @filter.command("视频取消")
    async def video_cancel_command(self, event: AstrMessageEvent, task_id: str = ""):
        self._reload_runtime()
        task_id = (task_id or "").strip()
        if not task_id:
            yield event.plain_result("用法: /视频取消 <编号或任务ID>")
            return
        record = self._resolve_task_ref(event, task_id, active_only=True)
        if not record:
            yield event.plain_result(f"❌ 未找到进行中的任务: {task_id}")
            return
        updated = self.task_manager.request_cancel(record.task_id)
        if not updated:
            yield event.plain_result("❌ 任务无法取消")
            return
        yield event.plain_result(f"✅ 已请求取消任务 {updated.task_id}")

    def _preset_entries_from_config(self) -> list[str]:
        try:
            section = self.raw_config.get("presets")
            entries = section.get("preset_list") if isinstance(section, dict) else None
            if isinstance(entries, str):
                return [entries] if entries.strip() else []
            if isinstance(entries, (list, tuple)):
                return [str(item) for item in entries if str(item).strip()]
        except Exception as exc:
            logger.warning(f"{LOG} 读取预设配置失败: {safe_log_text(exc)}")
        return []

    def _save_preset_entries(self, entries: list[str]) -> bool:
        try:
            section = self.raw_config.get("presets")
            if not isinstance(section, dict):
                section = {}
                self.raw_config["presets"] = section
            section["preset_list"] = entries
            save = getattr(self.raw_config, "save_config", None)
            if callable(save):
                save()
            return True
        except Exception as exc:
            logger.error(f"{LOG} 保存预设配置失败: {safe_log_text(exc)}", exc_info=True)
            return False

    def _upsert_preset_entry(self, name: str, content: str) -> bool:
        entries = self._preset_entries_from_config()
        entry_text = f"{name}:{content}"
        kept = [
            item
            for item in entries
            if name not in parse_presets([item])
        ]
        kept.append(entry_text)
        return self._save_preset_entries(kept)

    def _remove_preset_entry(self, name: str) -> bool:
        entries = self._preset_entries_from_config()
        kept: list[str] = []
        removed = False
        for item in entries:
            if name in parse_presets([item]):
                removed = True
                continue
            kept.append(item)
        if removed:
            self._save_preset_entries(kept)
        return removed

    @filter.command("视频预设")
    async def preset_command(self, event: AstrMessageEvent):
        self._reload_runtime()
        text = (event.message_str or "").strip()
        for prefix in ("/视频预设", "／视频预设", "视频预设"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        presets = self.config_manager.presets

        if not text:
            if not presets:
                yield event.plain_result(
                    "暂无预设。管理员可用 /视频预设 添加 <名称:提示词> 添加，"
                    "之后 /视频 <名称> [额外提示词] 调用"
                )
                return
            lines = ["已配置的预设提示词（/视频 <名称> [额外提示词] 调用）:"]
            for index, preset in enumerate(presets.values(), start=1):
                label = f" | {preset.params_label}" if preset.params_label else ""
                lines.append(f"{index}. {preset.name} — {preset.prompt[:60]}{label}")
            yield event.plain_result("\n".join(lines))
            return

        action, _, rest = text.partition(" ")
        rest = rest.strip()
        if action in ("添加", "新增", "add"):
            if not self._is_admin(event):
                yield event.plain_result("❌ 仅管理员可管理预设")
                return
            parts = re.split(r"[:：]", rest, maxsplit=1)
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                yield event.plain_result("用法: /视频预设 添加 <名称:提示词>（名称请勿为纯数字）")
                return
            name, content = parts[0].strip(), parts[1].strip()
            if not self._upsert_preset_entry(name, content):
                yield event.plain_result("❌ 预设保存失败，请查看插件日志")
                return
            self._reload_runtime()
            total = len(self.config_manager.presets)
            yield event.plain_result(
                f"✅ 已保存预设「{name}」（共 {total} 个），用 /视频 {name} [额外提示词] 调用"
            )
            return
        if action in ("删除", "移除", "del", "remove"):
            if not self._is_admin(event):
                yield event.plain_result("❌ 仅管理员可管理预设")
                return
            if not rest:
                yield event.plain_result("用法: /视频预设 删除 <名称>")
                return
            if not self._remove_preset_entry(rest):
                yield event.plain_result(f"❌ 未找到预设: {rest}")
                return
            self._reload_runtime()
            yield event.plain_result(f"✅ 已删除预设「{rest}」")
            return

        preset = presets.get(text)
        if not preset:
            yield event.plain_result(f"❌ 未找到预设: {text}，用 /视频预设 查看全部")
            return
        lines = [f"预设: {preset.name}", f"提示词: {preset.prompt}"]
        if preset.params_label:
            lines.append(f"参数: {preset.params_label}")
        yield event.plain_result("\n".join(lines))

    @filter.command("视频")
    async def video_command(self, event: AstrMessageEvent):
        self._reload_runtime()
        if limit_msg := self._check_limits(event):
            yield event.plain_result(limit_msg)
            return
        if rejection := self.task_manager.get_queue_rejection():
            yield event.plain_result(rejection)
            return

        message = ""
        try:
            message = event.message_str or ""
        except Exception:
            message = ""
        if not message:
            try:
                message = event.get_message_str() or ""
            except Exception:
                message = ""

        parsed = parse_video_command(
            message,
            default_duration=self.config_manager.generation.default_duration,
            default_aspect_ratio=self.config_manager.generation.default_aspect_ratio,
            default_resolution=self.config_manager.generation.default_resolution,
        )
        if parsed.errors:
            yield event.plain_result("❌ " + "；".join(parsed.errors))
            return

        images = await collect_event_images(
            event,
            max_images=self.config_manager.usage.max_reference_images,
            max_size_mb=self.config_manager.usage.max_reference_size_mb,
        )
        if len(images) > self.config_manager.usage.max_reference_images:
            yield event.plain_result(
                f"❌ 参考图最多 {self.config_manager.usage.max_reference_images} 张"
            )
            return

        mode = "image" if images else "text"
        matched_presets, extra_prompt = match_presets(
            parsed.prompt, self.config_manager.presets
        )
        prompt_text = combine_prompt(matched_presets, extra_prompt)
        preset = matched_presets[0] if matched_presets else None

        # Precedence per param: explicit (positional/inline) > preset >
        # [aspect only] image auto-fit > config default. 0/"" = 不指定, which
        # is omitted from the upstream request so the model applies defaults.
        duration = parsed.duration if parsed.duration_explicit else 0
        if not duration and preset and preset.duration:
            duration = preset.duration
        if not duration:
            duration = parsed.duration

        aspect_ratio = parsed.aspect_ratio if parsed.aspect_explicit else ""
        if not aspect_ratio and preset and preset.aspect_ratio:
            aspect_ratio = preset.aspect_ratio
        if mode == "image" and not aspect_ratio and images:
            detected = detect_image_aspect_ratio(images[0])
            if detected:
                aspect_ratio = detected
                logger.info(
                    f"{LOG} 图生视频自动适配比例: {detected} (task preview)"
                )
        if not aspect_ratio:
            aspect_ratio = parsed.aspect_ratio

        resolution = parsed.resolution if parsed.resolution_explicit else ""
        if not resolution and preset and preset.resolution:
            resolution = preset.resolution
        if not resolution:
            resolution = parsed.resolution
        model = (
            (preset.model if preset and preset.model else "")
            or self.config_manager.generation.model
            or self.config_manager.adapter.model
        )
        task_id = new_task_id()
        record = VideoTaskRecord(
            task_id=task_id,
            unified_msg_origin=event.unified_msg_origin,
            prompt=prompt_text,
            duration=duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            model=model,
            reference_image_count=len(images),
            mode=mode,
            status=VideoTaskStatus.QUEUED,
            message="任务已排队",
        )
        self.task_manager.create_record(record)
        self._mark_usage(event)

        start_text = format_start_task_message(
            self.config_manager.generation.start_task_message_template,
            task_id=task_id,
            prompt=prompt_text,
            duration=duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            model=model,
            reference_image_count=len(images),
            mode=mode,
            preset_names=[item.name for item in matched_presets],
        )
        yield event.plain_result(start_text)

        async def worker():
            await self._run_video_task(event, record, images)

        await self.task_manager.enqueue(task_id, worker)

    async def _run_video_task(self, event: AstrMessageEvent, record: VideoTaskRecord, images) -> None:
        current = self.task_manager.get(record.task_id)
        if not current:
            return
        if current.cancel_requested:
            self.task_manager.update(
                record.task_id,
                status=VideoTaskStatus.CANCELLED,
                finished_at=datetime.now(),
                message="任务已取消",
            )
            return

        enhanced_prompt = build_enhanced_video_prompt(
            record.prompt,
            has_reference_image=bool(images),
            enabled=self.config_manager.generation.enable_prompt_enhancement,
            common_enhancement=self.config_manager.generation.common_prompt_enhancement,
            image_enhancement=self.config_manager.generation.image_prompt_enhancement,
            text_enhancement=self.config_manager.generation.text_prompt_enhancement,
        )
        request = VideoRequest(
            prompt=enhanced_prompt,
            duration=record.duration,
            aspect_ratio=record.aspect_ratio,
            resolution=record.resolution,
            model=record.model,
            images=images,
            task_id=record.task_id,
        )

        def should_cancel() -> bool:
            latest = self.task_manager.get(record.task_id)
            return bool(latest and latest.cancel_requested)

        result = await self.adapter.generate(request, should_cancel=should_cancel)
        latest = self.task_manager.get(record.task_id)
        if not latest:
            return

        if latest.cancel_requested or (result.error and "取消" in result.error):
            self.task_manager.update(
                record.task_id,
                status=VideoTaskStatus.CANCELLED,
                finished_at=datetime.now(),
                message="任务已取消",
                upstream_request_id=result.upstream_request_id or latest.upstream_request_id,
                error=result.error or "",
            )
            try:
                await self._send_text(event, f"❎ 任务已取消 [{record.task_id}]")
            except Exception:
                pass
            return

        if result.error:
            self.task_manager.update(
                record.task_id,
                status=VideoTaskStatus.FAILED,
                finished_at=datetime.now(),
                message="任务失败",
                error=result.error,
                upstream_request_id=result.upstream_request_id or "",
            )
            try:
                await self._send_text(
                    event,
                    f"❌ 视频生成失败 [{record.task_id}]: {result.error}",
                )
            except Exception as exc:
                logger.error(f"{LOG} 发送失败消息出错: {safe_log_text(exc)}")
            return

        result_path = ""
        if result.video_bytes:
            temp_dir = self._ensure_temp_dir()
            path = temp_dir / f"{record.task_id}.mp4"
            path.write_bytes(result.video_bytes)
            result_path = str(path)

        self.task_manager.update(
            record.task_id,
            status=VideoTaskStatus.SUCCEEDED,
            finished_at=datetime.now(),
            message="任务完成",
            result_path=result_path,
            result_url=result.video_url or "",
            upstream_request_id=result.upstream_request_id or "",
        )
        latest = self.task_manager.get(record.task_id) or record
        info = format_result_info(latest, self.config_manager.generation.result_info_items)

        try:
            sent_as_media = await self._send_video_result(
                event,
                result_path=result_path,
                result_url=result.video_url or "",
                info=info,
                task_id=record.task_id,
            )
            if not sent_as_media:
                logger.warning(
                    f"{LOG} 任务 {record.task_id} 未通过视频/文件组件发送，已使用文本降级"
                )
            self._maybe_delete_local_video(result_path)
            if self.config_manager.generation.auto_delete_after_send and result_path:
                self.task_manager.update(record.task_id, result_path="")
        except Exception as exc:
            logger.error(f"{LOG} 发送视频失败: {safe_log_text(exc)}", exc_info=True)
            fallback = result.video_url or result_path or ""
            try:
                await self._send_text(
                    event,
                    f"✅ 视频已生成，但直接发送失败。请手动查看: {fallback}\n{info}",
                )
            except Exception:
                pass

