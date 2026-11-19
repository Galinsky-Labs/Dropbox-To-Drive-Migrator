import hashlib
import os

from dbx2gdrive.hashing import DROPBOX_BLOCK_SIZE, DropboxContentHasher


def reference(data: bytes) -> str:
    blocks = [data[i:i + DROPBOX_BLOCK_SIZE] for i in range(0, len(data), DROPBOX_BLOCK_SIZE)]
    return hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in blocks)).hexdigest()


def test_matches_reference_for_various_sizes_and_chunkings():
    for size in (0, 1, DROPBOX_BLOCK_SIZE - 1, DROPBOX_BLOCK_SIZE, DROPBOX_BLOCK_SIZE + 1, 2 * DROPBOX_BLOCK_SIZE + 77):
        data = os.urandom(size)
        for chunk in (1000, 1024 * 1024, 5 * 1024 * 1024):
            h = DropboxContentHasher()
            for i in range(0, len(data), chunk):
                h.update(data[i:i + chunk])
            assert h.hexdigest() == reference(data), (size, chunk)


def test_empty_file_hash():
    assert DropboxContentHasher().hexdigest() == hashlib.sha256(b"").hexdigest()
