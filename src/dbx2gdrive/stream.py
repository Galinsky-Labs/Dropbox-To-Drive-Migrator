"""A verified, read-once byte stream from Dropbox. Nothing is written to disk."""

import hashlib
from typing import Iterable, Optional

from .hashing import DropboxContentHasher


class SourceStream:
    """Wraps an iterator of byte chunks. Hashes everything that passes through and, at the
    moment end-of-file is reached, verifies size and Dropbox content_hash, raising IOError on a
    mismatch. Uploaders check at_eof() before sending their final chunk, so a corrupt or
    truncated download aborts the upload before Google ever finalizes the file."""

    def __init__(self, chunks: Iterable[bytes], name: str, size: Optional[int],
                 expected_hash: Optional[str] = None, closer=None):
        self._it = iter(chunks)
        self.name = name
        self.size = size  # None when unknown in advance (exported docs)
        self._expected_hash = expected_hash
        self._closer = closer
        self._buf = bytearray()
        self._eof = False
        self.bytes_read = 0
        self._md5 = hashlib.md5()
        self._ch = DropboxContentHasher()

    def _pull(self) -> bool:
        if self._eof:
            return False
        for chunk in self._it:
            if chunk:
                self._buf += chunk
                return True
        self._eof = True
        self._verify()
        return False

    def _verify(self) -> None:
        if self.size is not None and self.bytes_read + len(self._buf) != self.size:
            raise IOError(f"download truncated: got {self.bytes_read + len(self._buf)} of {self.size} bytes")

    def read(self, n: int) -> bytes:
        while len(self._buf) < n and self._pull():
            pass
        out = bytes(self._buf[:n])
        del self._buf[:n]
        self.bytes_read += len(out)
        self._md5.update(out)
        self._ch.update(out)
        if self._eof and not self._buf and self._expected_hash:
            got = self._ch.hexdigest()
            if got != self._expected_hash:
                raise IOError(f"Dropbox content hash mismatch (got {got}, expected {self._expected_hash})")
        return out

    def at_eof(self) -> bool:
        if self._buf:
            return False
        return not self._pull()

    @property
    def md5(self) -> str:
        return self._md5.hexdigest()

    @property
    def content_hash(self) -> str:
        return self._ch.hexdigest()

    def close(self) -> None:
        if self._closer:
            try:
                self._closer()
            except Exception:
                pass
            self._closer = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
