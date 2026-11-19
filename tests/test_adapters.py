"""Exercise the real Dropbox and Drive adapters against mocked SDK/HTTP layers."""

import datetime
import hashlib
import json
import os

import pytest
from dropbox import files as F
from dropbox.exceptions import ApiError
from googleapiclient.http import HttpMockSequence

from dbx2gdrive import drive_dest
from dbx2gdrive.drive_dest import DriveDest
from dbx2gdrive.dropbox_source import DropboxSource
from dbx2gdrive.hashing import DropboxContentHasher
from dbx2gdrive.models import Downloaded
from dbx2gdrive.retry import FatalError, PermanentError


def chash(b):
    h = DropboxContentHasher(); h.update(b); return h.hexdigest()


class Resp:
    def __init__(self, data): self.data = data
    def iter_content(self, n): return [self.data[i:i + n] for i in range(0, len(self.data), n)] or [b""]
    def __enter__(self): return self
    def __exit__(self, *a): pass


class FakeDbx:
    def __init__(self, data):
        self.data = data
    def files_list_folder(self, path, **kw):
        assert kw["recursive"] and kw["include_non_downloadable_files"]
        md = F.FileMetadata(name="a.txt", id="id:1", path_lower="/x/a.txt", path_display="/X/a.txt",
                            size=len(self.data), rev="015abcdef", content_hash=chash(self.data),
                            client_modified=datetime.datetime(2021, 5, 6, 7, 8, 9),
                            server_modified=datetime.datetime(2021, 5, 6, 7, 8, 9), is_downloadable=True)
        return F.ListFolderResult(entries=[F.FolderMetadata(name="X", id="id:0", path_lower="/x", path_display="/X"),
                                           md, F.DeletedMetadata(name="gone", path_lower="/gone")],
                                  cursor="c1", has_more=True)
    def files_list_folder_continue(self, cursor):
        paper = F.FileMetadata(name="Doc.paper", id="id:2", path_lower="/doc.paper", path_display="/Doc.paper",
                               size=0, rev="016abcdef", client_modified=datetime.datetime(2021, 1, 1),
                               server_modified=datetime.datetime(2021, 1, 1), is_downloadable=False,
                               export_info=F.ExportInfo(export_as="md"))
        return F.ListFolderResult(entries=[paper], cursor="c2", has_more=False)
    def files_download(self, path):
        if path == "id:missing":
            raise ApiError("req", F.DownloadError.path(F.LookupError.not_found), "msg", "en")
        if path == "id:restricted":
            raise ApiError("req", F.DownloadError.path(F.LookupError.restricted_content), "msg", "en")
        return None, Resp(self.data)
    def files_export(self, path):
        meta = F.ExportMetadata(name="Doc.md", size=5)
        return F.ExportResult(export_metadata=meta, file_metadata=None), Resp(b"# doc")


@pytest.fixture
def src(tmp_path):
    data = os.urandom(5 * 1024 * 1024 + 3)
    s = DropboxSource(lambda: FakeDbx(data), temp_dir=str(tmp_path), include_team_space=False)
    s.data = data
    return s


def test_listing_and_download(src, tmp_path):
    entries = list(src.list_all("/"))
    assert [e.kind for e in entries] == ["folder", "file", "file"]
    f, paper = entries[1], entries[2]
    assert f.client_modified == "2021-05-06T07:08:09.000Z" and f.parent_lower == "/x"
    dl = src.download(f)
    assert open(dl.local_path, "rb").read() == src.data
    assert dl.md5 == hashlib.md5(src.data).hexdigest()
    # Paper docs are exported under the exported name.
    assert not paper.downloadable and paper.exportable
    dl2 = src.download(paper)
    assert dl2.name == "Doc.md" and open(dl2.local_path, "rb").read() == b"# doc"


