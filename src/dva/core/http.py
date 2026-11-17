"""HTTP helpers with retries, backoff, Retry-After support and secret redaction."""
from __future__ import annotations

import functools
import logging
import random
import time
from typing import Callable, TypeVar

import requests

from .errors import TransientError
from .log import redact

log = logging.getLogger(__name__)
T = TypeVar("T")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def retry(attempts: int = 4, base_delay: float = 2.0, max_delay: float = 60.0,
          exceptions: tuple = (TransientError, requests.ConnectionError, requests.Timeout)):
    """Exponential backoff with jitter."""

    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            for i in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    if i == attempts:
                        raise
                    wait = getattr(exc, "retry_after", None) or min(max_delay, base_delay * 2 ** (i - 1))
                    wait += random.uniform(0, 0.5 * base_delay)
                    log.warning("%s failed (%s); retry %d/%d in %.1fs", fn.__name__,
                                redact(str(exc))[:200], i, attempts - 1, wait)
                    time.sleep(wait)
            raise RuntimeError("unreachable")

        return wrapper

    return deco


class HttpClient:
    """requests.Session wrapper. Swappable in tests."""

    def __init__(self, user_agent: str = "DailyFactsBot/0.1", timeout: float = 30):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout

    def request(self, method: str, url: str, *, ok_status: tuple = (200, 201), **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        resp = self.session.request(method, url, **kw)
        if resp.status_code in RETRY_STATUS:
            err = TransientError(f"HTTP {resp.status_code} for {redact(url)}: {redact(resp.text[:300])}")
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                err.retry_after = min(float(ra), 120)  # type: ignore[attr-defined]
            raise err
        if ok_status and resp.status_code not in ok_status:
            raise requests.HTTPError(f"HTTP {resp.status_code} for {redact(url)}: {redact(resp.text[:500])}",
                                     response=resp)
        return resp

    @retry()
    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    @retry()
    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)

    def post_once(self, url: str, **kw) -> requests.Response:
        """Non-idempotent POST (no automatic retry)."""
        return self.request("POST", url, **kw)
