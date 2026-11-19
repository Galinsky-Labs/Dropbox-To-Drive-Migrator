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
from dbx2gdrive.retry import FatalError, PermanentError
from dbx2gdrive.stream import SourceStream


def chash(b):
    h = DropboxContentHasher(); h.update(b); return h.hexdigest()


def drain(stream):
    out = b""
    while True:
        out += stream.read(1 << 20)
        if stream.at_eof():
            return out


class Resp:
    def __init__(self, data): self.data, self.closed = data, False
    def iter_content(self, n): return iter([self.data[i:i + n] for i in range(0, len(self.data), n)])
    def close(self): self.closed = True


class FakeDbx:
    def __init__(self, data):
        self.data = data
        self.responses = []
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
        r = Resp(self.data); self.responses.append(r)
        return None, r
    def files_export(self, path):
        meta = F.ExportMetadata(name="Doc.md", size=5)
        return F.ExportResult(export_metadata=meta, file_metadata=None), Resp(b"# doc")


@pytest.fixture
def src():
    data = os.urandom(5 * 1024 * 1024 + 3)
    client = FakeDbx(data)
    s = DropboxSource(lambda: client, include_team_space=False)
    s.data, s.fake = data, client
    return s


def test_listing_and_streaming_download(src):
    entries = list(src.list_all("/"))
    assert [e.kind for e in entries] == ["folder", "file", "file"]
    f, paper = entries[1], entries[2]
    assert f.client_modified == "2021-05-06T07:08:09.000Z" and f.parent_lower == "/x"
    with src.open(f) as st:
        assert drain(st) == src.data
        assert st.md5 == hashlib.md5(src.data).hexdigest() and st.bytes_read == len(src.data)
    assert src.fake.responses[-1].closed
    # Paper docs are exported under the exported name, size unknown in advance.
    assert not paper.downloadable and paper.exportable
    with src.open(paper) as st:
        assert st.name == "Doc.md" and st.size is None and drain(st) == b"# doc"


def test_corrupt_or_truncated_download_raises_at_eof(src):
    f = list(src.list_all(""))[1]
    f.content_hash = "0" * 64
    with pytest.raises(IOError, match="hash mismatch"):
        drain(src.open(f))
    f = list(src.list_all(""))[1]
    f.size += 1
    with pytest.raises(IOError, match="truncated"):
        drain(src.open(f))


def test_permanent_dropbox_errors(src):
    f = list(src.list_all(""))[1]
    for bad in ("id:missing", "id:restricted"):
        f.id = bad
        with pytest.raises(PermanentError):
            src.open(f)


def test_stream_read_sizes():
    st = SourceStream([b"ab", b"", b"cdefg", b"h"], "n", 8, chash(b"abcdefgh"))
    assert st.read(3) == b"abc" and not st.at_eof()
    assert st.read(10) == b"defgh" and st.at_eof()
    assert st.read(5) == b""


# ---------------------------------------------------------------- Drive: fake resumable server
class R:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""
    def json(self): return self._body


class FakeUploadServer:
    """Implements enough of Google's resumable upload protocol, with injectable faults."""

    def __init__(self, faults=()):
        self.faults = list(faults)  # per data-PUT: None, "drop", "503", ("partial", n)
        self.data = bytearray()
        self.created = []
        self.meta = None
        self.puts = 0

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        assert params["uploadType"] == "resumable"
        self.meta, self.method = json, "create"
        return R(200, headers={"Location": "https://upload/session1"})

    def patch(self, url, params=None, json=None, headers=None, timeout=None):
        self.meta, self.method, self.patched = json, "update", url.rsplit("/", 1)[1]
        return R(200, headers={"Location": "https://upload/session1"})

    def put(self, url, data=b"", headers=None, timeout=None):
        rng = headers["Content-Range"]
        spec, total = rng[len("bytes "):].split("/")
        if spec != "*" and data:
            self.puts += 1
            fault = self.faults.pop(0) if self.faults else None
            start = int(spec.split("-")[0])
            assert start == len(self.data), f"gap/overlap: server has {len(self.data)}, client sent from {start}"
            if fault == "drop":
                raise ConnectionError("connection reset")
            if fault == "lost_response":  # Google stored it, but the reply never arrived
                self.data += data
                raise TimeoutError("read timed out")
            if fault == "503":
                return R(503)
            if isinstance(fault, tuple):
                data = data[:fault[1]]
            self.data += data
        if total != "*" and len(self.data) == int(total):
            fid = getattr(self, "patched", None) or "NEW"
            self.created.append(fid)
            return R(200, {"id": fid, "md5Checksum": hashlib.md5(bytes(self.data)).hexdigest(),
                           "size": str(len(self.data))})
        return R(308, headers={"Range": f"bytes=0-{len(self.data) - 1}"} if self.data else {})


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(drive_dest, "CHUNK_SIZE", 256 * 1024)
    monkeypatch.setattr("dbx2gdrive.drive_dest.time.sleep", lambda s: None)


