"""Rate limiting for listing creation and mutation. Same two-layer pattern used by
the verification service (a per-client fixed window, plus a global daily cap checked
first so spoofing the client address can't grow the per-client table or push total
load past a fixed ceiling) — reimplemented here from scratch rather than imported,
since this is a deliberately separate, independent codebase.

Same known, accepted limitation as the verification service: this reads
request.client.host, which is only the real caller's address if whatever's in front
of this service (a proxy, Render's own edge) sets it correctly and nothing upstream
blindly trusts a client-supplied X-Forwarded-For. The global daily cap exists
specifically to bound the cost of that being spoofed.
"""

import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

_MAX_TRACKED_IPS = 10_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float, global_daily_cap: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.global_daily_cap = global_daily_cap  # <= 0 disables the daily cap
        self._lock = threading.Lock()
        self._counters: dict[str, tuple[float, int]] = {}
        self._day = _utc_now().strftime("%Y-%m-%d")
        self._served_today = 0

    def check(self, request: Request) -> None:
        ip = _client_ip(request)
        now = time.monotonic()
        utc = _utc_now()
        with self._lock:
            today = utc.strftime("%Y-%m-%d")
            if today != self._day:
                self._day, self._served_today = today, 0

            if 0 < self.global_daily_cap <= self._served_today:
                next_midnight = (utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                raise HTTPException(
                    status_code=429,
                    detail="This endpoint has reached its daily request limit across all callers. "
                    "Please try again after 00:00 UTC.",
                    headers={"Retry-After": str(max(1, int((next_midnight - utc).total_seconds())))},
                )

            window_start, count = self._counters.get(ip, (now, 0))
            if now - window_start >= self.window_seconds:
                window_start, count = now, 0
            count += 1
            self._counters[ip] = (window_start, count)
            if count > self.max_requests:
                retry = max(1, math.ceil(self.window_seconds - (now - window_start)))
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests to this endpoint. Please slow down and try again shortly.",
                    headers={"Retry-After": str(retry)},
                )

            self._served_today += 1  # only requests that will be served count toward the cap
            if len(self._counters) > _MAX_TRACKED_IPS:
                for tracked, (started, _) in list(self._counters.items()):
                    if now - started >= self.window_seconds:
                        del self._counters[tracked]

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._day = _utc_now().strftime("%Y-%m-%d")
            self._served_today = 0


create_limiter = RateLimiter(
    max_requests=int(os.getenv("LISTING_CREATE_RATE_LIMIT_MAX_REQUESTS", "5")),
    window_seconds=float(os.getenv("LISTING_CREATE_RATE_LIMIT_WINDOW_SECONDS", "60")),
    global_daily_cap=int(os.getenv("LISTING_CREATE_GLOBAL_DAILY_CAP", "1000")),
)

mutate_limiter = RateLimiter(
    max_requests=int(os.getenv("LISTING_MUTATE_RATE_LIMIT_MAX_REQUESTS", "30")),
    window_seconds=float(os.getenv("LISTING_MUTATE_RATE_LIMIT_WINDOW_SECONDS", "60")),
    global_daily_cap=int(os.getenv("LISTING_MUTATE_GLOBAL_DAILY_CAP", "5000")),
)


def rate_limit_listing_creation(request: Request) -> None:
    create_limiter.check(request)


def rate_limit_listing_mutation(request: Request) -> None:
    mutate_limiter.check(request)
