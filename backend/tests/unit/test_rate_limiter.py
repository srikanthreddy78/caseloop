from __future__ import annotations

from app.api.deps import RateLimiter


def test_bucket_bursts_then_refills() -> None:
    limiter = RateLimiter(per_minute=2)
    assert limiter.allow("k", now=0.0)
    assert limiter.allow("k", now=0.0)
    assert not limiter.allow("k", now=0.0)
    assert not limiter.allow("k", now=10.0)  # 2/min = one token per 30s
    assert limiter.allow("k", now=30.0)
    assert not limiter.allow("k", now=30.0)
    # Never accumulates more than the burst size, however long it sits idle.
    assert limiter.allow("k", now=1000.0)
    assert limiter.allow("k", now=1000.0)
    assert not limiter.allow("k", now=1000.0)


def test_buckets_are_independent_and_pruned() -> None:
    limiter = RateLimiter(per_minute=1)
    assert limiter.allow("a", now=0.0)
    assert limiter.allow("b", now=0.0)
    assert not limiter.allow("a", now=0.0)
    for i in range(10_001):
        limiter.allow(f"old-{i}", now=0.0)
    assert limiter.allow("new", now=120.0)  # triggers a prune of everything idle for over 60s
    assert len(limiter._buckets) == 1
