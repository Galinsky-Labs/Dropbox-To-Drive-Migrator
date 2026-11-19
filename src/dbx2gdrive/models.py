from dataclasses import dataclass
from typing import Optional


@dataclass
class Entry:
    """One item in the Dropbox tree."""

    kind: str  # "file" or "folder"
    id: str  # Dropbox id ("id:...") - stable across renames
    path_lower: str
    path_display: str
    name: str
    size: int = 0
    rev: Optional[str] = None
    content_hash: Optional[str] = None
    client_modified: Optional[str] = None  # RFC 3339
    downloadable: bool = True
    exportable: bool = False

    @property
    def parent_lower(self) -> str:
        return self.path_lower.rsplit("/", 1)[0]

    @property
    def depth(self) -> int:
        return self.path_lower.count("/")


@dataclass
class Downloaded:
    """A file pulled down from Dropbox into a local temp file."""

    local_path: str
    name: str  # may differ from the Dropbox name for exported files (e.g. Paper -> .md)
    size: int
    md5: str
    content_hash: str
