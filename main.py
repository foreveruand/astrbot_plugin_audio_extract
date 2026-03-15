"""
AstrBot Audio Extract Plugin - Extract audio from video files and clip videos.

This plugin provides audio extraction from video files using FFmpeg,
video clipping by time range, subtitle sync, and file index management.
"""

import json
import logging
import shutil
from pathlib import Path

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, filter
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


class Main(star.Star):
    """Main class for the Audio Extract plugin."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        self.context = context
        self.config = config
        self._initialized = False

    async def initialize(self) -> None:
        """Called when the plugin is activated."""
        if self._initialized:
            return

        await self._init()

        self._initialized = True
        logger.info("Audio Extract plugin initialized successfully")

    async def _init(self) -> None:
        """Initialize plugin components."""
        # Get data path
        self.data_path = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_audio_extract"
        )
        self.data_path.mkdir(parents=True, exist_ok=True)

        # Get work and output directories from config
        self.work_dir = Path(self.config.get("work_dir", "resources/audio_extract"))
        self.out_dir = Path(self.config.get("out_dir", "/tmp/audio_extract"))

        # Ensure directories exist
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / ".jobs").mkdir(parents=True, exist_ok=True)

        # Initialize scheduled tasks
        await self._init_scheduled_jobs()

        logger.info(f"Work directory: {self.work_dir}")
        logger.info(f"Output directory: {self.out_dir}")

    async def _init_scheduled_jobs(self) -> None:
        """Initialize scheduled jobs for subtitle sync and index refresh."""
        # Subtitle sync job - every 2 minutes
        await self._add_job(
            job_name="audio_extract_subtitle_sync",
            cron_expression="*/2 * * * *",
            handler=self._subtitle_sync_job,
            description="Audio Extract Plugin: Sync subtitles",
        )

        # Index refresh job - every 6 hours
        await self._add_job(
            job_name="audio_extract_index_refresh",
            cron_expression="0 */6 * * *",
            handler=self._index_refresh_job,
            description="Audio Extract Plugin: Refresh file index",
        )

        # Full rebuild job - daily at 3 AM
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
        # Delete existing job first
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

                # Check if subtitle files exist
                if not ass_file.exists() and not srt_file.exists():
                    continue

                logger.debug(f"Found subtitle file: {video_path.stem}")

                video_ext = video_path.suffix.lower()

                # FLAC files convert to LRC lyrics
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

                # Video files move subtitles
                else:
                    if ass_file.exists():
                        target = video_path.with_suffix(".zh-CN.default.ass")
                        shutil.move(str(ass_file), str(target))
                        logger.info(f"Subtitle move complete: {target}")
                    else:
                        target = video_path.with_suffix(".zh-CN.default.srt")
                        shutil.move(str(srt_file), str(target))
                        logger.info(f"Subtitle move complete: {target}")

                # Clean up temp files
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

                # Read subtitle text
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

    @filter.command("auex")
    async def auex(self, event: AstrMessageEvent) -> None:
        """Extract audio from video files.

        Usage:
            /auex <keyword> - Search and extract audio from video files matching keyword
        """
        await self.initialize()

        message = event.message_str.strip()
        keyword = message.replace("auex", "").strip()

        if not keyword:
            event.set_result(
                event.make_result().message(
                    "Usage: /auex <keyword>\n"
                    "Search for video files matching the keyword and extract audio as MP3."
                )
            )
            return

        # Search for files
        selector = FileSelector(self.config)
        results = await selector.search_files(keyword, limit=15)

        if not results:
            event.set_result(
                event.make_result().message(f"No files found matching: {keyword}")
            )
            return

        # If multiple results, show list and let user select (simplified - just process all)
        if len(results) > 1:
            file_list = "\n".join(
                [f"{i + 1}. {Path(p).name}" for i, p in enumerate(results[:10])]
            )
            event.set_result(
                event.make_result().message(
                    f"Found {len(results)} files. Processing all...\n{file_list}"
                )
            )

        # Process files
        await self._process_audio_extraction(event, results)

    async def _process_audio_extraction(
        self, event: AstrMessageEvent, video_paths: list[str]
    ) -> None:
        """Process audio extraction for multiple video files."""
        for i, video_path in enumerate(video_paths, 1):
            video_name = Path(video_path).stem
            temp_mp3_path = str(self.work_dir / f"{video_name}.mp3")
            mp3_path = str(self.out_dir / f"{video_name}.mp3")
            job_file = str(self.work_dir / ".jobs" / f"{Path(video_path).name}.json")

            # Save job info
            Path(job_file).write_text(
                json.dumps({"video": video_path, "mp3": mp3_path})
            )

            # Build FFmpeg command
            cmd = build_audio_extract_command(video_path, temp_mp3_path)

            # Execute FFmpeg
            status_msg = f"[{i}/{len(video_paths)}] Extracting: {video_name[:30]}"
            event.set_result(event.make_result().message(status_msg))

            async for status, msg in ffmpeg_progress_generator(cmd):
                if status == "progress":
                    event.set_result(
                        event.make_result().message(
                            f"[{i}/{len(video_paths)}] {video_name[:20]}\n{msg}"
                        )
                    )
                elif status == "success":
                    shutil.move(temp_mp3_path, mp3_path)
                    event.set_result(
                        event.make_result().message(
                            f"[{i}/{len(video_paths)}] ✅ {video_name[:30]} audio extraction complete"
                        )
                    )
                elif status == "failed":
                    event.set_result(
                        event.make_result().message(
                            f"[{i}/{len(video_paths)}] ❌ {video_name[:30]} failed: {msg}"
                        )
                    )
                    Path(job_file).unlink(missing_ok=True)

        event.set_result(
            event.make_result().message(
                f"✅ All complete! Processed {len(video_paths)} files"
            )
        )

    @filter.command("vclip")
    async def vclip(self, event: AstrMessageEvent) -> None:
        """Clip video by time range.

        Usage:
            /vclip <keyword> <start_time> <end_time> - Clip video segment
            Time format: HH:MM:SS or MM:SS
            Example: /vclip movie 00:05:30 00:10:45
        """
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("vclip", "").strip().split()

        if len(parts) < 3:
            event.set_result(
                event.make_result().message(
                    "Usage: /vclip <keyword> <start_time> <end_time>\n"
                    "Time format: HH:MM:SS or MM:SS\n"
                    "Example: /vclip movie 00:05:30 00:10:45"
                )
            )
            return

        keyword = parts[0]
        start_time = parts[1].replace("：", ":")
        end_time = parts[2].replace("：", ":")

        # Validate time format
        if not validate_time_format(start_time) or not validate_time_format(end_time):
            event.set_result(
                event.make_result().message(
                    "Invalid time format. Use HH:MM:SS or MM:SS"
                )
            )
            return

        # Normalize time format
        start_time = normalize_time_format(start_time)
        end_time = normalize_time_format(end_time)

        # Search for files
        selector = FileSelector(self.config)
        results = await selector.search_files(keyword, limit=15)

        if not results:
            event.set_result(
                event.make_result().message(f"No files found matching: {keyword}")
            )
            return

        # Process files
        await self._process_video_clip(event, results, start_time, end_time)

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

            # Generate output filename
            time_tag = f"{start_time.replace(':', '')}-{end_time.replace(':', '')}"
            output_path = (
                video_path_obj.parent
                / f"{video_path_obj.stem}_clip_{time_tag}{video_path_obj.suffix}"
            )

            # Build FFmpeg command
            cmd = build_video_clip_command(
                video_path, str(output_path), start_time, end_time
            )

            # Show start message
            event.set_result(
                event.make_result().message(
                    f"[{i}/{len(video_paths)}] Clipping: {video_path_obj.name}\n⏱ {start_time} → {end_time}"
                )
            )

            # Execute FFmpeg
            async for status, msg in ffmpeg_progress_generator(cmd):
                if status == "progress":
                    event.set_result(
                        event.make_result().message(
                            f"[{i}/{len(video_paths)}] {video_path_obj.stem}\n{msg}"
                        )
                    )
                elif status == "success":
                    event.set_result(
                        event.make_result().message(
                            f"[{i}/{len(video_paths)}] ✅ Clip complete\n📁 {output_path.name}"
                        )
                    )
                elif status == "failed":
                    event.set_result(
                        event.make_result().message(
                            f"[{i}/{len(video_paths)}] ❌ Clip failed: {msg}"
                        )
                    )

        event.set_result(
            event.make_result().message(
                f"✅ All complete! Clipped {len(video_paths)} files"
            )
        )

    @filter.command("aurebuild")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def aurebuild(self, event: AstrMessageEvent) -> None:
        """Rebuild file index (admin only).

        Usage:
            /aurebuild - Rebuild file index for all configured directories
        """
        await self.initialize()

        event.set_result(event.make_result().message("Starting file index rebuild..."))

        scan_dirs = self.config.get("scan_dirs", [])
        if not scan_dirs:
            event.set_result(
                event.make_result().message("No scan directories configured.")
            )
            return

        for scan_dir in scan_dirs:
            LocalIndexDB.rebuild_index_full(scan_dir)

        event.set_result(
            event.make_result().message(
                f"✅ File index rebuild complete for {len(scan_dirs)} directories."
            )
        )
