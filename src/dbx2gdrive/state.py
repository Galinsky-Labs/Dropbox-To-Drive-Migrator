"""Migration state, so runs can be stopped and resumed safely.

Backed by SQLite (a local file) or PostgreSQL (for cloud deployments, where the server's
disk is wiped on every restart). Pass a postgres:// URL to use Postgres."""

import logging
import sqlite3
import threading
import time
from typing import Iterable, List, Optional

from .models import Entry

log = logging.getLogger(__name__)

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS items (
        path_lower      TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        dbx_id          TEXT NOT NULL,
        path_display    TEXT NOT NULL,
        name            TEXT NOT NULL,
        depth           INTEGER NOT NULL,
        size            BIGINT NOT NULL DEFAULT 0,
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
    )""",
    "CREATE INDEX IF NOT EXISTS items_status ON items(kind, status)",
]

COLS = ("path_lower, kind, dbx_id, path_display, name, depth, size, rev, content_hash, "
        "client_modified, downloadable, exportable, status, drive_id, done_rev, attempts, error")

UPSERT = """INSERT INTO items(path_lower, kind, dbx_id, path_display, name, depth, size, rev,
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
            AND COALESCE(items.done_rev,'') != COALESCE(excluded.rev,'') THEN 'pending'
       ELSE items.status END,
     drive_id=CASE WHEN items.kind != excluded.kind THEN NULL ELSE items.drive_id END"""


def is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


class _Sqlite:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def executemany(self, sql, rows):
        self.conn.execute("BEGIN")
        try:
            self.conn.executemany(sql, rows)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise


class _Postgres:
    def __init__(self, url):
        import psycopg  # optional dependency: pip install 'dbx2gdrive[postgres]'
        from psycopg.rows import dict_row

        self._psycopg, self._dict_row, self.url = psycopg, dict_row, url
        self._connect()

    def _connect(self):
        self.conn = self._psycopg.connect(self.url, autocommit=True, row_factory=self._dict_row,
                                          keepalives=1, keepalives_idle=30, connect_timeout=30)

    def _run(self, fn):
        # Hosted Postgres (e.g. Neon) can drop idle connections or briefly restart; reconnect.
        for attempt in range(6):
            try:
                return fn()
            except (self._psycopg.OperationalError, self._psycopg.InterfaceError) as exc:
                if attempt == 5:
                    raise
                log.warning("database connection problem (%s); reconnecting", exc)
                time.sleep(min(30, 2 ** attempt))
                try:
                    self.conn.close()
                except Exception:
                    pass
                try:
                    self._connect()
                except Exception as exc2:
                    log.warning("reconnect failed: %s", exc2)

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        return self._run(lambda: self.conn.execute(sql, params))

    def executemany(self, sql, rows):
        sql = sql.replace("?", "%s")

        def go():
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.executemany(sql, rows)
        self._run(go)


class State:
    def __init__(self, path: str):
        self._lock = threading.RLock()  # one shared connection: serialize ALL access
        self.db = _Postgres(path) if is_postgres(path) else _Sqlite(path)
        for stmt in SCHEMA:
            self.db.execute(stmt)

    def _all(self, sql, params=()):
        with self._lock:
            return self.db.execute(sql, params).fetchall()

    def _one(self, sql, params=()):
        with self._lock:
            return self.db.execute(sql, params).fetchone()

    def _write(self, sql, params=()):
        with self._lock:
            self.db.execute(sql, params)

    # -- meta -----------------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._write("INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- scan -----------------------------------------------------------------
    def upsert_entries(self, entries: Iterable[Entry]) -> None:
        """Record a batch from the Dropbox listing. Files whose rev changed since they
        were copied go back to 'pending' and will overwrite the existing Drive copy."""
        rows = [(e.path_lower, e.kind, e.id, e.path_display, e.name, e.depth, e.size, e.rev,
                 e.content_hash, e.client_modified, int(e.downloadable), int(e.exportable)) for e in entries]
        if rows:
            with self._lock:
                self.db.executemany(UPSERT, rows)

    # -- queries --------------------------------------------------------------
    def folder_depths(self) -> List[int]:
        rows = self._all("SELECT DISTINCT depth FROM items WHERE kind='folder' AND status!='done' ORDER BY depth")
        return [r["depth"] for r in rows]

    def pending_folders_at(self, depth: int):
        return self._all(
            f"SELECT {COLS} FROM items WHERE kind='folder' AND status!='done' AND depth=? ORDER BY path_lower",
            (depth,))

    @staticmethod
    def _statuses(include_skipped):
        statuses = ("pending", "failed", "skipped") if include_skipped else ("pending", "failed")
        return statuses, ",".join("?" * len(statuses))

    def pending_file_keys(self, include_skipped: bool = False) -> List[str]:
        statuses, marks = self._statuses(include_skipped)
        return [r["path_lower"] for r in self._all(
            f"SELECT path_lower FROM items WHERE kind='file' AND status IN ({marks}) ORDER BY path_lower",
            statuses)]

    def pending_file_bytes(self, include_skipped: bool = False) -> int:
        statuses, marks = self._statuses(include_skipped)
        return int(self._one(
            f"SELECT COALESCE(SUM(size),0) AS n FROM items WHERE kind='file' AND status IN ({marks})",
            statuses)["n"])

    def get(self, path_lower: str):
        return self._one(f"SELECT {COLS} FROM items WHERE path_lower=?", (path_lower,))

    def drive_id_of(self, path_lower: str) -> Optional[str]:
        row = self._one("SELECT drive_id FROM items WHERE path_lower=? AND kind='folder'", (path_lower,))
        return row["drive_id"] if row else None

    # -- updates --------------------------------------------------------------
    def mark_started(self, path_lower: str) -> None:
        """Recorded BEFORE a copy starts, so after a crash we know a Drive copy may already exist."""
        self._write("UPDATE items SET attempts=attempts+1 WHERE path_lower=?", (path_lower,))

    def mark_done(self, path_lower: str, drive_id: str, rev: Optional[str]) -> None:
        self._write("UPDATE items SET status='done', drive_id=?, done_rev=?, error=NULL "
                    "WHERE path_lower=?", (drive_id, rev, path_lower))

    def mark_error(self, path_lower: str, error: str, permanent: bool) -> None:
        self._write("UPDATE items SET status=?, error=? WHERE path_lower=?",
                    ("skipped" if permanent else "failed", error[:2000], path_lower))

    def summary(self) -> dict:
        out = {}
        for r in self._all("SELECT kind, status, COUNT(*) AS n, COALESCE(SUM(size),0) AS b "
                           "FROM items GROUP BY kind, status"):
            out[(r["kind"], r["status"])] = (int(r["n"]), int(r["b"]))
        return out

    def problems(self, limit: int = 50):
        return self._all(
            "SELECT path_display, status, error FROM items WHERE status IN ('failed','skipped') "
            "ORDER BY path_lower LIMIT ?", (limit,))

    def undownloadable_count(self) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM items "
                             "WHERE kind='file' AND downloadable=0 AND exportable=0")["n"])
