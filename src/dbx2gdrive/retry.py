"""Retry-with-backoff for whole-file transfers, with special handling for network outages."""

import logging
import random
import socket
import ssl
import threading
import time

log = logging.getLogger(__name__)

PROBE_HOSTS = ("api.dropboxapi.com", "content.dropboxapi.com", "www.googleapis.com")
_net_lock = threading.Lock()


class PermanentError(Exception):
    """An error that retrying will not fix (file is restricted, quota exhausted, ...)."""


class FatalError(Exception):
    """An error that should stop the whole migration (e.g. Drive storage full).

    retry_in: seconds after which the condition is expected to clear by itself (daily quotas)."""

    def __init__(self, message, retry_in=None):
        super().__init__(message)
        self.retry_in = retry_in


_NETWORK_NAMES = {
    "ConnectionError", "SSLError", "Timeout", "ConnectTimeout", "ReadTimeout", "ProxyError",
    "NameResolutionError", "NewConnectionError", "MaxRetryError", "ProtocolError",
    "ServerNotFoundError", "TransportError", "RemoteDisconnected", "IncompleteRead",
    "ChunkedEncodingError", "SSLEOFError",
}


def is_network_error(exc: BaseException) -> bool:
    """True for failures of the network itself (DNS, dropped/refused connections, TLS cut off),
    as opposed to a problem with a particular file."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror, socket.timeout, ssl.SSLError)):
            return True
        if type(exc).__name__ in _NETWORK_NAMES:
            return True
        text = str(exc)
        if "name resolution" in text or "Name or service not known" in text or "Max retries exceeded" in text:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def network_ok(timeout: float = 5.0) -> bool:
    try:
        for host in PROBE_HOSTS:
            socket.getaddrinfo(host, 443)
        with socket.create_connection(("www.googleapis.com", 443), timeout=timeout):
            pass
        return True
    except OSError:
        return False


def wait_for_network(max_wait: float = 6 * 3600, probe=network_ok, sleep=time.sleep) -> None:
    """Block until Dropbox and Google are reachable again. One thread probes; the rest queue up
    behind it, so an outage pauses every transfer instead of failing them one after another."""
    with _net_lock:
        start = time.monotonic()
        last_log = 0.0
        while not probe():
            waited = time.monotonic() - start
            if waited > max_wait:
                raise IOError(f"network has been unavailable for {waited / 3600:.1f} hours")
            if waited - last_log >= 300 or last_log == 0.0:
                log.warning("Network unavailable (can't reach Dropbox/Google); pausing transfers until it's back.")
                last_log = waited or 1.0
            sleep(15)
        if last_log:
            log.info("Network is back after %.0f minutes; resuming.", (time.monotonic() - start) / 60)


def with_retries(fn, *, attempts=5, base_delay=2.0, max_delay=120.0, what="operation",
                 max_network_waits=50, _wait=None, _sleep=None):
    sleep = _sleep or time.sleep
    wait = _wait or wait_for_network  # looked up at call time so tests can replace it
    attempt, network_waits = 1, 0
    while True:
        try:
            return fn()
        except (PermanentError, FatalError, KeyboardInterrupt):
            raise
        except Exception as exc:
            if is_network_error(exc) and network_waits < max_network_waits:
                # Not this file's fault: wait for the network instead of using up its retries.
                network_waits += 1
                log.warning("%s: network error (%s); waiting for the connection", what, type(exc).__name__)
                wait()
                sleep(random.uniform(1, 5))
                continue
            if attempt >= attempts:
                raise
            delay = getattr(exc, "retry_after", None) or min(max_delay, base_delay * 2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)
            log.warning("%s failed (attempt %d/%d): %s; retrying in %.0fs", what, attempt, attempts, exc, delay)
            sleep(delay)
            attempt += 1
