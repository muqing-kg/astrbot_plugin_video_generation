"""AstrBot video generation plugin entrypoint."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .core.adapters.generator import VideoGenerator
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
from .core.shared.sendplan import build_video_component
from .core.shared.types import VideoRequest
from .core.tasks.ids import new_task_id
from .core.tasks.manager import TaskManager
from .core.tasks.models import VideoTaskRecord, VideoTaskStatus

LOG = log_prefix("Plugin")


@register(
    "astrbot_plugin_video_generation",
    "沐倾",
    "通用视频生成插件",
    "v0.5.3",
)
class VideoGenerationPlugin(Star):
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
        self.generator: VideoGenerator | None = None
        active_provider = self.config_manager.active_provider
        if active_provider is not None:
            self.generator = VideoGenerator(active_provider)
        self._rate_limit_until: dict[str, float] = {}
        self._daily_usage: dict[str, dict[str, int]] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    async def initialize(self):
        self._ensure_temp_dir()
        self.task_manager.start_workers()
        active = self.config_manager.active_provider
        active_desc = (
            f"{active.name}/{active.model}" if active is not None else "未配置供应商"
        )
        logger.info(
            f"{LOG} 插件已加载 line={safe_log_text(active_desc)} "
            f"timeout={self.config_manager.generation.timeout_seconds}s"
        )

    async def terminate(self):
        try:
            await self.task_manager.stop()
            if self.generator is not None:
                await self.generator.close()
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

    async def _reload_runtime(self) -> None:
        self.config_manager.reload(self.raw_config)
        self.task_manager.configure(
            max_running_tasks=self.config_manager.generation.max_running_tasks,
            max_queued_tasks=self.config_manager.generation.max_queued_tasks,
            enable_history=False,
        )
        self.task_manager.start_workers()
        active_provider = self.config_manager.active_provider
        if self.generator is not None:
            await self.generator.close()
            self.generator = None
        if active_provider is not None:
            self.generator = VideoGenerator(active_provider)

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
        Some model MP4s embed a top-level `uuid` C2PA content credential and a large
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

    async def _prepare_playable_mp4(self, path_obj: Path) -> tuple[Path, bool]:
        """Local mp4 -> bridge-playable local mp4 (separate temp files, never in-place).

        Used by BOTH the QQ and WeChat branches: bridges reject model MP4s with
        C2PA uuid boxes / fat metadata regardless of platform. Returns
        (send_path, sanitized_ok); sanitized_ok is informational only — hygiene
        is best-effort and the single send form proceeds either way.

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
                    f"{LOG} 视频本地清洗: {stripped_path.name} removed={','.join(removed)}"
                )
                work_path = stripped_path
        except Exception as exc:
            sanitized_ok = False
            logger.warning(f"{LOG} 视频本地清洗失败: {safe_log_text(exc, 120)}")

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
            logger.info(f"{LOG} 视频已 remux 为可播放文件: {play_path.name}")
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
            logger.info(f"{LOG} 视频已转码为可播放文件: {play_path.name}")
            return play_path, True
        if err:
            logger.warning(f"{LOG} ffmpeg 转码失败: {safe_log_text(err, 200)}")

        return work_path, sanitized_ok

    async def _send_video_result(
        self,
        event: AstrMessageEvent,
        *,
        result_path: str,
        info: str,
    ) -> bool:
        """Hand the finished video to the platform adapter, once, in the single
        proven form per platform.

        结果语义：组件交给桥之后，桥返回错误才向群里报告失败（错误原样透传）；
        无错误则插件静默。仅插件自身问题（无可用形态/文件缺失/组件库异常）
        同样说明原因。Returns True when the component was handed off.
        """
        path_obj = Path(result_path) if result_path else None
        has_local = bool(
            path_obj and path_obj.exists() and path_obj.is_file() and path_obj.stat().st_size > 0
        )

        if not has_local:
            logger.warning(f"{LOG} 本地视频不存在或为空: {result_path}")
            await self._send_text(event, "❌ 视频未能发出（插件侧原因）：本地视频文件缺失")
            return False

        try:
            import astrbot.api.message_components as Comp
        except Exception as exc:
            logger.warning(f"{LOG} 组件库导入失败: {safe_log_text(exc, 160)}")
            await self._send_text(
                event, f"❌ 视频未能发出（插件侧原因）：消息组件库导入失败 {safe_log_text(exc, 120)}"
            )
            return False

        is_qq = self._is_qq_platform(event)
        plain_cls = getattr(Comp, "Plain", None)
        # 桥对带 C2PA uuid / 冗余元数据的 MP4 一律拒收，两平台统一先清洗
        send_path, _sanitized = await self._prepare_playable_mp4(path_obj)
        component, form = build_video_component(
            Comp,
            send_path,
            callback_configured=self._callback_api_base_configured(),
            is_qq=is_qq,
        )
        if component is None:
            logger.warning(f"{LOG} 无可用视频发送形态: {form}")
            await self._send_text(event, f"❌ 视频未能发出（插件侧原因）：{form}")
            return False

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
                            f"{LOG} 视频已交给平台，但附加信息发送失败: {safe_log_text(exc, 120)}"
                        )
            else:
                chain_list = [component]
                if info and plain_cls is not None:
                    chain_list.append(plain_cls(info))
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain(chain=chain_list),
                )
        except Exception as exc:
            # 交付后桥返回了错误：如实转述一次，不重试、不换形态、不解读
            reason = safe_log_text(str(exc), 200)
            logger.warning(
                f"{LOG} 桥返回错误: form={form} platform={self._platform_kind(event)} err={reason}"
            )
            await self._send_text(event, f"❌ 视频发送失败：{reason}")
            return False

        logger.info(
            f"{LOG} 已将视频交给平台适配器: form={form} path={send_path.name} "
            f"platform={self._platform_kind(event)}"
        )
        return True

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
        await self._reload_runtime()
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
        await self._reload_runtime()
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
        await self._reload_runtime()
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
            await self._reload_runtime()
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
            await self._reload_runtime()
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

    @filter.command("视频模型")
    async def video_model_command(self, event: AstrMessageEvent, choice: str = ""):
        await self._reload_runtime()
        choices = self.config_manager.model_choices()
        if not choices:
            yield event.plain_result("暂无可用视频线路，请先在插件配置页「视频供应商」中添加")
            return
        current = self.config_manager.current_model_setting or (
            f"{self.generator.provider.name}/{self.generator.provider.model}"
            if self.generator is not None and self.generator.provider.model
            else ""
        )
        if not choice:
            lines = ["可用视频线路（/视频模型 <序号> 切换）:"]
            for index, item in enumerate(choices, start=1):
                lines.append(f"{index}. {item}" + ("  ✓" if item == current else ""))
            yield event.plain_result("\n".join(lines))
            return
        target = None
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(choices):
                target = choices[index - 1]
        elif choice in choices:
            target = choice
        else:
            for item in choices:
                if item.startswith(choice) or choice in item:
                    target = item
                    break
        if target is None:
            yield event.plain_result(f"❌ 未找到对应的视频线路: {choice}")
            return
        self.config_manager.save_video_model(target)
        await self._reload_runtime()
        yield event.plain_result(f"✅ 当前视频线路: {target}")

    @filter.command("视频")
    async def video_command(self, event: AstrMessageEvent):
        await self._reload_runtime()
        if limit_msg := self._check_limits(event):
            yield event.plain_result(limit_msg)
            return
        if rejection := self.task_manager.get_queue_rejection():
            yield event.plain_result(rejection)
            return
        if self.generator is None or self.generator.provider is None:
            yield event.plain_result("❌ 请先在插件配置页「视频供应商」中添加供应商")
            return
        if not self.generator.provider.model:
            yield event.plain_result(
                "❌ 当前供应商未选择模型，请用 /视频模型 切换或在配置中填写模型列表"
            )
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
            or self.generator.provider.model
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

        result = await self.generator.generate(request, should_cancel=should_cancel)
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
            await self._send_video_result(
                event,
                result_path=result_path,
                info=info,
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

