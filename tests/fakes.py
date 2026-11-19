import hashlib
import os
import tempfile

from dbx2gdrive.hashing import DropboxContentHasher
from dbx2gdrive.models import Downloaded, Entry


def folder(path):
    return Entry("folder", "id:" + path, path.lower(), path, path.rsplit("/", 1)[1])


def file(path, data: bytes, rev="r1"):
    h = DropboxContentHasher()
    h.update(data)
    e = Entry("file", "id:" + path, path.lower(), path, path.rsplit("/", 1)[1], size=len(data), rev=rev,
              content_hash=h.hexdigest(), client_modified="2020-01-02T03:04:05.000Z")
    e.data = data
    return e


class FakeSource:
    def __init__(self, entries):
        self.entries = entries
        self.fail = {}  # id -> list of exceptions to raise on successive downloads

    def list_all(self, root=""):
        root = root.lower()
        return [e for e in self.entries if e.path_lower == root or e.path_lower.startswith(root + "/")]

    def download(self, entry):
        errs = self.fail.get(entry.id)
        if errs:
            raise errs.pop(0)
        data = next(e for e in self.entries if e.id == entry.id).data
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        h = DropboxContentHasher()
        h.update(data)
        return Downloaded(path, entry.name, len(data), hashlib.md5(data).hexdigest(), h.hexdigest())


class FakeDrive:
    def __init__(self):
        self.items = {}  # id -> dict(name, parent, folder, data, modified)
        self.n = 0
        self.fail_uploads = []

    def _new(self, **kw):
        self.n += 1
        fid = f"d{self.n}"
        self.items[fid] = kw
        return fid

    def ensure_folder(self, name, parent_id, reuse_existing=True):
        for fid, it in self.items.items():
            if it["folder"] and it["parent"] == parent_id and it["name"] == name:
                return fid
        return self._new(name=name, parent=parent_id, folder=True)

    def upload(self, dl, parent_id, modified, existing_id=None, adopt_existing=False):
        if self.fail_uploads:
            raise self.fail_uploads.pop(0)
        assert os.path.exists(dl.local_path)
        data = open(dl.local_path, "rb").read()
        if existing_id:
            self.items[existing_id].update(data=data, modified=modified)
            return existing_id
        return self._new(name=dl.name, parent=parent_id, folder=False, data=data, modified=modified)

    def tree(self, root):
        """Return {relative/path: data-or-None} for everything under root."""
        out = {}
        def walk(pid, prefix):
            for fid, it in self.items.items():
                if it["parent"] == pid:
                    p = f"{prefix}/{it['name']}"
                    if it["folder"]:
                        out[p] = None
                        walk(fid, p)
                    else:
                        assert p not in out, f"duplicate {p}"
                        out[p] = it["data"]
        walk(root, "")
        return out
