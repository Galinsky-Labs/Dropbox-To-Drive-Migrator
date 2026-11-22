"""Orchestrates scan -> folders -> files, with concurrency, resume and progress reporting."""

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Optional

from .models import Entry
from .retry import FatalError, PermanentError, with_retries
from .state import State

log = logging.getLogger(__name__)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    return f"{h}h{rem // 60:02d}m" if h else f"{rem // 60}m{rem % 60:02d}s"


@dataclass
class Options:
    source_root: str = ""
    workers: int = 4
    retry_skipped: bool = False
    skip_scan: bool = False
    attempts: int = 5
    progress_interval: float = 30.0


class Migrator:
    def __init__(self, source, dest, state: State, dest_root_id: str, opts: Options, status=None):
        self.src, self.dst, self.state, self.opts = source, dest, state, opts
        self.status = status  # optional StatusFile
        self.dest_root_id = dest_root_id
        self.root_lower = "" if opts.source_root in ("", "/") else opts.source_root.rstrip("/").lower()
        self._stop = threading.Event()
        self._fatal: Optional[BaseException] = None
        self._lock = threading.Lock()
        self.files_done = self.files_failed = self.bytes_done = 0

    # -- phases ---------------------------------------------------------------
    def scan(self) -> int:
        log.info("Scanning Dropbox%s ...", f" folder {self.opts.source_root}" if self.root_lower else "")
        batch, count = [], 0
        for entry in self.src.list_all(self.opts.source_root):
            if entry.path_lower == self.root_lower:
                continue  # the source folder itself maps to the Drive destination folder
            batch.append(entry)
            if len(batch) >= 1000:
                self.state.upsert_entries(batch)
                count += len(batch)
                batch = []
                log.info("  ... %d items listed", count)
        self.state.upsert_entries(batch)
        count += len(batch)
        log.info("Scan complete: %d items", count)
        return count

    def _parent_id(self, parent_lower: str) -> Optional[str]:
        if parent_lower == self.root_lower:
            return self.dest_root_id
        return self.state.drive_id_of(parent_lower)

    def create_folders(self) -> None:
        depths = self.state.folder_depths()
        if not depths:
            return
        log.info("Creating folder tree in Google Drive ...")
        with ThreadPoolExecutor(self.opts.workers) as pool:
            for depth in depths:  # parents before children; siblings in parallel
                rows = self.state.pending_folders_at(depth)
                list(pool.map(self._make_folder, rows))
                if self._fatal:
                    raise self._fatal

    def _make_folder(self, row) -> None:
        if self._stop.is_set():
            return
        try:
            self._make_folder_inner(row)
        except BaseException as exc:
            log.exception("Unexpected error creating folder %s", row["path_display"])
            self.state.mark_error(row["path_lower"], f"{type(exc).__name__}: {exc}", permanent=False)

    def _make_folder_inner(self, row) -> None:
        key = row["path_lower"]
        parent_id = self._parent_id(key.rsplit("/", 1)[0])
        if parent_id is None:
            self.state.mark_error(key, "parent folder was not created", permanent=False)
            return
        try:
            fid = with_retries(lambda: self.dst.ensure_folder(row["name"], parent_id),
                               attempts=self.opts.attempts, what=f"create folder {row['path_display']}")
            self.state.mark_done(key, fid, None)
        except FatalError as exc:
            self._fatal = exc
            self._stop.set()
        except Exception as exc:
            log.error("Folder %s failed: %s", row["path_display"], exc)
            self.state.mark_error(key, str(exc), permanent=isinstance(exc, PermanentError))

    def copy_files(self) -> None:
        keys = self.state.pending_file_keys(include_skipped=self.opts.retry_skipped)
        if not keys:
            log.info("No files left to copy.")
            return
        total_bytes = self.state.pending_file_bytes(include_skipped=self.opts.retry_skipped)
        log.info("Copying %d files (%s) with %d workers ...", len(keys), human(total_bytes), self.opts.workers)
        reporter = threading.Thread(target=self._report, args=(len(keys), total_bytes), daemon=True)
        reporter.start()
        it = iter(keys)
        try:
            with ThreadPoolExecutor(self.opts.workers) as pool:
                running = set()
                while not self._stop.is_set():
                    while len(running) < self.opts.workers * 2:
                        key = next(it, None)
                        if key is None:
                            break
                        running.add(pool.submit(self._copy_one, key))
                    if not running:
                        break
                    _, running = wait(running, return_when=FIRST_COMPLETED)
                for f in running:
                    f.cancel()
        except KeyboardInterrupt:
            self._stop.set()
            log.warning("Interrupted - waiting for in-flight transfers to stop. Re-run to resume.")
            raise
        finally:
            self._stop.set()
            reporter.join(timeout=1)
        if self._fatal:
            raise self._fatal

    def _copy_one(self, key: str) -> None:
        if self._stop.is_set():
            return
        try:
            self._copy_one_inner(key)
        except BaseException as exc:  # never let a worker die silently
            log.exception("Unexpected error copying %s", key)
            try:
                self.state.mark_error(key, f"{type(exc).__name__}: {exc}", permanent=False)
            finally:
                self._count(failed=True)

    def _copy_one_inner(self, key: str) -> None:
        row = self.state.get(key)
        parent_id = self._parent_id(key.rsplit("/", 1)[0])
        if parent_id is None:
            self.state.mark_error(key, "parent folder was not created", permanent=False)
            self._count(failed=True)
            return

        entry = Entry("file", row["dbx_id"], key, row["path_display"], row["name"], row["size"], row["rev"],
                      row["content_hash"], row["client_modified"], bool(row["downloadable"]),
                      bool(row["exportable"]))
        existing_id = row["drive_id"]  # set when the file changed in Dropbox since the last copy
        adopt = row["attempts"] > 0 and not existing_id

        def attempt():
            with self.src.open(entry) as stream:  # streamed Dropbox -> Drive, never written to disk
                drive_id = self.dst.upload(stream, parent_id, entry.client_modified, rev=entry.rev,
                                           existing_id=existing_id, adopt_existing=adopt)
                return drive_id, stream.bytes_read

        try:
            drive_id, size = with_retries(attempt, attempts=self.opts.attempts, what=f"copy {entry.path_display}")
            self.state.mark_done(key, drive_id, entry.rev)
            self._count(nbytes=size)
            log.debug("Copied %s", entry.path_display)
        except FatalError as exc:
            self.state.mark_error(key, str(exc), permanent=False)
            if not self._fatal:
                self._fatal = exc
                log.error("Stopping: %s", exc)
            self._stop.set()
        except Exception as exc:
            permanent = isinstance(exc, PermanentError)
            log.error("%s %s: %s", "Skipped" if permanent else "Failed", entry.path_display, exc)
            self.state.mark_error(key, f"{type(exc).__name__}: {exc}", permanent=permanent)
            self._count(failed=True)

    def _count(self, nbytes: int = 0, failed: bool = False) -> None:
        with self._lock:
            if failed:
                self.files_failed += 1
            else:
                self.files_done += 1
                self.bytes_done += nbytes

    def _report(self, total_files: int, total_bytes: int) -> None:
        start = time.monotonic()
        while not self._stop.wait(self.opts.progress_interval):
            elapsed = time.monotonic() - start
            rate = self.bytes_done / elapsed if elapsed else 0
            remaining = (total_bytes - self.bytes_done) / rate if rate else 0
            log.info("Progress: %d/%d files, %s/%s, %s/s, %d failed, ETA %s",
                     self.files_done, total_files, human(self.bytes_done), human(total_bytes),
                     human(rate), self.files_failed, _duration(remaining))

    def _phase(self, phase: str) -> None:
        if self.status:
            self.status.set_phase(phase)

    def run(self) -> None:
        if not self.opts.skip_scan:
            self._phase("scanning")
            self.scan()
        self._phase("folders")
        self.create_folders()
        self._phase("copying")
        self.copy_files()
