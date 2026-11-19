"""Creates folders and uploads files into Google Drive (My Drive or a shared drive)."""

import json
import logging
import mimetypes
import random
import threading
import time
from typing import Optional

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .retry import FatalError, PermanentError

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
CHUNK_SIZE = 16 * 1024 * 1024  # held in memory per worker; must be a multiple of 256 KiB
HTTP_TIMEOUT = 300
FATAL_REASONS = {
    "storageQuotaExceeded": "Google Drive storage is full.",
    "teamDriveFileLimitExceeded": "The shared drive has hit its item limit.",
    "uploadLimitExceeded": "Google's daily upload limit (~750 GB/day) was hit. Re-run in 24 hours to continue.",
    "dailyLimitExceeded": "Google Drive API daily quota exhausted. Re-run tomorrow to continue.",
}
REAUTH_MESSAGE = ("Google sign-in has expired or was revoked (apps in 'Testing' status expire after 7 days). "
                  "Run `dbx2gdrive auth google --client-secret <FILE>`, then re-run migrate to resume.")
# Limits that reset by themselves: a long-running service waits and resumes automatically.
WAIT_REASONS = {"uploadLimitExceeded": 3600, "dailyLimitExceeded": 3600}
RETRYABLE_REASONS = {"userRateLimitExceeded", "rateLimitExceeded", "backendError", "internalError"}


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _reason(exc: HttpError) -> str:
    try:
        return json.loads(exc.content)["error"]["errors"][0]["reason"]
    except Exception:
        return ""


class RetryableHttpError(Exception):
    def __init__(self, text, retry_after=None):
        super().__init__(text)
        self.retry_after = float(retry_after) if retry_after and str(retry_after).isdigit() else None


def _raise_for(r) -> None:
    if r.status_code < 400:
        return
    try:
        reason = r.json()["error"]["errors"][0]["reason"]
    except Exception:
        reason = ""
    raise _classify(r.status_code, reason, r.headers.get("Retry-After"), r.text[:500])


def _translate(exc: HttpError) -> Exception:
    status = exc.resp.status if exc.resp is not None else 0
    ra = exc.resp.get("retry-after") if exc.resp is not None else None
    return _classify(status, _reason(exc), ra, str(exc), original=exc)


def _classify(status, reason, retry_after, text, original=None) -> Exception:
    if reason in FATAL_REASONS:
        return FatalError(f"{FATAL_REASONS[reason]} ({reason})", retry_in=WAIT_REASONS.get(reason))
    if status in (429, 500, 502, 503, 504) or reason in RETRYABLE_REASONS:
        return RetryableHttpError(text, retry_after)
    if status in (400, 403, 404):
        return PermanentError(f"Drive rejected the request: {reason or status}: {text}")
    return original or IOError(f"Drive HTTP {status}: {text}")