def test_corrupt_download_detected_and_temp_removed(src, tmp_path):
    f = list(src.list_all(""))[1]
    f.content_hash = "0" * 64
    with pytest.raises(IOError, match="hash mismatch"):
        src.download(f)
    assert os.listdir(tmp_path) == []


def test_permanent_dropbox_errors(src):
    f = list(src.list_all(""))[1]
    for bad in ("id:missing", "id:restricted"):
        f.id = bad
        with pytest.raises(PermanentError):
            src.download(f)


# ---------------------------------------------------------------- Drive
def ok(body): return ({"status": "200"}, json.dumps(body))
def err(status, reason): return ({"status": str(status)}, json.dumps({"error": {"errors": [{"reason": reason}], "code": status, "message": reason}}))


def make_dl(tmp_path, data, name="f.txt"):
    p = tmp_path / "blob"; p.write_bytes(data)
    return Downloaded(str(p), name, len(data), hashlib.md5(data).hexdigest(), chash(data))


def drive(seq):
    return DriveDest(None, http_factory=lambda: HttpMockSequence(seq))


def test_drive_small_upload_verifies_md5(tmp_path):
    dl = make_dl(tmp_path, b"hello")
    d = drive([ok({"id": "F1", "md5Checksum": dl.md5, "size": "5"})])
    assert d.upload(dl, "P", "2021-01-01T00:00:00.000Z") == "F1"


def test_drive_md5_mismatch_deletes_and_raises(tmp_path):
    dl = make_dl(tmp_path, b"hello")
    d = drive([ok({"id": "F1", "md5Checksum": "bad", "size": "5"}), ({"status": "204"}, "")])
    with pytest.raises(IOError, match="MD5"):
        d.upload(dl, "P", None)


def test_drive_resumable_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_dest, "RESUMABLE_THRESHOLD", 10)
    data = os.urandom(1000)
    dl = make_dl(tmp_path, data, "big.bin")
    d = drive([({"status": "200", "location": "https://upload.example/session"}, ""),
               ok({"id": "F2", "md5Checksum": dl.md5, "size": "1000"})])
    assert d.upload(dl, "P", None) == "F2"


def test_drive_adopts_existing_upload(tmp_path):
    dl = make_dl(tmp_path, b"hello")
    d = drive([ok({"files": [{"id": "OLD", "md5Checksum": dl.md5}]})])
    assert d.upload(dl, "P", None, adopt_existing=True) == "OLD"


def test_drive_error_classification(tmp_path, monkeypatch):
    monkeypatch.setattr("googleapiclient.http.time.sleep", lambda s: None)
    dl = make_dl(tmp_path, b"hello")
    with pytest.raises(FatalError, match="storage is full"):
        drive([err(403, "storageQuotaExceeded")]).upload(dl, "P", None)
    with pytest.raises(PermanentError):
        drive([err(404, "notFound")]).upload(dl, "P", None)
    with pytest.raises(drive_dest.RetryableHttpError):
        drive([err(403, "userRateLimitExceeded")] * 6).ensure_folder("x", "P", reuse_existing=False)


def test_drive_folder_reuse_escapes_quotes():
    seq = HttpMockSequence([ok({"files": [{"id": "EXIST"}]})])
    d = DriveDest(None, http_factory=lambda: seq)
    assert d.ensure_folder("Bob's \\ stuff", "P") == "EXIST"


def test_expired_google_signin_is_fatal(tmp_path):
    from google.auth.exceptions import RefreshError

    class Expiring:
        def __call__(self):
            seq = HttpMockSequence([])
            def boom(*a, **k):
                raise RefreshError("invalid_grant: Token has been expired or revoked.")
            seq.request = boom
            return seq

    d = DriveDest(None, http_factory=Expiring())
    with pytest.raises(FatalError, match="auth google"):
        d.ensure_folder("x", "P", reuse_existing=False)
    with pytest.raises(FatalError, match="auth google"):
        d.upload(make_dl(tmp_path, b"hi"), "P", None)
