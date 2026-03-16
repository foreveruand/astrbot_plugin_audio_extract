"""SQLite file index database for audio extract plugin."""

import logging
import os
import sqlite3
import time

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

logger = logging.getLogger("astrbot")

VIDEO_EXTS = ["mp4", "mkv", "mov", "wmv", "flv", "webm", "ts", "flac"]


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

    def build_index(self, base_dir: str, incremental: bool = True):
        """
        Build file index.

        Args:
            base_dir: Base directory to scan
            incremental: Whether to do incremental update (True=only update changed files, False=full rebuild)
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        base_dir = os.path.abspath(base_dir)
        now = int(time.time())
        batch = []
        scanned_paths = set()

        logger.info(
            f"Starting {'incremental' if incremental else 'full'} scan of: {base_dir}"
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
                    p = os.path.join(root, f)
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

            # Delete non-existent files
            logger.info("Cleaning up deleted files...")
            self._cleanup_deleted_files(cursor, base_dir, scanned_paths)
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

    def _cleanup_deleted_files(self, cursor, base_dir: str, scanned_paths: set):
        """
        Delete database records for files that no longer exist.

        Args:
            cursor: Database cursor
            base_dir: Base directory
            scanned_paths: Set of all paths scanned this time
        """
        # Query all files in this directory from database
        cursor.execute("SELECT path FROM files WHERE path LIKE ?", (base_dir + "%",))
        db_paths = [row[0] for row in cursor.fetchall()]

        # Find paths to delete
        to_delete = []
        for db_path in db_paths:
            if db_path not in scanned_paths:
                # Confirm file really doesn't exist
                if not os.path.exists(db_path):
                    to_delete.append((db_path,))

        if to_delete:
            logger.info(f"Deleting {len(to_delete)} non-existent file records")
            cursor.executemany("DELETE FROM files WHERE path = ?", to_delete)

    def search_index(
        self, keyword: str, is_dir: bool = False, limit: int = 10
    ) -> list[str]:
        """
        Search for files or directories.

        Args:
            keyword: Search keyword
            is_dir: Whether to search only directories
            limit: Maximum number of results
        """
        paths: list[str] = []

        with self._get_conn() as conn:
            cursor = conn.cursor()

            if is_dir:
                # Search directories
                sql = "SELECT path FROM files WHERE name LIKE ? AND is_dir = 1 LIMIT ?"
                cursor.execute(sql, (f"%{keyword.lower()}%", limit))
                rows = cursor.fetchall()
                paths.extend([r[0] for r in rows])
                return paths

            # Search files, only match VIDEO_EXTS
            for ext in VIDEO_EXTS:
                sql = "SELECT path FROM files WHERE is_dir=0 AND LOWER(name) GLOB ? LIMIT ?"
                cursor.execute(sql, (f"*{keyword.lower()}*.{ext}", limit))
                rows = cursor.fetchall()
                paths.extend([r[0] for r in rows])
                if len(paths) >= limit:
                    break

        return paths[:limit]

    def list_video_files_recursive(self, dir_path: str, limit: int = 50) -> list[str]:
        """
        Return video file paths in specified directory and subdirectories.

        Args:
            dir_path: Directory path
            limit: Maximum number of results
        """
        paths = []
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # Ensure path ends with separator
            dir_path = dir_path.rstrip(os.sep) + os.sep

            for ext in VIDEO_EXTS:
                pattern = f"{dir_path}%.{ext}"
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
            cursor.execute(
                "SELECT last_updated, file_count FROM index_meta WHERE base_dir = ?",
                (base_dir,),
            )
            row = cursor.fetchone()

            if row:
                return {"last_updated": row[0], "file_count": row[1]}
            return {"last_updated": None, "file_count": 0}

    def rebuild_index_full(self, base_dir: str):
        """Fully rebuild index (delete old data and rescan)."""
        conn = self._get_conn()
        cursor = conn.cursor()

        logger.info(f"Starting full index rebuild: {base_dir}")

        try:
            # Delete all old records for this directory
            cursor.execute("DELETE FROM files WHERE path LIKE ?", (base_dir + "%",))
            cursor.execute("DELETE FROM index_meta WHERE base_dir = ?", (base_dir,))
            conn.commit()

            # Rebuild
            conn.close()
            self.build_index(base_dir, incremental=False)

        except Exception as e:
            logger.error(f"Full index rebuild failed: {e}")
            conn.rollback()
        finally:
            if conn:
                conn.close()
