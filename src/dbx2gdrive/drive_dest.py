"""Creates folders and uploads files into Google Drive (My Drive or a shared drive)."""

import json
import logging
import mimetypes
import threading
from typing import Optional

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .models import Downloaded
from .retry import FatalError, PermanentError

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
RESUMABLE_THRESHOLD = 5 * 1024 * 1024
CHUNK_SIZE = 32 * 1024 * 1024  # must be a multiple of 256 KiB
FATAL_REASONS = {
    "storageQuotaExceeded": "Google Drive storage is full.",
    "teamDriveFileLimitExceeded": "The shared drive has hit its item limit.",
    "uploadLimitExceeded": "Google's daily upload limit (~750 GB/day) was hit. Re-run in 24 hours to continue.",
    "dailyLimitExceeded": "Google Drive API daily quota exhausted. Re-run tomorrow to continue.",
}
REAUTH_MESSAGE = ("Google sign-in has expired or was revoked (apps in 'Testing' status expire after 7 days). "
                  "Run `dbx2gdrive auth google --client-secret <FILE>`, then re-run migrate to resume.")
RETRYABLE_REASONS = {"userRateLimitExceeded", "rateLimitExceeded", "backendError", "internalError"}


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _reason(exc: HttpError) -> str:
    try:
        return json.loads(exc.content)["error"]["errors"][0]["reason"]
    except Exception:
        return ""


class RetryableHttpError(Exception):
    def __init__(self, exc: HttpError):
        super().__init__(str(exc))
        ra = exc.resp.get("retry-after") if exc.resp is not None else None
        self.retry_after = float(ra) if ra and str(ra).isdigit() else None


def _translate(exc: HttpError) -> Exception:
    reason = _reason(exc)
    if reason in FATAL_REASONS:
        return FatalError(f"{FATAL_REASONS[reason]} ({reason})")
    status = exc.resp.status if exc.resp is not None else 0
    if status in (429, 500, 502, 503, 504) or reason in RETRYABLE_REASONS:
        return RetryableHttpError(exc)
    if status == 401:
        return exc  # transient token refresh problems are retried by the caller
    if status in (400, 403, 404):
        return PermanentError(f"Drive rejected the request: {reason or status}: {exc}")
    return exc


class DriveDest:
    def __init__(self, credentials, shared_drive_id: Optional[str] = None, http_factory=None):
        self._creds = credentials
        self._http_factory = http_factory  # tests inject mock transports
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

    def upload(self, dl: Downloaded, parent_id: str, modified: Optional[str],
               existing_id: Optional[str] = None, adopt_existing: bool = False) -> str:
        """Upload dl into parent_id. Returns the Drive file id. Verifies the MD5 Drive reports."""
        if existing_id is None and adopt_existing:
            # A previous attempt may have finished on Google's side without us recording it.
            for f in self.find_child(parent_id, dl.name, folder=False):
                if f.get("md5Checksum") == dl.md5:
                    return f["id"]

        mime = mimetypes.guess_type(dl.name)[0] or "application/octet-stream"
        body = {"name": dl.name}
        if modified:
            body["modifiedTime"] = modified
        media = None
        if dl.size > 0:
            media = MediaFileUpload(dl.local_path, mimetype=mime,
                                    resumable=dl.size > RESUMABLE_THRESHOLD, chunksize=CHUNK_SIZE)
        else:
            body["mimeType"] = mime

        files = self.svc.files()
        if existing_id:
            req = files.update(fileId=existing_id, body=body, media_body=media,
                               fields="id,md5Checksum,size", supportsAllDrives=True)
        else:
            body["parents"] = [parent_id]
            req = files.create(body=body, media_body=media,
                               fields="id,md5Checksum,size", supportsAllDrives=True)
        try:
            if media is not None and media.resumable():
                resp = None
                while resp is None:
                    _, resp = req.next_chunk(num_retries=8)
            else:
                resp = req.execute(num_retries=5)
        except HttpError as exc:
            raise _translate(exc) from exc
        except RefreshError as exc:
            raise FatalError(REAUTH_MESSAGE) from exc

        if dl.size > 0 and resp.get("md5Checksum") and resp["md5Checksum"] != dl.md5:
            if not existing_id:
                self._delete_quietly(resp["id"])
            raise IOError(f"Drive MD5 mismatch after upload ({resp['md5Checksum']} != {dl.md5})")
        if int(resp.get("size", dl.size)) != dl.size:
            if not existing_id:
                self._delete_quietly(resp["id"])
            raise IOError(f"Drive size mismatch after upload ({resp.get('size')} != {dl.size})")
        return resp["id"]

    def _delete_quietly(self, file_id: str) -> None:
        try:
            self.svc.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except Exception as exc:  # pragma: no cover
            log.warning("could not remove bad upload %s: %s", file_id, exc)
