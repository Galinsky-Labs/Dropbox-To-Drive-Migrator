import requests
import pytest

from dbx2gdrive import retry
from dbx2gdrive.retry import PermanentError, is_network_error, wait_for_network, with_retries

# The exact failures seen on the VM.
DNS = requests.exceptions.ConnectionError(
    "HTTPSConnectionPool(host='api.dropboxapi.com', port=443): Max retries exceeded with url: /oauth2/token "
    "(Caused by NameResolutionError(\"HTTPSConnection(host='api.dropboxapi.com', port=443): Failed to resolve "
    "'api.dropboxapi.com' ([Errno -2] Name or service not known)\"))")
TLS = requests.exceptions.SSLError(
    "HTTPSConnectionPool(host='content.dropboxapi.com', port=443): Max retries exceeded with url: "
    "/2/files/download (Caused by SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred')))")


def test_classifies_network_errors():
    assert is_network_error(DNS) and is_network_error(TLS)
    assert is_network_error(ConnectionResetError()) and is_network_error(TimeoutError())
    assert not is_network_error(IOError("Dropbox content hash mismatch"))
    assert not is_network_error(PermanentError("restricted"))
    wrapped = IOError("upload failed")
    wrapped.__cause__ = DNS
    assert is_network_error(wrapped)


def test_network_outage_does_not_use_up_file_retries():
    calls, waits = [], []
    outcomes = [DNS, TLS, DNS, DNS, DNS, DNS, DNS, DNS, "ok"]  # 8 outages > 5 attempts

    def fn():
        calls.append(1)
        o = outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o

    assert with_retries(fn, attempts=5, _wait=lambda: waits.append(1), _sleep=lambda s: None) == "ok"
    assert len(waits) == 8


def test_file_errors_still_limited():
    def fn():
        raise IOError("Dropbox content hash mismatch")
    with pytest.raises(IOError):
        with_retries(fn, attempts=3, _wait=lambda: None, _sleep=lambda s: None)


def test_endless_network_errors_eventually_give_up():
    def fn():
        raise DNS
    with pytest.raises(requests.exceptions.ConnectionError):
        with_retries(fn, attempts=2, max_network_waits=3, _wait=lambda: None, _sleep=lambda s: None)


def test_wait_for_network_blocks_until_back():
    states = [False, False, True]
    sleeps = []
    wait_for_network(probe=lambda: states.pop(0), sleep=sleeps.append)
    assert sleeps == [15, 15]
    with pytest.raises(IOError, match="unavailable"):
        wait_for_network(max_wait=-1, probe=lambda: False, sleep=lambda s: None)


def test_migration_survives_outage_without_failing_files(monkeypatch):
    from fakes import FakeDrive, FakeSource
    from test_migrator import account, expected, run
    from dbx2gdrive.state import State

    monkeypatch.setattr(retry, "wait_for_network", lambda *a, **k: None)
    monkeypatch.setattr(retry.time, "sleep", lambda s: None)
    monkeypatch.setattr(retry.random, "uniform", lambda a, b: 0)
    entries = account()
    src, dst, state = FakeSource(entries), FakeDrive(), State(":memory:")
    for e in entries:
        if e.kind == "file":
            src.fail[e.id] = [DNS] * 7  # a long outage hits every file, more times than its retry budget
    m = run(src, dst, state)
    assert m.files_failed == 0 and dst.tree("ROOT") == expected(entries)
