"""SQLite file index database for audio extract plugin."""

import logging
import os
import sqlite3
import time
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

logger = logging.getLogger("astrbot")

DEFAULT_INDEX_EXTENSIONS = [
    ".mp4",
    ".mkv",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".ts",
    ".flac",
]


def normalize_index_extensions(extensions) -> list[str]:
    """Normalize configured index extensions to lowercase values with leading dots."""
    if extensions is None:
        candidates = DEFAULT_INDEX_EXTENSIONS
    elif isinstance(extensions, str):
        candidates = extensions.replace("，", ",").split(",")
    elif isinstance(extensions, (list, tuple, set)):
        candidates = list(extensions)
    else:
        logger.warning(
            "Invalid index_extensions config type %s, falling back to defaults",
            type(extensions).__name__,
        )
        candidates = DEFAULT_INDEX_EXTENSIONS

    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, str):
            continue
        ext = item.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        if ext in seen:
            continue
        seen.add(ext)
        normalized.append(ext)

    return normalized


class LocalIndex:
    """SQLite-based file index database."""

    def __init__(self, db_name: str):
        # Get plugin directory
        script_dir = get_astrbot_plugin_data_path()
        self.db_path = str(
            os.path.join(script_dir, "astrbot_plugin_audio_extract", db_name)
        )
        logger.info(f"Initializing LocalIndex database at: {script_dir}")
        self.create_tables()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _normalize_base_dir(self, base_dir: str) -> str:
        """Normalize base directory for index metadata and cleanup queries."""
        normalized = str(Path(base_dir).expanduser().resolve())
        if normalized != os.sep:
            normalized = normalized.rstrip(os.sep)
        return normalized

    def _get_scope_query_params(self, base_dir: str) -> tuple[str, str]:
        """Return exact and prefix match parameters for a directory scope."""
        normalized = self._normalize_base_dir(base_dir)
        prefix = normalized if normalized.endswith(os.sep) else normalized + os.sep
        return normalized, prefix + "%"

    def create_tables(self):
        """Create database tables."""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Create files table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                name TEXT,
                is_dir INTEGER,
                mtime INTEGER
            )
        """)

        # Create index metadata table (records last scan time)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS index_meta (
                base_dir TEXT PRIMARY KEY,
                last_updated TEXT,
                file_count INTEGER
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_path_prefix ON files(path)")

        conn.commit()
        conn.close()

    def build_index(
        self,
        base_dir: str,
        incremental: bool = True,
        index_extensions: list[str] | None = None,
    ):
        """
        Build file index.

        Args:
            base_dir: Base directory to scan
            incremental: Whether to do incremental update (True=only update changed files, False=full rebuild)
            index_extensions: File extensions to keep in the index
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        base_dir = self._normalize_base_dir(base_dir)
        normalized_extensions = normalize_index_extensions(index_extensions)
        now = int(time.time())
        batch = []
        scanned_paths: set[str] = set()

        logger.info(
            f"Starting {'incremental' if incremental else 'full'} scan of: {base_dir}"
        )
        logger.info(
            "Indexing file extensions: %s",
            ", ".join(normalized_extensions) if normalized_extensions else "(none)",
        )
        start_time = time.time()

        # Walk file system
        try:
            for root, dirs, files in os.walk(base_dir):
                # Skip hidden and temp directories
                dirs[:] = [
                    d
                    for d in dirs
                    if not d.startswith(".") and d not in ["@eaDir", "Temp", "tmp"]
                ]

                # Add directories
                for d in dirs:
                    p = os.path.join(root, d)
                    scanned_paths.add(p)

                    if incremental:
                        # Check if update needed
                        cursor.execute("SELECT mtime FROM files WHERE path = ?", (p,))
                        row = cursor.fetchone()
                        try:
                            current_mtime = int(os.path.getmtime(p))
                        except Exception:
                            current_mtime = now

                        if not row or row[0] != current_mtime:
                            batch.append((p, d.lower(), 1, current_mtime))
                    else:
                        batch.append((p, d.lower(), 1, now))

                # Add files
                for f in files:
                    file_path = Path(root, f)
                    if file_path.suffix.lower() not in normalized_extensions:
                        continue

                    p = str(file_path)
                    scanned_paths.add(p)

                    if incremental:
                        # Check if update needed
                        cursor.execute("SELECT mtime FROM files WHERE path = ?", (p,))
                        row = cursor.fetchone()
                        try:
                            current_mtime = int(os.path.getmtime(p))
                        except Exception:
                            current_mtime = now

                        if not row or row[0] != current_mtime:
                            batch.append((p, f.lower(), 0, current_mtime))
                    else:
                        batch.append((p, f.lower(), 0, now))

                    # Batch commit
                    if len(batch) >= 1000:
                        cursor.executemany(
                            "REPLACE INTO files VALUES (?, ?, ?, ?)", batch
                        )
                        conn.commit()
                        batch.clear()

            # Commit remaining
            if batch:
                cursor.executemany("REPLACE INTO files VALUES (?, ?, ?, ?)", batch)
                conn.commit()

            # Delete stale files that are no longer included in the scoped scan
            logger.info("Cleaning up stale file index entries...")
            self._cleanup_stale_entries(cursor, base_dir, scanned_paths)
            conn.commit()

            # Update metadata
            file_count = len(scanned_paths)
            cursor.execute(
                "REPLACE INTO index_meta VALUES (?, ?, ?)",
                (base_dir, time.strftime("%Y-%m-%d %H:%M:%S"), file_count),
            )
            conn.commit()

            elapsed = time.time() - start_time
            logger.info(
                f"Index build complete: {file_count} entries, elapsed {elapsed:.2f}s"
            )

        except Exception as e:
            logger.error(f"Failed to build index: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _cleanup_stale_entries(
        self, cursor, base_dir: str, scanned_paths: set[str]
    ) -> None:
        """
        Delete database records no longer covered by the current scan.

        Args:
            cursor: Database cursor
            base_dir: Base directory
            scanned_paths: Set of all paths scanned this time
        """
        scope_base_dir, scope_prefix = self._get_scope_query_params(base_dir)
        cursor.execute(
            "SELECT path FROM files WHERE path = ? OR path LIKE ?",
            (scope_base_dir, scope_prefix),
        )
        db_paths = [row[0] for row in cursor.fetchall()]

        # Find paths to delete
        to_delete = [(db_path,) for db_path in db_paths if db_path not in scanned_paths]

        if to_delete:
            logger.info(f"Deleting {len(to_delete)} stale file records")
            cursor.executemany("DELETE FROM files WHERE path = ?", to_delete)

    def search_index(
        self,
        keyword: str,
        is_dir: bool = False,
        limit: int = 10,
        index_extensions: list[str] | None = None,
    ) -> list[str]:
        """
        Search for files or directories.

        Args:
            keyword: Search keyword
            is_dir: Whether to search only directories
            limit: Maximum number of results
            index_extensions: File extensions allowed in file results
        """
        paths: list[str] = []
        normalized_extensions = normalize_index_extensions(index_extensions)

        with self._get_conn() as conn:
            cursor = conn.cursor()

            if is_dir:
                # Search directories
                sql = "SELECT path FROM files WHERE name LIKE ? AND is_dir = 1 LIMIT ?"
                cursor.execute(sql, (f"%{keyword.lower()}%", limit))
                rows = cursor.fetchall()
                paths.extend([r[0] for r in rows])
                return paths

            if not normalized_extensions:
                return []

            # Search files, only match configured extensions
            for ext in normalized_extensions:
                sql = "SELECT path FROM files WHERE is_dir=0 AND LOWER(name) GLOB ? LIMIT ?"
                cursor.execute(sql, (f"*{keyword.lower()}*{ext}", limit))
                rows = cursor.fetchall()
                paths.extend([r[0] for r in rows])
                if len(paths) >= limit:
                    break

        return paths[:limit]

    def list_video_files_recursive(
        self,
        dir_path: str,
        limit: int = 50,
        index_extensions: list[str] | None = None,
    ) -> list[str]:
        """
        Return video file paths in specified directory and subdirectories.

        Args:
            dir_path: Directory path
            limit: Maximum number of results
            index_extensions: File extensions allowed in file results
        """
        normalized_extensions = normalize_index_extensions(index_extensions)
        if not normalized_extensions:
            return []

        paths = []
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # Ensure path ends with separator
            dir_path = dir_path.rstrip(os.sep) + os.sep

            for ext in normalized_extensions:
                pattern = f"{dir_path}%{ext}"
                cursor.execute(
                    "SELECT path FROM files WHERE is_dir=0 AND LOWER(path) LIKE ? LIMIT ?",
                    (pattern.lower(), limit),
                )
                rows = cursor.fetchall()
                paths.extend([r[0] for r in rows])
                if len(paths) >= limit:
                    break

        return paths[:limit]

    def get_index_info(self, base_dir: str) -> dict:
        """
        Get index information.

        Returns:
            dict: {"last_updated": "2024-01-01 12:00:00", "file_count": 1000}
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            base_dir = self._normalize_base_dir(base_dir)
            cursor.execute(
                "SELECT last_updated, file_count FROM index_meta WHERE base_dir = ?",
                (base_dir,),
            )
            row = cursor.fetchone()

            if row:
                return {"last_updated": row[0], "file_count": row[1]}
            return {"last_updated": None, "file_count": 0}

    def rebuild_index_full(
        self, base_dir: str, index_extensions: list[str] | None = None
    ):
        """Fully rebuild index (delete old data and rescan)."""
        conn = self._get_conn()
        cursor = conn.cursor()

        base_dir = self._normalize_base_dir(base_dir)
        logger.info(f"Starting full index rebuild: {base_dir}")

        try:
            # Delete all old records for this directory
            scope_base_dir, scope_prefix = self._get_scope_query_params(base_dir)
            cursor.execute(
                "DELETE FROM files WHERE path = ? OR path LIKE ?",
                (scope_base_dir, scope_prefix),
            )
            cursor.execute("DELETE FROM index_meta WHERE base_dir = ?", (base_dir,))
            conn.commit()

            # Rebuild
            conn.close()
            self.build_index(
                base_dir, incremental=False, index_extensions=index_extensions
            )

        except Exception as e:
            logger.error(f"Full index rebuild failed: {e}")
            conn.rollback()
        finally:
            if conn:
                conn.close()