def mkstream(data, known_size=True):
    pieces = [data[i:i + 100_000] for i in range(0, len(data), 100_000)]
    return SourceStream(pieces, "big.bin", len(data) if known_size else None, chash(data))


@pytest.mark.parametrize("faults", [
    [],
    ["drop"],
    ["503", "503", "drop"],
    [None, ("partial", 1000), None, "drop", ("partial", 5)],
    ["lost_response", None, "lost_response", "lost_response"],
])
@pytest.mark.parametrize("known_size", [True, False])
def test_streaming_upload_survives_faults(small_chunks, faults, known_size):
    data = os.urandom(256 * 1024 * 4 + 777)
    server = FakeUploadServer(faults)
    d = DriveDest(None, session_factory=lambda: server)
    fid = d.upload(mkstream(data, known_size), "PARENT", "2021-01-01T00:00:00.000Z", rev="r1")
    assert fid == "NEW" and bytes(server.data) == data and server.created == ["NEW"]
    assert server.meta == {"name": "big.bin", "modifiedTime": "2021-01-01T00:00:00.000Z",
                           "appProperties": {"dbx_rev": "r1"}, "parents": ["PARENT"]}


def test_streaming_upload_empty_file_and_update(small_chunks):
    server = FakeUploadServer()
    d = DriveDest(None, session_factory=lambda: server)
    assert d.upload(SourceStream([], "e.txt", 0, chash(b"")), "P", None) == "NEW"
    server2 = FakeUploadServer()
    d2 = DriveDest(None, session_factory=lambda: server2)
    assert d2.upload(mkstream(b"v2"), "P", None, existing_id="F9") == "F9"
    assert server2.method == "update" and "parents" not in server2.meta


def test_corrupt_source_never_finalizes_upload(small_chunks):
    data = os.urandom(256 * 1024 * 2 + 10)
    bad = SourceStream([data], "x", len(data), "0" * 64)
    server = FakeUploadServer()
    with pytest.raises(IOError, match="hash mismatch"):
        DriveDest(None, session_factory=lambda: server).upload(bad, "P", None)
    assert server.created == []


def test_upload_permanent_and_fatal_errors(small_chunks):
    class Full(FakeUploadServer):
        def post(self, *a, **k):
            return R(403, {"error": {"errors": [{"reason": "storageQuotaExceeded"}]}})
    with pytest.raises(FatalError, match="storage is full"):
        DriveDest(None, session_factory=Full).upload(mkstream(b"x"), "P", None)

    class Limit(FakeUploadServer):
        def post(self, *a, **k):
            return R(403, {"error": {"errors": [{"reason": "uploadLimitExceeded"}]}})
    with pytest.raises(FatalError) as ei:
        DriveDest(None, session_factory=Limit).upload(mkstream(b"x"), "P", None)
    assert ei.value.retry_in == 3600

    class Gone(FakeUploadServer):
        def put(self, *a, **k):
            return R(404)
    with pytest.raises(IOError, match="session expired"):
        DriveDest(None, session_factory=Gone).upload(mkstream(b"x"), "P", None)


# ---------------------------------------------------------------- Drive: metadata API
def ok(body): return ({"status": "200"}, json.dumps(body))
def err(status, reason): return ({"status": str(status)}, json.dumps({"error": {"errors": [{"reason": reason}], "code": status, "message": reason}}))


def drive(seq):
    return DriveDest(None, http_factory=lambda: HttpMockSequence(seq))


def test_adopts_upload_recorded_by_rev():
    d = drive([ok({"files": [{"id": "OLD"}]})])
    assert d.upload(mkstream(b"hello"), "P", None, rev="r1", adopt_existing=True) == "OLD"


def test_metadata_error_classification(monkeypatch):
    monkeypatch.setattr("googleapiclient.http.time.sleep", lambda s: None)
    with pytest.raises(PermanentError):
        drive([err(404, "notFound")]).ensure_folder("x", "P", reuse_existing=False)
    with pytest.raises(drive_dest.RetryableHttpError):
        drive([err(403, "userRateLimitExceeded")] * 6).ensure_folder("x", "P", reuse_existing=False)


def test_drive_folder_reuse_escapes_quotes():
    seq = HttpMockSequence([ok({"files": [{"id": "EXIST"}]})])
    d = DriveDest(None, http_factory=lambda: seq)
    assert d.ensure_folder("Bob's \\ stuff", "P") == "EXIST"


def test_expired_google_signin_is_fatal(small_chunks):
    from google.auth.exceptions import RefreshError

    def expiring():
        seq = HttpMockSequence([])
        def boom(*a, **k):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        seq.request = boom
        return seq

    class ExpiringSession(FakeUploadServer):
        def post(self, *a, **k):
            raise RefreshError("invalid_grant")

    with pytest.raises(FatalError, match="auth google"):
        DriveDest(None, http_factory=expiring).ensure_folder("x", "P", reuse_existing=False)
    with pytest.raises(FatalError, match="auth google"):
        DriveDest(None, session_factory=ExpiringSession).upload(mkstream(b"hi"), "P", None)
