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
