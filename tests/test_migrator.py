import pytest

from dbx2gdrive.migrator import Migrator, Options
from dbx2gdrive.retry import FatalError, PermanentError
from dbx2gdrive.state import State

from fakes import FakeDrive, FakeSource, file, folder


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr("dbx2gdrive.retry.time.sleep", lambda s: None)


def account():
    # Listing order deliberately puts children before parents.
    return [
        file("/Photos/2020/a.jpg", b"aaa"),
        folder("/Photos/2020"),
        folder("/Photos"),
        file("/Photos/b.png", b"b" * 10),
        folder("/Empty"),
        file("/notes.txt", b"hello"),
        file("/zero.bin", b""),
        folder("/Docs"),
        file("/Docs/Report 'final'.docx", b"report"),
    ]


def expected(entries, strip=""):
    return {e.path_display[len(strip):]: getattr(e, "data", None) for e in entries if e.path_display != strip}


def run(src, dst, state, root="ROOT", **kw):
    m = Migrator(src, dst, state, root, Options(workers=3, progress_interval=0.05, **kw))
    m.run()
    return m


def test_full_migration_mirrors_tree():
    src, dst, state = FakeSource(account()), FakeDrive(), State(":memory:")
    m = run(src, dst, state)
    assert dst.tree("ROOT") == expected(src.entries)
    assert m.files_done == 5 and m.files_failed == 0
    a = next(i for i in dst.items.values() if i["name"] == "a.jpg")
    assert a["modified"] == "2020-01-02T03:04:05.000Z"


def test_rerun_is_idempotent_and_picks_up_changes():
    entries = account()
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    run(src, dst, state)
    n_items = len(dst.items)

    m = run(src, dst, state)
    assert m.files_done == 0 and len(dst.items) == n_items

    # A file changes and a new one appears in Dropbox.
    entries[5] = file("/notes.txt", b"hello v2", rev="r2")
    entries.append(file("/Photos/new.gif", b"gif"))
    m = run(src, dst, state)
    assert m.files_done == 2
    assert len(dst.items) == n_items + 1  # changed file updated in place, not duplicated
    assert dst.tree("ROOT") == expected(entries)


def test_transient_errors_are_retried_and_permanent_ones_skipped():
    entries = account()
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    src.fail["id:/notes.txt"] = [ConnectionError("reset"), TimeoutError("slow")]
    src.fail["id:/Photos/b.png"] = [PermanentError("restricted content")]
    m = run(src, dst, state)
    assert m.files_done == 4 and m.files_failed == 1
    assert state.summary()[("file", "skipped")][0] == 1
    assert "/notes.txt" in dst.tree("ROOT")

    # Skipped stays skipped unless asked.
    assert run(src, dst, state).files_done == 0
    assert run(src, dst, state, retry_skipped=True).files_done == 1
    assert dst.tree("ROOT") == expected(entries)


def test_failures_beyond_retry_budget_are_retried_next_run():
    entries = account()
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    src.fail["id:/notes.txt"] = [ConnectionError("x")] * 5
    run(src, dst, state)
    assert state.summary()[("file", "failed")][0] == 1
    run(src, dst, state)
    assert dst.tree("ROOT") == expected(entries)


def test_fatal_error_stops_and_resume_finishes():
    entries = account()
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    dst.fail_uploads = [FatalError("Drive full")]
    with pytest.raises(FatalError):
        run(src, dst, state)
    run(src, dst, state)
    assert dst.tree("ROOT") == expected(entries)


def test_subfolder_source():
    entries = account()
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    run(src, dst, state, source_root="/Photos")
    assert dst.tree("ROOT") == expected([e for e in entries if e.path_lower.startswith("/photos/")], "/Photos")


def test_state_persists_on_disk(tmp_path):
    entries = account()
    src, dst = FakeSource(entries), FakeDrive()
    db = str(tmp_path / "s.sqlite3")
    src.fail["id:/zero.bin"] = [PermanentError("nope")]
    run(src, dst, State(db))
    m = run(src, dst, State(db), retry_skipped=True)
    assert m.files_done == 1
    assert dst.tree("ROOT") == expected(entries)


def test_corrupt_dropbox_data_is_never_finalized():
    entries = account()
    entries[5].data = b"hellX"  # bytes no longer match the listed content_hash
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    m = run(src, dst, state)
    assert m.files_failed == 1
    assert "/notes.txt" not in dst.tree("ROOT")
    assert "hash mismatch" in state.problems()[0]["error"]


def test_stop_right_after_google_finishes_does_not_duplicate():
    """VM stopped in the instant between Google finalizing a file and us recording it."""
    entries = account()
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    dst.fail_uploads = ["finish_then_crash"]
    Migrator(src, dst, state, "ROOT", Options(workers=1, progress_interval=60)).run()
    crashed = [r for r in state._all("SELECT path_lower, status, attempts FROM items WHERE kind='file'")
               if r["status"] != "done"]
    assert len(crashed) == 1 and crashed[0]["attempts"] == 1  # Drive has it; we never recorded it
    run(src, dst, state)  # VM comes back
    assert dst.tree("ROOT") == expected(entries)  # tree() asserts there are no duplicates


def test_retry_after_lost_response_does_not_duplicate():
    entries = account()
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    real = dst.upload
    calls = {"n": 0}

    def flaky(stream, parent_id, modified, rev=None, existing_id=None, adopt_existing=False):
        calls["n"] += 1
        fid = real(stream, parent_id, modified, rev, existing_id, adopt_existing)
        if calls["n"] == 1:
            raise ConnectionError("response lost after Google stored the file")
        return fid
    dst.upload = flaky
    run(src, dst, state)
    assert dst.tree("ROOT") == expected(entries)
