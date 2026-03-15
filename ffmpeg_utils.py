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
    """
    if line.startswith("out_time_ms="):
        state["out_time"] = int(line.split("=", 1)[1]) / 1_000_000
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

    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )

        last_yield_time = 0.0

        while True:
            line = await process.stderr.readline()
            if not line:
                break

            decoded = line.decode("utf-8", errors="replace").strip()

            # Try to parse total duration
            if total_duration is None:
                d = parse_ffmpeg_duration(decoded)
                if d:
                    total_duration = d
                    continue

            # Update progress state
            if update_progress_state(decoded, progress_state):
                now = time.monotonic()
                formatted = format_ffmpeg_progress(progress_state, total_duration)

                # Throttle control
                if formatted and (now - last_yield_time >= interval):
                    last_yield_time = now
                    yield ("progress", formatted)

        await process.wait()

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
    """Validate time format (HH:MM:SS or MM:SS)."""
    pattern = r"^\d{1,2}:\d{2}(:\d{2})?$"
    return bool(re.match(pattern, time_str))


def normalize_time_format(time_str: str) -> str:
    """Normalize time format to HH:MM:SS."""
    parts = time_str.split(":")
    if len(parts) == 2:  # MM:SS
        return f"00:{parts[0]}:{parts[1]}"
    return time_str  # HH:MM:SS