class DriveDest:
    def __init__(self, credentials, shared_drive_id: Optional[str] = None, http_factory=None,
                 session_factory=None):
        self._creds = credentials
        self._http_factory = http_factory  # tests inject mock transports
        self._session_factory = session_factory
        self._local = threading.local()
        self.shared_drive_id = shared_drive_id

    @property
    def svc(self):
        # httplib2 is not thread-safe, so each worker thread gets its own client.
        s = getattr(self._local, "svc", None)
        if s is None:
            if self._http_factory:
                s = build("drive", "v3", http=self._http_factory(), cache_discovery=False)
            else:
                s = build("drive", "v3", credentials=self._creds, cache_discovery=False)
            self._local.svc = s
        return s

    def _exec(self, request):
        try:
            return request.execute(num_retries=5)
        except HttpError as exc:
            raise _translate(exc) from exc
        except RefreshError as exc:
            raise FatalError(REAUTH_MESSAGE) from exc

    def _list_kwargs(self):
        kw = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}
        if self.shared_drive_id:
            kw.update(corpora="drive", driveId=self.shared_drive_id)
        return kw

    def account_label(self) -> str:
        about = self._exec(self.svc.about().get(fields="user(displayName,emailAddress)"))
        return f"{about['user']['displayName']} <{about['user']['emailAddress']}>"

    def check_folder(self, folder_id: str) -> str:
        meta = self._exec(self.svc.files().get(fileId=folder_id, fields="id,name,mimeType", supportsAllDrives=True))
        if meta["mimeType"] != FOLDER_MIME:
            raise SystemExit(f"Drive item {folder_id} ({meta['name']}) is not a folder")
        return meta["name"]

    def find_child(self, parent_id: str, name: str, folder: bool):
        mime = f"mimeType {'=' if folder else '!='} '{FOLDER_MIME}'"
        q = f"'{_q(parent_id)}' in parents and name = '{_q(name)}' and {mime} and trashed = false"
        res = self._exec(self.svc.files().list(
            q=q, fields="files(id,name,md5Checksum,size)", pageSize=10, **self._list_kwargs()))
        return res.get("files", [])

    def ensure_folder(self, name: str, parent_id: str, reuse_existing: bool = True) -> str:
        if reuse_existing:
            found = self.find_child(parent_id, name, folder=True)
            if found:
                return found[0]["id"]
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        return self._exec(self.svc.files().create(body=body, fields="id", supportsAllDrives=True))["id"]

    @property
    def http(self):
        """Per-thread authorized requests session, used for the streaming upload protocol."""
        h = getattr(self._local, "http", None)
        if h is None:
            if self._session_factory:
                h = self._session_factory()
            else:
                from google.auth.transport.requests import AuthorizedSession
                h = AuthorizedSession(self._creds)
            self._local.http = h
        return h

    def find_by_rev(self, parent_id: str, name: str, rev: str):
        """A file a previous attempt finished uploading but never got recorded."""
        q = (f"'{_q(parent_id)}' in parents and name = '{_q(name)}' and trashed = false and "
             f"appProperties has {{ key='dbx_rev' and value='{_q(rev)}' }}")
        res = self._exec(self.svc.files().list(q=q, fields="files(id)", pageSize=1, **self._list_kwargs()))
        files = res.get("files", [])
        return files[0]["id"] if files else None

    def upload(self, stream, parent_id: str, modified: Optional[str], rev: Optional[str] = None,
               existing_id: Optional[str] = None, adopt_existing: bool = False) -> str:
        """Stream a file into Drive with the resumable-upload protocol, chunk by chunk from memory.
        Returns the Drive file id. Verifies size and the MD5 Drive computes."""
        if existing_id is None and adopt_existing and rev:
            found = self.find_by_rev(parent_id, stream.name, rev)
            if found:
                return found

        mime = mimetypes.guess_type(stream.name)[0] or "application/octet-stream"
        body = {"name": stream.name}
        if modified:
            body["modifiedTime"] = modified
        if rev:
            body["appProperties"] = {"dbx_rev": rev}
        if not existing_id:
            body["parents"] = [parent_id]
        try:
            session_url = self._start_session(body, mime, stream.size, existing_id)
            resp = self._send_chunks(session_url, stream)
        except RefreshError as exc:
            raise FatalError(REAUTH_MESSAGE) from exc

        size = stream.bytes_read
        if resp.get("md5Checksum") and resp["md5Checksum"] != stream.md5 or int(resp.get("size", size)) != size:
            if not existing_id:
                self._delete_quietly(resp["id"])
            raise IOError(f"Drive checksum mismatch after upload ({resp.get('md5Checksum')} != {stream.md5})")
        return resp["id"]

    def _start_session(self, body, mime, size, existing_id) -> str:
        params = {"uploadType": "resumable", "supportsAllDrives": "true", "fields": "id,md5Checksum,size"}
        headers = {"X-Upload-Content-Type": mime, "Content-Type": "application/json; charset=UTF-8"}
        if size is not None:
            headers["X-Upload-Content-Length"] = str(size)
        if existing_id:
            r = self.http.patch(f"{UPLOAD_URL}/{existing_id}", params=params, json=body, headers=headers,
                                timeout=HTTP_TIMEOUT)
        else:
            r = self.http.post(UPLOAD_URL, params=params, json=body, headers=headers, timeout=HTTP_TIMEOUT)
        _raise_for(r)
        return r.headers["Location"]

    def _send_chunks(self, url: str, stream) -> dict:
        """Send the stream in CHUNK_SIZE pieces. Only the current piece is held in memory; if a
        request fails we ask Google how much it has and resend the rest of that piece."""
        offset = 0  # stream position of the start of `chunk`
        while True:
            chunk = stream.read(CHUNK_SIZE)
            final = stream.at_eof()  # raises if the Dropbox data failed verification -> never finalized
            total = str(offset + len(chunk)) if final else "*"
            sent, failures = 0, 0
            while True:
                part = chunk[sent:]
                start = offset + sent
                rng = f"bytes {start}-{start + len(part) - 1}/{total}" if part else f"bytes */{total}"
                status, result = self._put(url, part, rng)
                if status == "done":
                    return result
                if status == "progress":
                    sent = result - offset
                    if sent < 0:
                        raise IOError("upload session lost data; restarting this file")
                    if sent >= len(chunk) and not final:
                        break  # this piece is fully stored; read the next one
                    if result > start:
                        failures = 0
                        continue  # partial progress: send the remainder straight away
                failures += 1
                if failures > 8:
                    raise IOError(f"upload kept failing at byte {start}: {result}")
                time.sleep(min(60, 2 ** failures) + random.random())
                status, result = self._put(url, b"", f"bytes */{total}")  # how much does Google have?
                if status == "done":
                    return result
                if status == "progress":
                    sent = result - offset
                    if sent < 0:
                        raise IOError("upload session lost data; restarting this file")
                    if sent >= len(chunk) and not final:
                        break
            offset += len(chunk)

    def _put(self, url: str, data: bytes, content_range: str):
        """One PUT to the upload session. Returns ("done", file_json), ("progress", committed_bytes)
        or ("error", description) for a retryable failure. Raises on permanent errors."""
        try:
            r = self.http.put(url, data=data, headers={"Content-Range": content_range}, timeout=HTTP_TIMEOUT)
        except RefreshError:
            raise
        except Exception as exc:  # connection reset, timeout, ...
            return "error", f"{type(exc).__name__}: {exc}"
        if r.status_code in (200, 201):
            return "done", r.json()
        if r.status_code == 308:
            rng = r.headers.get("Range")  # "bytes=0-N"; absent means nothing stored yet
            return "progress", int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
        if r.status_code in (404, 410):
            raise IOError("upload session expired; restarting this file")
        if r.status_code in (408, 429, 500, 502, 503, 504):
            return "error", f"HTTP {r.status_code}"
        _raise_for(r)
        return "error", f"HTTP {r.status_code}"

    def _delete_quietly(self, file_id: str) -> None:
        try:
            self.svc.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except Exception as exc:  # pragma: no cover
            log.warning("could not remove bad upload %s: %s", file_id, exc)
