import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.rate_limit import RateLimiter


def _fake_request(ip: str) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def test_client_ip_falls_back_to_unknown_when_absent() -> None:
    from app.core.rate_limit import _client_ip

    assert _client_ip(SimpleNamespace(client=None)) == "unknown"


def test_allows_requests_up_to_the_limit() -> None:
    limiter = RateLimiter(max_requests=3, window_seconds=60, global_daily_cap=0)
    request = _fake_request("1.2.3.4")
    for _ in range(3):
        limiter.check(request)  # should not raise


def test_blocks_requests_over_the_limit() -> None:
    limiter = RateLimiter(max_requests=3, window_seconds=60, global_daily_cap=0)
    request = _fake_request("1.2.3.4")
    for _ in range(3):
        limiter.check(request)
    with pytest.raises(HTTPException) as exc:
        limiter.check(request)
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_different_ips_are_tracked_independently() -> None:
    limiter = RateLimiter(max_requests=3, window_seconds=60, global_daily_cap=0)
    a, b = _fake_request("1.1.1.1"), _fake_request("2.2.2.2")
    for _ in range(3):
        limiter.check(a)
    limiter.check(b)  # unaffected by a's usage


def test_window_resets_after_it_expires() -> None:
    limiter = RateLimiter(max_requests=3, window_seconds=0.2, global_daily_cap=0)
    request = _fake_request("3.3.3.3")
    for _ in range(3):
        limiter.check(request)
    with pytest.raises(HTTPException):
        limiter.check(request)

    time.sleep(0.4)
    limiter.check(request)  # window reset -> should not raise


def test_reset_clears_state() -> None:
    limiter = RateLimiter(max_requests=1, window_seconds=60, global_daily_cap=0)
    request = _fake_request("4.4.4.4")
    limiter.check(request)
    with pytest.raises(HTTPException):
        limiter.check(request)
    limiter.reset()
    limiter.check(request)  # should not raise after reset


def test_global_daily_cap_kicks_in_even_when_every_caller_looks_different() -> None:
    limiter = RateLimiter(max_requests=100, window_seconds=60, global_daily_cap=5)
    for n in range(5):
        limiter.check(_fake_request(f"10.0.0.{n}"))
    with pytest.raises(HTTPException) as exc:
        limiter.check(_fake_request("10.0.0.99"))
    assert exc.value.status_code == 429
    assert "daily request limit" in exc.value.detail
    assert 1 <= int(exc.value.headers["Retry-After"]) <= 86_400


def test_requests_rejected_by_the_per_ip_limit_do_not_use_up_the_daily_cap() -> None:
    limiter = RateLimiter(max_requests=3, window_seconds=60, global_daily_cap=4)
    hammering = _fake_request("1.1.1.1")
    for _ in range(3):
        limiter.check(hammering)
    for _ in range(50):
        with pytest.raises(HTTPException):
            limiter.check(hammering)
    assert limiter._served_today == 3

    limiter.check(_fake_request("2.2.2.2"))  # the 4th served request
    with pytest.raises(HTTPException) as exc:
        limiter.check(_fake_request("3.3.3.3"))
    assert "daily request limit" in exc.value.detail


def test_daily_cap_of_zero_disables_it() -> None:
    limiter = RateLimiter(max_requests=1000, window_seconds=60, global_daily_cap=0)
    for n in range(50):
        limiter.check(_fake_request(f"7.7.7.{n}"))  # never raises


def test_create_and_mutate_limiters_are_independent() -> None:
    from app.core.rate_limit import rate_limit_listing_creation, rate_limit_listing_mutation

    assert rate_limit_listing_creation is not rate_limit_listing_mutation


# ---- check_ip: the same limiter logic, for callers with no Starlette Request -------


def test_check_ip_behaves_identically_to_check() -> None:
    limiter = RateLimiter(max_requests=2, window_seconds=60, global_daily_cap=0)
    limiter.check_ip("9.9.9.9")
    limiter.check_ip("9.9.9.9")
    with pytest.raises(HTTPException) as exc:
        limiter.check_ip("9.9.9.9")
    assert exc.value.status_code == 429


def test_check_and_check_ip_share_the_same_counters() -> None:
    limiter = RateLimiter(max_requests=2, window_seconds=60, global_daily_cap=0)
    limiter.check(_fake_request("8.8.8.8"))
    limiter.check_ip("8.8.8.8")  # same bucket as the request-based call above
    with pytest.raises(HTTPException):
        limiter.check_ip("8.8.8.8")


def test_mcp_search_limiter_exists_and_is_independent() -> None:
    from app.core.rate_limit import create_limiter, mcp_search_limiter, mutate_limiter

    assert mcp_search_limiter is not create_limiter
    assert mcp_search_limiter is not mutate_limiter
