"""Lists the Dropbox tree and streams file contents (no local disk is used)."""

import logging
import threading
from typing import Iterator

import dropbox
from dropbox import files as dbx_files
from dropbox.common import PathRoot

from .models import Entry
from .retry import PermanentError
from .stream import SourceStream

log = logging.getLogger(__name__)
CHUNK = 1024 * 1024


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z") if dt else None


class DropboxSource:
    def __init__(self, client_factory, include_team_space: bool = True):
        self._factory = client_factory
        self._local = threading.local()
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

    def open(self, entry: Entry) -> SourceStream:
        """Start streaming a file's bytes from Dropbox. Nothing touches local disk."""
        try:
            if entry.downloadable:
                _, resp = self.client.files_download(entry.id)
                name, size, expected_hash = entry.name, entry.size, entry.content_hash
            elif entry.exportable:
                # Paper docs and other cloud-only files: export to a regular file format.
                result, resp = self.client.files_export(entry.id)
                name, size, expected_hash = result.export_metadata.name, None, None
            else:
                raise PermanentError("file is not downloadable and has no export format")
        except dropbox.exceptions.ApiError as exc:
            raise self._classify(exc) from exc
        return SourceStream(resp.iter_content(CHUNK), name, size, expected_hash, closer=resp.close)

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
