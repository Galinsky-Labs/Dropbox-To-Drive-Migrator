"""Runs the full migration against a real PostgreSQL state store.

Set DBX2GDRIVE_TEST_PG to a throwaway database URL to enable, e.g.
DBX2GDRIVE_TEST_PG=postgresql://postgres@localhost:5432/postgres pytest
"""

import os

import pytest

from dbx2gdrive.retry import PermanentError
from dbx2gdrive.state import State

from fakes import FakeDrive, FakeSource
from test_migrator import account, expected, run

PG = os.environ.get("DBX2GDRIVE_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="set DBX2GDRIVE_TEST_PG to run Postgres tests")


@pytest.fixture
def pg_state():
    s = State(PG)
    s._write("DELETE FROM items")
    s._write("DELETE FROM meta")
    return s


def test_full_migration_and_resume_on_postgres(pg_state):
    entries = account()
    src, dst = FakeSource(entries), FakeDrive()
    src.fail["id:/zero.bin"] = [PermanentError("nope")]
    pg_state.set_meta("dest_root_id", "ROOT")
    pg_state.set_meta("dest_root_id", "ROOT")  # upsert path
    m = run(src, dst, pg_state)
    assert m.files_done == 4
    assert pg_state.summary()[("file", "skipped")] == (1, 0)
    # A fresh connection (as after a server restart) sees the same state and resumes.
    again = State(PG)
    assert again.get_meta("dest_root_id") == "ROOT"
    assert run(src, dst, again, retry_skipped=True).files_done == 1
    assert dst.tree("ROOT") == expected(entries)
    assert again.pending_file_bytes() == 0 and again.undownloadable_count() == 0


def test_reconnects_after_connection_drop(pg_state):
    pg_state.set_meta("k", "v")
    pg_state.db.conn.close()  # simulate Neon dropping an idle connection
    assert pg_state.get_meta("k") == "v"
