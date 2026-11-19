"""Generic retry-with-backoff for whole-file transfers."""

import logging
import random
import time

log = logging.getLogger(__name__)


class PermanentError(Exception):
    """An error that retrying will not fix (file is restricted, quota exhausted, ...)."""


class FatalError(Exception):
    """An error that should stop the whole migration (e.g. Drive storage full).

    retry_in: seconds after which the condition is expected to clear by itself (daily quotas)."""

    def __init__(self, message, retry_in=None):
        super().__init__(message)
        self.retry_in = retry_in


def with_retries(fn, *, attempts=5, base_delay=2.0, max_delay=120.0, what="operation"):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (PermanentError, FatalError, KeyboardInterrupt):
            raise
        except Exception as exc:  # network blips, 5xx, rate limits
            if attempt == attempts:
                raise
            delay = getattr(exc, "retry_after", None) or min(max_delay, base_delay * 2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)
            log.warning("%s failed (attempt %d/%d): %s; retrying in %.0fs", what, attempt, attempts, exc, delay)
            time.sleep(delay)
