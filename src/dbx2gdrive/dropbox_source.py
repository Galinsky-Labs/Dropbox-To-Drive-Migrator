"""Reads the Dropbox tree and downloads files to local temp files."""

import hashlib
import logging
import os
import tempfile
import threading
from typing import Iterator

import dropbox
from dropbox import files as dbx_files
from dropbox.common import PathRoot

from .hashing import DropboxContentHasher
from .models import Downloaded, Entry
from .retry import PermanentError

log = logging.getLogger(__name__)
CHUNK = 1024 * 1024


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z") if dt else None


class DropboxSource:
    def __init__(self, client_factory, temp_dir: str = None, include_team_space: bool = True):
        self._factory = client_factory
        self._local = threading.local()
        self.temp_dir = temp_dir
        self._path_root = None
        if include_team_space:
            # Business accounts with a team space: list from the root namespace so team
            # folders are included, not just the member's private folder.
            root = client_factory().users_get_current_account().root_info
            if root.root_namespace_id != root.home_namespace_id:
                log.info("Team space detected; migrating from the team root namespace")
                self._path_root = PathRoot.root(root.root_namespace_id)

    @property
    def client(self) -> dropbox.Dropbox:
        c = getattr(self._local, "client", None)
        if c is None:
            c = self._factory()
            if self._path_root is not None:
                c = c.with_path_root(self._path_root)
            self._local.client = c
        return c

    def account_label(self) -> str:
        acct = self.client.users_get_current_account()
        return f"{acct.name.display_name} <{acct.email}>"

    def list_all(self, root: str = "") -> Iterator[Entry]:
        root = "" if root in ("", "/") else root
        res = self.client.files_list_folder(
            root, recursive=True, include_mounted_folders=True,
            include_non_downloadable_files=True, limit=2000,
        )
        while True:
            for md in res.entries:
                entry = self._to_entry(md)
                if entry is not None:
                    yield entry
            if not res.has_more:
                break
            res = self.client.files_list_folder_continue(res.cursor)

    @staticmethod
    def _to_entry(md):
        if isinstance(md, dbx_files.FolderMetadata):
            return Entry("folder", md.id, md.path_lower, md.path_display, md.name)
        if isinstance(md, dbx_files.FileMetadata):
            return Entry(
                "file", md.id, md.path_lower, md.path_display, md.name,
                size=md.size, rev=md.rev, content_hash=md.content_hash,
                client_modified=_iso(md.client_modified),
                downloadable=md.is_downloadable is not False,
                exportable=md.export_info is not None,
            )
        return None  # DeletedMetadata

    def download(self, entry: Entry) -> Downloaded:
        try:
            if entry.downloadable:
                _, resp = self.client.files_download(entry.id)
                name, expected_hash = entry.name, entry.content_hash
            elif entry.exportable:
                # Paper docs and other cloud-only files: export to a regular file format.
                result, resp = self.client.files_export(entry.id)
                name, expected_hash = result.export_metadata.name, None
            else:
                raise PermanentError("file is not downloadable and has no export format")
        except dropbox.exceptions.ApiError as exc:
            raise self._classify(exc) from exc

        fd, local_path = tempfile.mkstemp(prefix="dbx2gdrive-", dir=self.temp_dir)
        md5, ch, size = hashlib.md5(), DropboxContentHasher(), 0
        try:
            with os.fdopen(fd, "wb") as out, resp:
                for chunk in resp.iter_content(CHUNK):
                    out.write(chunk)
                    md5.update(chunk)
                    ch.update(chunk)
                    size += len(chunk)
            digest = ch.hexdigest()
            if expected_hash and digest != expected_hash:
                raise IOError(f"Dropbox content hash mismatch (got {digest}, expected {expected_hash})")
            if entry.downloadable and size != entry.size:
                raise IOError(f"size mismatch (got {size}, expected {entry.size})")
        except BaseException:
            os.unlink(local_path)
            raise
        return Downloaded(local_path, name, size, md5.hexdigest(), digest)

    @staticmethod
    def _classify(exc):
        err = exc.error
        try:
            if err.is_path():
                p = err.get_path()
                if p.is_not_found():
                    return PermanentError("file no longer exists in Dropbox")
                if p.is_restricted_content():
                    return PermanentError("Dropbox restricts downloading this file (e.g. copyright/DMCA)")
            if hasattr(err, "is_unsupported_file") and err.is_unsupported_file():
                return PermanentError("Dropbox does not support downloading this file type")
            if hasattr(err, "is_non_exportable") and err.is_non_exportable():
                return PermanentError("Dropbox cannot export this file")
        except Exception:
            pass
        return exc
