"""SQLite-backed migration state, so runs can be stopped and resumed safely."""

import sqlite3
import threading
from typing import Iterable, List, Optional

from .models import Entry

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items (
    path_lower      TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    dbx_id          TEXT NOT NULL,
    path_display    TEXT NOT NULL,
    name            TEXT NOT NULL,
    depth           INTEGER NOT NULL,
    size            INTEGER NOT NULL DEFAULT 0,
    rev             TEXT,
    content_hash    TEXT,
    client_modified TEXT,
    downloadable    INTEGER NOT NULL DEFAULT 1,
    exportable      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | skipped
    drive_id        TEXT,
    done_rev        TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS items_status ON items(kind, status);
"""

COLS = ("path_lower, kind, dbx_id, path_display, name, depth, size, rev, content_hash, "
        "client_modified, downloadable, exportable, status, drive_id, done_rev, attempts, error")


class State:
    def __init__(self, path: str):
        self._lock = threading.RLock()  # one shared connection: serialize ALL access
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # -- meta -----------------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # -- scan -----------------------------------------------------------------
    def upsert_entries(self, entries: Iterable[Entry]) -> None:
        """Record a batch from the Dropbox listing. Files whose rev changed since they
        were copied go back to 'pending' and will overwrite the existing Drive copy."""
        with self._lock:
            self.db.execute("BEGIN")
            try:
                for e in entries:
                    self.db.execute(
                        """INSERT INTO items(path_lower, kind, dbx_id, path_display, name, depth, size, rev,
                                             content_hash, client_modified, downloadable, exportable)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(path_lower) DO UPDATE SET
                             kind=excluded.kind, dbx_id=excluded.dbx_id, path_display=excluded.path_display,
                             name=excluded.name, size=excluded.size, rev=excluded.rev,
                             content_hash=excluded.content_hash, client_modified=excluded.client_modified,
                             downloadable=excluded.downloadable, exportable=excluded.exportable,
                             status=CASE
                               WHEN items.kind != excluded.kind THEN 'pending'
                               WHEN items.status='done' AND excluded.kind='file'
                                    AND IFNULL(items.done_rev,'') != IFNULL(excluded.rev,'') THEN 'pending'
                               ELSE items.status END,
                             drive_id=CASE WHEN items.kind != excluded.kind THEN NULL ELSE items.drive_id END""",
                        (e.path_lower, e.kind, e.id, e.path_display, e.name, e.depth, e.size, e.rev,
                         e.content_hash, e.client_modified, int(e.downloadable), int(e.exportable)),
                    )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    # -- queries --------------------------------------------------------------
    def _all(self, sql, params=()):
        with self._lock:
            return self.db.execute(sql, params).fetchall()

    def _one(self, sql, params=()):
        with self._lock:
            return self.db.execute(sql, params).fetchone()

    def folder_depths(self) -> List[int]:
        rows = self._all(
            "SELECT DISTINCT depth FROM items WHERE kind='folder' AND status!='done' ORDER BY depth")
        return [r[0] for r in rows]

    def pending_folders_at(self, depth: int) -> List[sqlite3.Row]:
        return self._all(
            f"SELECT {COLS} FROM items WHERE kind='folder' AND status!='done' AND depth=? ORDER BY path_lower",
            (depth,))

    def pending_file_keys(self, include_skipped: bool = False) -> List[str]:
        statuses = ("pending", "failed", "skipped") if include_skipped else ("pending", "failed")
        marks = ",".join("?" * len(statuses))
        return [r[0] for r in self._all(
            f"SELECT path_lower FROM items WHERE kind='file' AND status IN ({marks}) ORDER BY path_lower",
            statuses)]

    def pending_file_bytes(self, include_skipped: bool = False) -> int:
        statuses = ("pending", "failed", "skipped") if include_skipped else ("pending", "failed")
        marks = ",".join("?" * len(statuses))
        return self._one(f"SELECT IFNULL(SUM(size),0) FROM items WHERE kind='file' AND status IN ({marks})",
                         statuses)[0]

    def get(self, path_lower: str) -> Optional[sqlite3.Row]:
        return self._one(f"SELECT {COLS} FROM items WHERE path_lower=?", (path_lower,))

    def drive_id_of(self, path_lower: str) -> Optional[str]:
        row = self._one("SELECT drive_id FROM items WHERE path_lower=? AND kind='folder'", (path_lower,))
        return row[0] if row else None

    # -- updates --------------------------------------------------------------
    def mark_done(self, path_lower: str, drive_id: str, rev: Optional[str]) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE items SET status='done', drive_id=?, done_rev=?, error=NULL, attempts=attempts+1 "
                "WHERE path_lower=?", (drive_id, rev, path_lower))

    def mark_error(self, path_lower: str, error: str, permanent: bool) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE items SET status=?, error=?, attempts=attempts+1 WHERE path_lower=?",
                ("skipped" if permanent else "failed", error[:2000], path_lower))

    def summary(self) -> dict:
        out = {}
        for r in self._all("SELECT kind, status, COUNT(*), IFNULL(SUM(size),0) FROM items GROUP BY kind, status"):
            out[(r[0], r[1])] = (r[2], r[3])
        return out

    def problems(self, limit: int = 50) -> List[sqlite3.Row]:
        return self._all(
            "SELECT path_display, status, error FROM items WHERE status IN ('failed','skipped') "
            "ORDER BY path_lower LIMIT ?", (limit,))

    def undownloadable_count(self) -> int:
        return self._one("SELECT COUNT(*) FROM items WHERE kind='file' AND downloadable=0 AND exportable=0")[0]
