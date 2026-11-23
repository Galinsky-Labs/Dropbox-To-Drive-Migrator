import pytest


@pytest.fixture(autouse=True)
def no_real_network_wait(monkeypatch, request):
    """Simulated network errors must not trigger real connectivity probes/sleeps in tests."""
    if request.node.name.startswith("test_wait_for_network"):
        return
    from dbx2gdrive import retry
    monkeypatch.setattr(retry, "wait_for_network", lambda *a, **k: None)
    monkeypatch.setattr(retry.random, "uniform", lambda a, b: 0)
