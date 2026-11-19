"""Streaming hashers used to verify every transfer end to end."""

import hashlib

DROPBOX_BLOCK_SIZE = 4 * 1024 * 1024


class DropboxContentHasher:
    """Dropbox's content_hash: SHA-256 over the concatenated SHA-256 of each 4 MiB block.

    See https://www.dropbox.com/developers/reference/content-hash
    """

    def __init__(self):
        self._overall = hashlib.sha256()
        self._block = hashlib.sha256()
        self._block_pos = 0

    def update(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self._block_pos == DROPBOX_BLOCK_SIZE:
                self._overall.update(self._block.digest())
                self._block = hashlib.sha256()
                self._block_pos = 0
            take = min(len(view), DROPBOX_BLOCK_SIZE - self._block_pos)
            self._block.update(view[:take])
            self._block_pos += take
            view = view[take:]

    def hexdigest(self) -> str:
        overall = self._overall.copy()
        if self._block_pos > 0:
            overall.update(self._block.digest())
        return overall.hexdigest()
