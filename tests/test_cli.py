import pytest

from dbx2gdrive import cli
from fakes import FakeDrive, FakeSource, file, folder


@pytest.fixture
def wired(monkeypatch):
    entries = [folder("/A"), file("/A/x.txt", b"x"), file("/y.txt", b"yy")]
    drive = FakeDrive()
    drive.account_label = lambda: "drive-user"
    drive.check_folder = lambda fid: "Existing"

    class Src(FakeSource):
        def __init__(self, *a, **k): super().__init__(entries)
        def account_label(self): return "dbx-user"

    monkeypatch.setattr("dbx2gdrive.dropbox_source.DropboxSource", Src)
    monkeypatch.setattr("dbx2gdrive.drive_dest.DriveDest", lambda *a, **k: drive)
    monkeypatch.setattr("dbx2gdrive.auth.google_credentials", lambda d: None)
    return drive


def test_migrate_then_status(wired, tmp_path, capsys):
    base = ["--config-dir", str(tmp_path)]
    assert cli.main(base + ["migrate"]) == 0
    root = next(fid for fid, it in wired.items.items() if it["name"] == "Dropbox")
    assert wired.tree(root) == {"/A": None, "/A/x.txt": b"x", "/y.txt": b"yy"}
    # Re-run reuses the same destination and copies nothing new.
    assert cli.main(base + ["migrate"]) == 0
    assert sum(1 for it in wired.items.values() if it["name"] == "Dropbox") == 1
    assert len(wired.items) == 4
    assert cli.main(base + ["status", "--problems"]) == 0
    assert "Migration complete." in capsys.readouterr().out
    # Switching destination with the same state file is refused.
    with pytest.raises(SystemExit, match="different Drive destination"):
        cli.main(base + ["migrate", "--dest-folder-id", "OTHER"])


def test_dry_run_writes_nothing(wired, tmp_path, capsys):
    assert cli.main(["--config-dir", str(tmp_path), "migrate", "--dry-run"]) == 0
    assert wired.items == {}
    assert "2 files (3 B) in 1 folders would be copied" in capsys.readouterr().out


def test_auth_export_import_roundtrip(tmp_path, monkeypatch, capsys):
    import io
    import json
    import os

    from dbx2gdrive import auth

    src = tmp_path / "mac"
    auth._write_secret(src / "dropbox_token.json", {"app_key": "K", "app_secret": None, "refresh_token": "R"})
    google = {"refresh_token": "G", "client_id": "C", "client_secret": "S", "token": "short-lived",
              "token_uri": "https://oauth2.googleapis.com/token", "scopes": auth.GOOGLE_SCOPES}
    auth._write_secret(src / "google_token.json", google)
    assert cli.main(["--config-dir", str(src), "auth", "export"]) == 0
    exported = capsys.readouterr().out
    assert "short-lived" not in exported and "DROPBOX_REFRESH_TOKEN=R" in exported

    dst = tmp_path / "server"
    monkeypatch.setattr("sys.stdin", io.StringIO("# comment\n" + exported))
    assert cli.main(["--config-dir", str(dst), "auth", "import"]) == 0
    assert json.loads((dst / "dropbox_token.json").read_text())["refresh_token"] == "R"
    assert json.loads((dst / "google_token.json").read_text())["refresh_token"] == "G"
    assert oct(os.stat(dst / "google_token.json").st_mode & 0o777) == "0o600"

    # The same values work as environment variables (Render-style).
    for line in exported.strip().splitlines():
        k, v = line.split("=", 1)
        monkeypatch.setenv(k, v)
    assert auth._dropbox_token(tmp_path / "nowhere")["refresh_token"] == "R"
    assert auth._google_token(tmp_path / "nowhere")[0]["client_id"] == "C"


class _Stop(Exception):
    pass


def _service(monkeypatch, tmp_path, outcomes):
    """Run `migrate --service` where each attempt yields the next outcome; capture idle messages."""
    calls, idles = [], []

    def fake_once(args, service=False):
        calls.append(1)
        o = outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o

    def fake_idle(msg, seconds=None):
        idles.append((msg, seconds))
        if seconds is None:
            raise _Stop()

    monkeypatch.setattr(cli, "_migrate_once", fake_once)
    monkeypatch.setattr(cli, "_idle", fake_idle)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    with pytest.raises(_Stop):
        cli.main(["--config-dir", str(tmp_path), "migrate", "--service"])
    return calls, idles


def test_service_waits_out_daily_limit_then_finishes(monkeypatch, tmp_path):
    from dbx2gdrive.retry import FatalError
    calls, idles = _service(monkeypatch, tmp_path, [FatalError("limit", retry_in=3600), 0])
    assert len(calls) == 2 and idles[0][1] == 3600 and "complete" in idles[-1][0]


def test_service_pauses_on_bad_dropbox_signin(monkeypatch, tmp_path):
    from dropbox.exceptions import AuthError
    calls, idles = _service(monkeypatch, tmp_path, [AuthError("req", "invalid_access_token")])
    assert "Dropbox rejected the sign-in" in idles[-1][0]


def test_service_retries_unexpected_errors_and_failed_files(monkeypatch, tmp_path):
    calls, idles = _service(monkeypatch, tmp_path, [RuntimeError("boom"), 1, 1, 1, 1])
    assert len(calls) == 5 and "could not be copied" in idles[-1][0]


def test_google_manual_signin_on_server(tmp_path, monkeypatch, capsys):
    import json
    from urllib.parse import parse_qs, urlparse

    from google_auth_oauthlib.flow import InstalledAppFlow

    client = {"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec",
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://oauth2.googleapis.com/token",
                            "redirect_uris": ["http://localhost"]}}
    pasted = json.dumps(client, indent=2).splitlines()  # pasted over many lines
    seen = {}

    def fake_fetch(self, **kw):
        seen.update(kw, verifier=self.code_verifier)
        self.oauth2session.token = {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "expires_at": 9e9}

    monkeypatch.setattr(InstalledAppFlow, "fetch_token", fake_fetch)
    answers = iter(pasted + ["http://localhost:1/?state=s&code=4/abc&scope=https://www.googleapis.com/auth/drive"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert cli.main(["--config-dir", str(tmp_path), "auth", "google", "--client-secret", "-", "--manual"]) == 0

    out = capsys.readouterr().out
    url = next(w for w in out.split() if w.startswith("https://accounts.google.com"))
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"] == ["http://localhost:1/"] and q["access_type"] == ["offline"]
    assert "code_challenge" in q and seen["verifier"]  # PKCE
    assert seen["authorization_response"].startswith("http://localhost:1/")
    saved = json.loads((tmp_path / "google_token.json").read_text())
    assert saved["refresh_token"] == "RT" and saved["client_id"] == "cid.apps.googleusercontent.com"


def test_google_signin_rejects_web_client(tmp_path, monkeypatch):
    import json
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(SystemExit, match="Desktop app"):
        cli.main(["--config-dir", str(tmp_path), "auth", "google", "--client-secret", str(p), "--manual"])
