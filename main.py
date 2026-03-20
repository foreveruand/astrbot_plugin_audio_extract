"""
AstrBot Audio Extract Plugin - Extract audio from video files and clip videos.

This plugin provides audio extraction from video files using FFmpeg,
video clipping by time range, subtitle sync, and file index management.
"""

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import Plain
from astrbot.api.util import SessionController, session_waiter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.telegram.tg_event import (
    TelegramCallbackQueryEvent,
    TelegramPlatformEvent,
)
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .ffmpeg_utils import (
    build_audio_extract_command,
    build_video_clip_command,
    ffmpeg_progress_generator,
    normalize_time_format,
    validate_time_format,
)
from .file_selector import FileSelector, LocalIndexDB

logger = logging.getLogger("astrbot")

SESSION_TIMEOUT = 300

# Store inline keyboard session state for Telegram platform
KEYBOARD_SESSIONS: dict[str, dict] = {}


class Main(star.Star):
    """Main class for the Audio Extract plugin."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config
        self._initialized = False

    async def _send_stream_updates(
        self,
        event: AstrMessageEvent,
        stream_factory: Callable[[], AsyncGenerator[str, None]],
    ) -> None:
        """Send progress updates by editing a single message.

        On Telegram, this sends one message and edits it for each update.
        On other platforms, falls back to sending individual messages.
        """
        # Check if we're on Telegram and can use message editing
        is_telegram = isinstance(event, TelegramPlatformEvent)

        if is_telegram:
            await self._send_progress_telegram(event, stream_factory)
        else:
            # Fallback: send messages one by one on other platforms
            async for text in stream_factory():
                if text:
                    await event.send(MessageChain([Plain(text)]))

    async def _send_progress_telegram(
        self,
        event: TelegramPlatformEvent,
        stream_factory: Callable[[], AsyncGenerator[str, None]],
    ) -> None:
        """Send progress updates on Telegram using message editing."""
        from astrbot.core.platform.astrbot_message import MessageType

        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            chat_id = event.message_obj.group_id
        else:
            chat_id = event.get_sender_id()

        # Handle supergroup thread_id
        message_thread_id = None
        if isinstance(chat_id, str) and "#" in chat_id:
            chat_id, message_thread_id = chat_id.split("#")

        message_id = None
        last_update_time = 0.0
        throttle_interval = 0.5  # Minimum interval between edits (seconds)
        last_text = ""

        async for text in stream_factory():
            if not text:
                continue

            current_time = asyncio.get_running_loop().time()

            # Throttle: skip if too soon since last update (unless first message)
            if message_id and (current_time - last_update_time) < throttle_interval:
                continue

            try:
                if message_id is None:
                    # Send initial message
                    payload = {"chat_id": chat_id, "text": text}
                    if message_thread_id:
                        payload["message_thread_id"] = int(message_thread_id)
                    msg = await event.client.send_message(**payload)
                    message_id = msg.message_id
                    last_text = text
                else:
                    # Edit existing message (only if text changed)
                    if text != last_text:
                        await event.client.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=text,
                        )
                        last_text = text

                last_update_time = current_time
            except Exception as exc:
                logger.warning(f"Failed to send/edit progress message: {exc}")
                # If edit fails, try sending a new message
                try:
                    payload = {"chat_id": chat_id, "text": text}
                    if message_thread_id:
                        payload["message_thread_id"] = int(message_thread_id)
                    msg = await event.client.send_message(**payload)
                    message_id = msg.message_id
                    last_text = text
                    last_update_time = current_time
                except Exception as exc2:
                    logger.error(f"Failed to send fallback message: {exc2}")

    async def initialize(self) -> None:
        """Called when the plugin is activated."""
        if self._initialized:
            return

        await self._init()

        self._initialized = True
        logger.info("Audio Extract plugin initialized successfully")

    async def _init(self) -> None:
        """Initialize plugin components."""
        self.data_path = Path(get_astrbot_data_path() , "astrbot_plugin_audio_extract")
        self.data_path.mkdir(parents=True, exist_ok=True)

        self.work_dir = Path(self.config.get("work_dir", self.data_path))
        self.out_dir = Path(self.config.get("out_dir", "/tmp/audio_extract"))

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / ".jobs").mkdir(parents=True, exist_ok=True)

        await self._init_scheduled_jobs()

        logger.info(f"Work directory: {self.work_dir}")
        logger.info(f"Output directory: {self.out_dir}")

    async def _init_scheduled_jobs(self) -> None:
        """Initialize scheduled jobs for subtitle sync and index refresh."""
        await self._add_job(
            job_name="audio_extract_subtitle_sync",
            cron_expression="*/2 * * * *",
            handler=self._subtitle_sync_job,
            description="Audio Extract Plugin: Sync subtitles",
        )

        await self._add_job(
            job_name="audio_extract_index_refresh",
            cron_expression="0 */6 * * *",
            handler=self._index_refresh_job,
            description="Audio Extract Plugin: Refresh file index",
        )

        await self._add_job(
            job_name="audio_extract_full_rebuild",
            cron_expression="0 3 * * *",
            handler=self._full_rebuild_job,
            description="Audio Extract Plugin: Full rebuild file index",
        )

    async def _add_job(
        self, job_name: str, cron_expression: str, handler, description: str
    ) -> None:
        """Add or update a scheduled job."""
        jobs = await self.context.cron_manager.list_jobs(job_type="basic")
        for job in jobs:
            if job.name == job_name:
                await self.context.cron_manager.delete_job(job.job_id)
                logger.info(f"Deleted existing job: {job_name}")
                break

        await self.context.cron_manager.add_basic_job(
            name=job_name,
            cron_expression=cron_expression,
            handler=handler,
            description=description,
            persistent=False,
            enabled=True,
        )
        logger.info(f"Added scheduled job: {job_name}")

    async def _subtitle_sync_job(self) -> None:
        """Scheduled task: Sync subtitle files."""
        job_dir = self.work_dir / ".jobs"

        for job_file in job_dir.glob("*.json"):
            try:
                job = json.loads(job_file.read_text())
                video_path = Path(job["video"])
                mp3_file = Path(job["mp3"])

                ass_file = self.out_dir / (video_path.stem + ".ass")
                srt_file = self.out_dir / (video_path.stem + ".srt")

                if not ass_file.exists() and not srt_file.exists():
                    continue

                logger.debug(f"Found subtitle file: {video_path.stem}")

                video_ext = video_path.suffix.lower()

                if video_ext == ".flac":
                    target = video_path.with_suffix(".lrc")
                    try:
                        self._convert_vtt_to_lrc(str(srt_file), str(target))
                        logger.info(f"LRC conversion complete: {target}")
                    except Exception as e:
                        logger.error(f"LRC conversion failed: {e}")
                        continue

                    if ass_file.exists():
                        ass_file.unlink()

                else:
                    if ass_file.exists():
                        target = video_path.with_suffix(".zh-CN.default.ass")
                        shutil.move(str(ass_file), str(target))
                        logger.info(f"Subtitle move complete: {target}")
                    else:
                        target = video_path.with_suffix(".zh-CN.default.srt")
                        shutil.move(str(srt_file), str(target))
                        logger.info(f"Subtitle move complete: {target}")

                if srt_file.exists():
                    srt_file.unlink()
                if mp3_file.exists():
                    mp3_file.unlink()
                job_file.unlink()

            except Exception as e:
                logger.error(f"Failed to process job {job_file}: {e}")

    async def _index_refresh_job(self) -> None:
        """Scheduled task: Incremental refresh of file index."""
        logger.info("Starting incremental file index refresh...")
        scan_dirs = self.config.get("scan_dirs", [])
        for scan_dir in scan_dirs:
            LocalIndexDB.build_index(scan_dir, incremental=True)
        logger.info("File index refresh complete")

    async def _full_rebuild_job(self) -> None:
        """Scheduled task: Full rebuild of file index."""
        logger.info("Starting full rebuild of file index...")
        scan_dirs = self.config.get("scan_dirs", [])
        for scan_dir in scan_dirs:
            LocalIndexDB.rebuild_index_full(scan_dir)
        logger.info("File index full rebuild complete")

    def _convert_vtt_to_lrc(self, vtt_path: str, lrc_path: str) -> None:
        """Convert VTT subtitle to LRC format."""
        with open(vtt_path, encoding="utf-8") as f:
            lines = f.readlines()

        lrc_lines = []
        for i, line in enumerate(lines):
            line = line.strip()
            if "-->" in line:
                ts = line.split("-->")[0].strip()
                lrc_time = self._vtt_timestamp_to_lrc(ts)

                if i + 2 < len(lines):
                    text = lines[i + 2].strip()
                    if text and not text.isdigit():
                        lrc_lines.append(f"{lrc_time}{text}")
                if i + 1 < len(lines):
                    text = lines[i + 1].strip()
                    if text and not text.isdigit():
                        lrc_lines.append(f"{lrc_time}{text}")

        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lrc_lines))

    def _vtt_timestamp_to_lrc(self, ts: str) -> str:
        """Convert VTT timestamp to LRC format (ignore milliseconds)."""
        ts = ts.replace(",", ".")
        parts = ts.split(":")
        if len(parts) != 3:
            return "[00:00]"

        h, m, s = parts
        if "." in s:
            s = s.split(".")[0]
        h, m, s = int(h), int(m), int(s)
        total_m = h * 60 + m
        return f"[{total_m:02d}:{s:02d}]"

    async def terminate(self) -> None:
        """Called when the plugin is disabled or reloaded."""
        logger.info("Audio Extract plugin terminated")

    def _build_file_list_message(self, results: list[str], keyword: str) -> str:
        """Build the file selection message."""
        lines = [f"🔍 找到 {len(results)} 个匹配「{keyword}」的文件：", ""]
        for i, path in enumerate(results[:15], 1):
            name = Path(path).name
            lines.append(f"{i}. {name}")
        if len(results) > 15:
            lines.append(f"... 还有 {len(results) - 15} 个文件")

        lines.append("")
        lines.append("请回复要处理的文件序号（逗号分隔）")
        lines.append("示例: 1,3,5 或 1-5 或 全部")
        lines.append("回复「取消」退出")

        return "\n".join(lines)

    def _parse_selection(self, reply: str, max_count: int) -> list[int] | None:
        """Parse user selection input.

        Supports:
        - Single number: 1
        - Comma separated: 1,3,5
        - Range: 1-5
        - All: 全部, all

        Returns list of 0-based indices or None if invalid.
        """
        reply = reply.strip().lower()

        if reply in ("全部", "all", "全选"):
            return list(range(min(max_count, 15)))

        indices = set()
        parts = reply.replace("，", ",").split(",")

        for part in parts:
            part = part.strip()
            if not part:
                continue

            if "-" in part:
                # Range: 1-5
                try:
                    start, end = part.split("-", 1)
                    start, end = int(start.strip()), int(end.strip())
                    for i in range(start, end + 1):
                        if 1 <= i <= max_count:
                            indices.add(i - 1)
                except ValueError:
                    return None
            else:
                # Single number
                try:
                    idx = int(part)
                    if 1 <= idx <= max_count:
                        indices.add(idx - 1)
                except ValueError:
                    return None

        return sorted(indices) if indices else None

    def _build_inline_keyboard(
        self, session_id: str, results: list[str], selected: set[int]
    ) -> list[list[dict]]:
        """Build inline keyboard buttons for file selection."""
        buttons = []
        # File buttons (3 per row)
        for i in range(0, len(results), 3):
            row = []
            for j in range(3):
                idx = i + j
                if idx >= len(results):
                    break
                file_name = Path(results[idx]).name
                # Truncate long filenames
                display_name = file_name[:10] + "..." if len(file_name) > 10 else file_name
                prefix = "✓ " if idx in selected else ""
                row.append({
                    "text": f"{prefix}{idx + 1}. {display_name}",
                    "callback_data": f"auex:{session_id}:{idx}",
                })
            buttons.append(row)

        # Action buttons
        action_row = [
            {"text": "全部", "callback_data": f"auex:{session_id}:all"},
            {"text": "确认", "callback_data": f"auex:{session_id}:confirm"},
            {"text": "取消", "callback_data": f"auex:{session_id}:cancel"},
        ]
        buttons.append(action_row)

        return buttons

    @filter.callback_query()
    async def handle_auex_callback(self, event: TelegramCallbackQueryEvent) -> None:
        """Handle inline keyboard button clicks for auex command."""
        if not event.data.startswith("auex:"):
            return

        parts = event.data.split(":")
        if len(parts) < 3:
            return

        session_id = parts[1]
        action = parts[2]

        session = KEYBOARD_SESSIONS.get(session_id)
        if not session:
            await event.answer_callback_query(text="会话已过期，请重新发送命令")
            return

        results = session["results"]
        selected = session["selected"]

        if action == "cancel":
            # Cancel operation
            del KEYBOARD_SESSIONS[session_id]
            await event.answer_callback_query(text="已取消操作")
            result = MessageEventResult()
            result.message("已取消操作。")
            event.set_result(result)
            return

        if action == "all":
            # Select all files
            selected.clear()
            selected.update(range(len(results)))
            session["selected"] = selected

            await event.answer_callback_query(text=f"已选择全部 {len(results)} 个文件")

            # Update keyboard
            result = MessageEventResult()
            result.message(f"🔍 已选择全部 {len(results)} 个文件")
            result.inline_keyboard(self._build_inline_keyboard(session_id, results, selected))
            event.set_result(result)
            return

        if action == "confirm":
            # Confirm selection
            if not selected:
                await event.answer_callback_query(text="请至少选择一个文件")
                return

            selected_files = [results[i] for i in sorted(selected)]
            del KEYBOARD_SESSIONS[session_id]

            await event.answer_callback_query(text=f"已选择 {len(selected_files)} 个文件，开始处理...")

            result = MessageEventResult()
            result.message(f"✅ 已选择 {len(selected_files)} 个文件，开始处理...")
            event.set_result(result)

            # Process audio extraction
            await self._process_audio_extraction(event, selected_files)
            return

        # Toggle file selection
        try:
            file_idx = int(action)
            if file_idx in selected:
                selected.discard(file_idx)
                await event.answer_callback_query(text=f"已取消选择文件 {file_idx + 1}")
            else:
                selected.add(file_idx)
                await event.answer_callback_query(text=f"已选择文件 {file_idx + 1}")

            session["selected"] = selected

            # Build selection status message
            selected_count = len(selected)
            msg = f"🔍 已选择 {selected_count} 个文件" if selected_count > 0 else "🔍 请选择文件"

            # Update keyboard
            result = MessageEventResult()
            result.message(msg)
            result.inline_keyboard(self._build_inline_keyboard(session_id, results, selected))
            event.set_result(result)

        except ValueError:
            await event.answer_callback_query(text="无效操作")

    @filter.command("auex")
    async def auex(self, event: AstrMessageEvent):
        """Extract audio from video files.

        Usage:
            /auex <keyword> - Search and extract audio from video files matching keyword
        """
        await self.initialize()

        message = event.message_str.strip()
        keyword = message.replace("auex", "", 1).strip()

        if not keyword:
            yield event.plain_result(
                "用法: /auex <关键词>\n搜索匹配关键词的视频文件并提取音频为 MP3 格式。"
            )
            return

        selector = FileSelector(self.config)
        results = await selector.search_files(keyword, limit=15)

        if not results:
            yield event.plain_result(f"未找到匹配「{keyword}」的文件")
            return

        # Single file - process directly
        if len(results) == 1:
            yield event.plain_result("找到 1 个文件，开始处理...")
            await self._process_audio_extraction(event, results)
            return

        # Multiple files - check platform for inline keyboard support
        is_telegram = event.get_platform_name() == "telegram"

        if is_telegram:
            # Use inline keyboard for Telegram
            session_id = uuid.uuid4().hex[:8]
            KEYBOARD_SESSIONS[session_id] = {
                "results": results,
                "selected": set(),
                "keyword": keyword,
            }

            msg = f"🔍 找到 {len(results)} 个匹配「{keyword}」的文件"
            result = MessageEventResult()
            result.message(msg)
            result.inline_keyboard(
                self._build_inline_keyboard(session_id, results, set())
            )
            event.set_result(result)
            return

        # Non-Telegram platforms: use text selection
        msg = self._build_file_list_message(results, keyword)
        yield event.plain_result(msg)

        @session_waiter(timeout=SESSION_TIMEOUT)
        async def wait_for_selection(
            controller: SessionController, reply_event: AstrMessageEvent
        ) -> None:
            reply_text = reply_event.message_str.strip()

            if reply_text.lower() in ("取消", "cancel", "退出", "exit"):
                await reply_event.send(reply_event.plain_result("已取消操作。"))
                controller.stop()
                return

            selected = self._parse_selection(reply_text, len(results))
            if selected is None:
                await reply_event.send(
                    reply_event.plain_result(
                        "无效输入，请输入序号（如 1,3,5 或 1-5）\n回复「取消」退出"
                    )
                )
                return

            if not selected:
                await reply_event.send(
                    reply_event.plain_result("请至少选择一个文件\n回复「取消」退出")
                )
                return

            selected_files = [results[i] for i in selected]
            await reply_event.send(
                reply_event.plain_result(
                    f"已选择 {len(selected_files)} 个文件，开始处理..."
                )
            )
            await self._process_audio_extraction(reply_event, selected_files)
            controller.stop()

        try:
            await wait_for_selection(event)
        except TimeoutError:
            yield event.plain_result("⏰ 等待超时，操作已取消。")

    async def _process_audio_extraction(
        self, event: AstrMessageEvent, video_paths: list[str]
    ) -> None:
        """Process audio extraction for multiple video files."""
        for i, video_path in enumerate(video_paths, 1):
            video_name = Path(video_path).stem
            temp_mp3_path = str(self.work_dir / f"{video_name}.mp3")
            mp3_path = str(self.out_dir / f"{video_name}.mp3")
            job_file = str(self.work_dir / ".jobs" / f"{Path(video_path).name}.json")

            Path(job_file).write_text(
                json.dumps({"video": video_path, "mp3": mp3_path})
            )

            cmd = build_audio_extract_command(video_path, temp_mp3_path)

            async def progress_stream() -> AsyncGenerator[str, None]:
                base_prefix = f"[{i}/{len(video_paths)}] {video_name[:30]}"
                yield f"{base_prefix} 提取中..."
                async for status, msg in ffmpeg_progress_generator(cmd):
                    if status == "progress":
                        yield f"{base_prefix}\n{msg}"
                    elif status == "success":
                        shutil.move(temp_mp3_path, mp3_path)
                        yield f"{base_prefix} ✅ 音频提取完成"
                        return
                    elif status == "failed":
                        Path(job_file).unlink(missing_ok=True)
                        yield f"{base_prefix} ❌ 失败: {msg}"
                        return
                    elif status == "exception":
                        Path(job_file).unlink(missing_ok=True)
                        yield f"{base_prefix} ❌ 出错: {msg}"
                        return

            await self._send_stream_updates(event, progress_stream)

        await event.send(
            event.plain_result(f"✅ 全部完成! 共处理 {len(video_paths)} 个文件")
        )

    @filter.command("vclip")
    async def vclip(self, event: AstrMessageEvent):
        """Clip video by time range.

        Usage:
            /vclip <keyword> <start_time> <end_time> - Clip video segment
            Time format: HH:MM:SS or MM:SS
            Example: /vclip movie 00:05:30 00:10:45
        """
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("vclip", "", 1).strip().split()

        if len(parts) < 3:
            yield event.plain_result(
                "用法: /vclip <关键词> <开始时间> <结束时间>\n"
                "时间格式: HH:MM:SS 或 MM:SS\n"
                "示例: /vclip movie 00:05:30 00:10:45"
            )
            return

        keyword = parts[0]
        start_time = parts[1].replace("：", ":")
        end_time = parts[2].replace("：", ":")

        if not validate_time_format(start_time) or not validate_time_format(end_time):
            yield event.plain_result("无效的时间格式，请使用 HH:MM:SS 或 MM:SS")
            return

        start_time = normalize_time_format(start_time)
        end_time = normalize_time_format(end_time)

        selector = FileSelector(self.config)
        results = await selector.search_files(keyword, limit=15)

        if not results:
            yield event.plain_result(f"未找到匹配「{keyword}」的文件")
            return

        # Store time parameters in a closure
        time_params = (start_time, end_time)

        # Single file - process directly
        if len(results) == 1:
            yield event.plain_result(
                f"找到 1 个文件，开始剪辑...\n⏱ {start_time} → {end_time}"
            )
            await self._process_video_clip(event, results, start_time, end_time)
            return

        # Multiple files - let user select
        msg = self._build_file_list_message(results, keyword)
        msg += f"\n\n⏱ 剪辑时间: {start_time} → {end_time}"
        yield event.plain_result(msg)

        @session_waiter(timeout=SESSION_TIMEOUT)
        async def wait_for_selection(
            controller: SessionController, reply_event: AstrMessageEvent
        ) -> None:
            reply_text = reply_event.message_str.strip()

            if reply_text.lower() in ("取消", "cancel", "退出", "exit"):
                await reply_event.send(reply_event.plain_result("已取消操作。"))
                controller.stop()
                return

            selected = self._parse_selection(reply_text, len(results))
            if selected is None:
                await reply_event.send(
                    reply_event.plain_result(
                        "无效输入，请输入序号（如 1,3,5 或 1-5）\n回复「取消」退出"
                    )
                )
                return

            if not selected:
                await reply_event.send(
                    reply_event.plain_result("请至少选择一个文件\n回复「取消」退出")
                )
                return

            selected_files = [results[i] for i in selected]
            await reply_event.send(
                reply_event.plain_result(
                    f"已选择 {len(selected_files)} 个文件，开始剪辑..."
                )
            )
            await self._process_video_clip(
                reply_event, selected_files, time_params[0], time_params[1]
            )
            controller.stop()

        try:
            await wait_for_selection(event)
        except TimeoutError:
            yield event.plain_result("⏰ 等待超时，操作已取消。")

    async def _process_video_clip(
        self,
        event: AstrMessageEvent,
        video_paths: list[str],
        start_time: str,
        end_time: str,
    ) -> None:
        """Process video clipping for multiple video files."""
        for i, video_path in enumerate(video_paths, 1):
            video_path_obj = Path(video_path)

            time_tag = f"{start_time.replace(':', '')}-{end_time.replace(':', '')}"
            output_path = (
                video_path_obj.parent
                / f"{video_path_obj.stem}_clip_{time_tag}{video_path_obj.suffix}"
            )

            cmd = build_video_clip_command(
                video_path, str(output_path), start_time, end_time
            )

            async def progress_stream() -> AsyncGenerator[str, None]:
                base_prefix = f"[{i}/{len(video_paths)}] `{video_path_obj.name}`"
                yield f"{base_prefix} 剪辑中...\n⏱ {start_time} → {end_time}"
                async for status, msg in ffmpeg_progress_generator(cmd):
                    if status == "progress":
                        yield f"{base_prefix}\n{msg}"
                    elif status == "success":
                        yield f"{base_prefix} ✅ 剪辑完成\n📁 {output_path.name}"
                        return
                    elif status == "failed":
                        yield f"{base_prefix} ❌ 剪辑失败: {msg}"
                        return
                    elif status == "exception":
                        yield f"{base_prefix} ❌ 出错: {msg}"
                        return

            await self._send_stream_updates(event, progress_stream)

        await event.send(
            MessageChain([Plain(f"✅ 全部完成! 共剪辑 {len(video_paths)} 个文件")])
        )

    @filter.command("aurebuild")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def aurebuild(self, event: AstrMessageEvent) -> None:
        """Rebuild file index (admin only).

        Usage:
            /aurebuild - Rebuild file index for all configured directories
        """
        await self.initialize()

        await event.send(event.plain_result("开始重建文件索引..."))

        scan_dirs = self.config.get("scan_dirs", [])
        if not scan_dirs:
            await event.send(event.plain_result("未配置扫描目录。"))
            return

        for scan_dir in scan_dirs:
            LocalIndexDB.rebuild_index_full(scan_dir)

        await event.send(
            event.plain_result(f"✅ 文件索引重建完成，共 {len(scan_dirs)} 个目录。")
        )
