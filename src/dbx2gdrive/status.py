"""A plain-text status report kept inside the destination folder in Google Drive, so progress
can be checked from Drive itself (no server login needed)."""

import logging
import threading
import time
from datetime import datetime, timezone

from .migrator import human
from .retry import PermanentError

log = logging.getLogger(__name__)

STATUS_NAME = "_Migration status.txt"
DAILY_LIMIT = 750 * 1024 ** 3


def _n(x: int) -> str:
    return f"{x:,}"


def _pct(a, b) -> str:
    return f"{(100 * a / b):.0f}%" if b else "-"


def _dur(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m:02d}m" if h else f"{m}m"


class StatusFile:
    def __init__(self, dst, state, root_id: str, source_label: str = "", dest_label: str = "",
                 interval: float = 300):
        self.dst, self.state, self.root_id = dst, state, root_id
        self.source_label, self.dest_label = source_label, dest_label
        self.interval = interval
        self.phase, self.message = "starting", ""
        self._t0 = time.monotonic()
        self._bytes0 = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------
    def set_phase(self, phase: str, message: str = "", write: bool = True) -> None:
        self.phase, self.message = phase, message
        if write:
            self.write_now()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="status-file")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.write_now()

    # -- content --------------------------------------------------------------
    def render(self) -> str:
        s = self.state.summary()

        def get(kind, status):
            return s.get((kind, status), (0, 0))

        files_total = sum(v[0] for (k, _), v in s.items() if k == "file")
        bytes_total = sum(v[1] for (k, _), v in s.items() if k == "file")
        folders_total = sum(v[0] for (k, _), v in s.items() if k == "folder")
        done_n, done_b = get("file", "done")
        failed_n = get("file", "failed")[0] + get("folder", "failed")[0]
        skipped_n = get("file", "skipped")[0]
        remaining_n = files_total - done_n - skipped_n
        remaining_b = bytes_total - done_b - get("file", "skipped")[1]

        if self._bytes0 is None:
            self._bytes0 = done_b
        elapsed = time.monotonic() - self._t0
        rate = (done_b - self._bytes0) / elapsed if elapsed > 60 else 0

        if self.phase == "done":
            if skipped_n or failed_n:
                headline = (f"FINISHED - {_n(done_n)} of {_n(files_total)} files copied and verified. "
                            f"{_n(skipped_n + failed_n)} could not be copied (listed below).")
            else:
                headline = (f"COMPLETE - all {_n(files_total)} files ({human(bytes_total)}) are in Google Drive "
                            "and verified.")
        elif self.phase == "scanning":
            headline = f"Listing your Dropbox... {_n(files_total + folders_total)} items found so far."
        elif self.phase == "folders":
            headline = "Creating the folder structure in Google Drive."
        elif self.phase == "copying":
            headline = "Copying files."
        else:
            headline = self.message or self.phase

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "DROPBOX -> GOOGLE DRIVE MIGRATION STATUS",
            f"Last updated: {now}   (refreshes every {int(self.interval // 60)} minutes while running)",
            "",
            f"STATUS: {headline}",
        ]
        if self.message and self.phase not in ("done", "scanning", "folders", "copying", "starting"):
            pass  # message already used as the headline
        elif self.message:
            lines.append(self.message)
        lines.append("")
        if self.source_label or self.dest_label:
            lines += [f"From: Dropbox {self.source_label}", f"To:   Google Drive {self.dest_label}", ""]
        lines += [
            f"Files copied:   {_n(done_n)} of {_n(files_total)}  ({_pct(done_n, files_total)})",
            f"Data copied:    {human(done_b)} of {human(bytes_total)}  ({_pct(done_b, bytes_total)})",
            f"Folders:        {_n(get('folder', 'done')[0])} of {_n(folders_total)} created",
            f"Still to copy:  {_n(max(remaining_n, 0))} files ({human(max(remaining_b, 0))})",
        ]
        if failed_n:
            lines.append(f"Failed so far:  {_n(failed_n)} (retried automatically)")
        if skipped_n:
            lines.append(f"Cannot copy:    {_n(skipped_n)} (Dropbox won't release them; listed below)")
        if self.phase == "copying" and rate > 0 and remaining_b > 0:
            eta = remaining_b / rate
            days_by_limit = remaining_b / DAILY_LIMIT
            note = ""
            if days_by_limit > eta / 86400:
                eta = days_by_limit * 86400
                note = " (limited by Google's ~750 GB/day upload cap)"
            lines.append(f"Speed:          {human(rate)}/s   Estimated time left: about {_dur(eta)}{note}")
        lines += ["", "Every file is checked against Dropbox's checksum while downloading and against",
                  "Google Drive's checksum after uploading. A file only counts as copied if both match."]

        problems = self.state.problems(limit=200)
        if problems:
            lines += ["", f"FILES THAT COULD NOT BE COPIED (first {len(problems)}):"]
            for p in problems:
                tag = "will retry" if p["status"] == "failed" else "cannot copy"
                lines.append(f"  [{tag}] {p['path_display']}")
                if p["error"]:
                    lines.append(f"      {p['error'][:300]}")
        return "\n".join(lines) + "\n"

    def write_now(self) -> None:
        with self._lock:
            try:
                text = self.render()
                fid = self.state.get_meta("status_file_id")
                try:
                    fid = self.dst.write_text(STATUS_NAME, self.root_id, text, file_id=fid)
                except PermanentError:  # file was deleted from Drive; make a new one
                    fid = self.dst.write_text(STATUS_NAME, self.root_id, text, file_id=None)
                self.state.set_meta("status_file_id", fid)
            except Exception as exc:  # never let status reporting break the migration
                log.warning("Could not update the status file in Google Drive: %s", exc)
