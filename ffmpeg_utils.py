"""FFmpeg utility functions for audio extraction and video clipping."""

import asyncio
import logging
import re
import time

logger = logging.getLogger("astrbot")


def parse_ffmpeg_duration(line: str) -> float | None:
    """Parse total duration from FFmpeg output."""
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", line)
    if not m:
        return None
    h, m_, s = m.groups()
    return int(h) * 3600 + int(m_) * 60 + float(s)


def update_progress_state(line: str, state: dict) -> bool:
    """
    Update FFmpeg progress state.
    Returns True if this line may trigger a progress output.

    FFmpeg may emit non-numeric placeholders such as ``out_time_ms=N/A`` when
    running in stream-copy mode (``-c copy``). Those values are skipped instead
    of being fed to ``int()``, so progress parsing never raises and the clip
    operation is not aborted by a harmless ``N/A`` report.
    """
    if line.startswith("out_time_ms="):
        value = line.split("=", 1)[1].strip()
        if value == "N/A" or not value.lstrip("-").isdigit():
            return False
        state["out_time"] = int(value) / 1_000_000
        return True
    if line.startswith("speed="):
        state["speed"] = line.split("=", 1)[1]
        return False
    return False


def format_ffmpeg_progress(
    state: dict, total_duration: float | None = None
) -> str | None:
    """Format FFmpeg progress information."""
    current = state.get("out_time")
    if current is None:
        return None

    speed = state.get("speed") or "?"

    if total_duration:
        total = int(total_duration)
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        percent = min(100.00, current / total_duration * 100)
        return f"Progress: {percent:.2f}% | Duration: {h:02d}:{m:02d}:{s:02d} | Speed: {speed}"
    else:
        return f"time: {current:.1f}s | speed: {speed}"


async def ffmpeg_progress_generator(
    command: list, total_duration: float | None = None, interval: float = 2.0
):
    """
    Execute FFmpeg command and generate progress information.

    yields:
        ("progress", formatted_message) - progress update
        ("success", message) - completed successfully
        ("failed", returncode) - failed
        ("exception", error_message) - exception occurred
    """
    progress_state = {
        "out_time": None,
        "speed": None,
    }
    progress_updated = asyncio.Event()

    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )

        async def read_progress() -> None:
            """Continuously read FFmpeg output and retain only the latest state."""
            nonlocal total_duration

            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break

                    decoded = line.decode("utf-8", errors="replace").strip()

                    if total_duration is None:
                        duration = parse_ffmpeg_duration(decoded)
                        if duration:
                            total_duration = duration
                            continue

                    if update_progress_state(decoded, progress_state):
                        progress_updated.set()
            finally:
                progress_updated.set()

        reader_task = asyncio.create_task(read_progress())
        last_yield_time = 0.0
        last_progress = None

        while True:
            await progress_updated.wait()
            if reader_task.done():
                break

            remaining = interval - (time.monotonic() - last_yield_time)
            if remaining > 0:
                await asyncio.sleep(remaining)

            progress_updated.clear()
            formatted = format_ffmpeg_progress(progress_state, total_duration)
            if formatted and formatted != last_progress:
                last_yield_time = time.monotonic()
                last_progress = formatted
                yield ("progress", formatted)

            if reader_task.done():
                break

        await reader_task

        await process.wait()

        # Always publish the final snapshot, regardless of the update interval.
        final_progress = format_ffmpeg_progress(progress_state, total_duration)
        if final_progress and final_progress != last_progress:
            yield ("progress", final_progress)

        if process.returncode == 0:
            yield ("success", "FFmpeg processing complete")
        else:
            yield ("failed", process.returncode)

    except Exception as e:
        logger.error(f"FFmpeg execution error: {e}")
        yield ("exception", str(e))


def build_audio_extract_command(video_path: str, mp3_path: str) -> list:
    """Build audio extraction command."""
    return [
        "ffmpeg",
        "-y",
        "-progress",
        "pipe:2",
        "-nostats",
        "-i",
        video_path,
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        mp3_path,
    ]


def build_video_clip_command(
    video_path: str, output_path: str, start_time: str, end_time: str
) -> list:
    """
    Build video clip command (using copy mode for speed).

    Args:
        video_path: Input video path
        output_path: Output video path
        start_time: Start time (HH:MM:SS or MM:SS)
        end_time: End time (HH:MM:SS or MM:SS)
    """
    return [
        "ffmpeg",
        "-y",
        "-ss",
        start_time,
        "-to",
        end_time,
        "-i",
        video_path,
        "-c",
        "copy",
        "-avoid_negative_ts",
        "1",
        "-progress",
        "pipe:2",
        "-nostats",
        output_path,
    ]


def validate_time_format(time_str: str) -> bool:
    """Validate time format (HH:MM:SS or MM:SS).

    Rejects syntactically correct but out-of-range values such as ``99:99`` so
    downstream clipping math and FFmpeg do not receive invalid timestamps.
    """
    if not re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", time_str):
        return False
    parts = [int(p) for p in time_str.split(":")]
    if len(parts) == 2:
        minute, second = parts
    else:
        _, minute, second = parts
    return minute < 60 and second < 60


def normalize_time_format(time_str: str) -> str:
    """Normalize time format to HH:MM:SS."""
    parts = time_str.split(":")
    if len(parts) == 2:  # MM:SS
        return f"00:{parts[0]}:{parts[1]}"
    return time_str  # HH:MM:SS


def parse_compact_time_format(time_str: str) -> str | None:
    """Parse compact time format to HH:MM:SS.

    Supported formats:
    - HMMSS (e.g. 10101 -> 01:01:01)
    - HHMMSS (e.g. 120305 -> 12:03:05)
    """
    if not time_str.isdigit() or len(time_str) not in (5, 6):
        return None

    if len(time_str) == 5:
        hour = int(time_str[0])
        minute = int(time_str[1:3])
        second = int(time_str[3:5])
    else:
        hour = int(time_str[0:2])
        minute = int(time_str[2:4])
        second = int(time_str[4:6])

    if minute >= 60 or second >= 60:
        return None

    return f"{hour:02d}:{minute:02d}:{second:02d}"


def parse_compact_time_interval(interval_str: str) -> tuple[str, str] | None:
    """Parse compact time interval string.

    Example:
    - 10101-20356 -> (01:01:01, 02:03:56)
    """
    parts = interval_str.split("-", 1)
    if len(parts) != 2:
        return None

    start = parse_compact_time_format(parts[0].strip())
    end = parse_compact_time_format(parts[1].strip())

    if not start or not end:
        return None

    return (start, end)
