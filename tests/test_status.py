from dbx2gdrive.migrator import Migrator, Options
from dbx2gdrive.retry import PermanentError
from dbx2gdrive.state import State
from dbx2gdrive.status import STATUS_NAME, StatusFile

from fakes import FakeDrive, FakeSource
from test_migrator import account


def status_text(dst):
    return next(it["data"].decode() for it in dst.items.values() if it["name"] == STATUS_NAME)


def test_status_file_tracks_phases_and_completion():
    src, dst, state = FakeSource(account()), FakeDrive(), State(":memory:")
    st = StatusFile(dst, state, "ROOT", "a@dropbox", "b@gmail", interval=3600)
    Migrator(src, dst, state, "ROOT", Options(workers=2, progress_interval=60), status=st).run()
    st.set_phase("done")
    text = status_text(dst)
    assert "STATUS: COMPLETE - all 5 files" in text and "verified" in text
    assert "Files copied:   5 of 5  (100%)" in text
    assert "From: Dropbox a@dropbox" in text
    # Exactly one status file, in the destination root, updated in place.
    assert sum(1 for it in dst.items.values() if it["name"] == STATUS_NAME) == 1


def test_status_lists_problems_and_waits():
    entries = account()
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    src.fail["id:/notes.txt"] = [PermanentError("restricted content")]
    st = StatusFile(dst, state, "ROOT", interval=3600)
    Migrator(src, dst, state, "ROOT", Options(workers=2, progress_interval=60), status=st).run()
    st.set_phase("done")
    text = status_text(dst)
    assert "FINISHED - 4 of 5 files" in text
    assert "[cannot copy] /notes.txt" in text and "restricted content" in text
    st.set_phase("waiting", "Paused: Google's daily upload limit was reached.")
    assert "STATUS: Paused: Google's daily upload limit" in status_text(dst)


def test_status_recreated_if_deleted_and_never_breaks_migration():
    src, dst, state = FakeSource(account()), FakeDrive(), State(":memory:")
    st = StatusFile(dst, state, "ROOT", interval=3600)
    st.write_now()
    fid = state.get_meta("status_file_id")
    del dst.items[fid]
    orig = dst.write_text

    def write(name, parent, text, file_id=None):
        if file_id and file_id not in dst.items:
            raise PermanentError("notFound")
        return orig(name, parent, text, file_id)
    dst.write_text = write
    st.write_now()
    assert state.get_meta("status_file_id") != fid and status_text(dst)

    dst.write_text = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Drive down"))
    st.write_now()  # logs a warning, does not raise
