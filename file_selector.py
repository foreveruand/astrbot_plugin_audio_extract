"""File selector component for audio extract plugin."""

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .database import LocalIndex

if TYPE_CHECKING:
    from astrbot.api import AstrBotConfig

logger = logging.getLogger("astrbot")

# Global database instance
LocalIndexDB = LocalIndex("file_index.db")

# Index build control
_index_lock = asyncio.Lock()
_index_building: set[str] = set()


async def request_rebuild_async(base_dir: str, force_full: bool = False):
    """
    Asynchronously request index rebuild (prevent duplicate builds).

    Args:
        base_dir: Base directory
        force_full: Whether to force full rebuild
    """
    async with _index_lock:
        if base_dir in _index_building:
            logger.debug(f"Index is being built, skipping: {base_dir}")
            return
        _index_building.add(base_dir)

    loop = asyncio.get_running_loop()

    async def runner():
        try:
            if force_full:
                await loop.run_in_executor(
                    None, LocalIndexDB.rebuild_index_full, base_dir
                )
            else:
                await loop.run_in_executor(
                    None, LocalIndexDB.build_index, base_dir, True
                )
        finally:
            async with _index_lock:
                _index_building.discard(base_dir)

    asyncio.create_task(runner())


async def search_files_async(keyword: str, is_dir=False, limit=10) -> list[str] | None:
    """Async search for files, trigger index rebuild if no results."""
    results = LocalIndexDB.search_index(keyword, is_dir=is_dir, limit=limit)

    # If no results, return None to indicate rebuild needed
    if not results:
        return None

    return results


class FileSelector:
    """File selector component - simplified for AstrBot."""

    def __init__(self, config: "AstrBotConfig"):
        self.config = config

    async def search_files(self, keyword: str, limit: int = 15) -> list[str]:
        """
        Search for files matching keyword.

        Args:
            keyword: Search keyword
            limit: Maximum number of results

        Returns:
            List of file paths matching the keyword
        """
        results = await search_files_async(keyword, limit=limit)

        if results is None:
            # No results, trigger index rebuild
            logger.info(f"No results found, triggering index rebuild for: {keyword}")
            scan_dirs = self.config.get("scan_dirs", [])

            for scan_dir in scan_dirs:
                await request_rebuild_async(scan_dir)

            # Wait for index to complete (max 30 seconds)
            for _ in range(30):
                await asyncio.sleep(1)
                results = await search_files_async(keyword, limit=limit)
                if results is not None:
                    break

            # Still no results
            if results is None or len(results) == 0:
                return []

        paths = list(results)

        # If searching files yields no results, try searching directories
        if len(paths) < 1:
            dirs = await search_files_async(keyword, is_dir=True, limit=limit)

            if dirs is None or len(dirs) == 0:
                return []

            for dir_path in dirs:
                paths.extend(
                    LocalIndexDB.list_video_files_recursive(dir_path, limit=limit)
                )

        return paths

    def build_file_list_message(self, paths: list[str], extra_info: str = "") -> str:
        """Build file list message."""
        lines = ["Files found:\n"]

        for idx, p in enumerate(paths[:20], start=1):  # Limit display to 20 files
            path = Path(p)
            lines.append(f"{idx}. `{path.name}`")

        if len(paths) > 20:
            lines.append(f"\n... and {len(paths) - 20} more files")

        if extra_info:
            lines.append(f"\n{extra_info}")

        return "\n".join(lines)
