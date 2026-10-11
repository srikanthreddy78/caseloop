from __future__ import annotations

from app.api.deps import RateLimiter, StreamSlots


def test_window_sets_the_refill_rate_and_pruning_respects_it() -> None:
    limiter = RateLimiter(limit=3, window_seconds=600)
    for _ in range(3):
        assert limiter.allow("e", now=0.0)
    assert not limiter.allow("e", now=0.0)
    assert not limiter.allow("e", now=199.0)  # 3 per 600 s: one token every 200 s
    assert limiter.allow("e", now=200.0)
    # Pruning after only a minute of idleness would hand the bucket back full; it waits a window.
    for i in range(10_001):
        limiter.allow(f"old-{i}", now=200.0)
    assert not limiter.allow("e", now=290.0)
    assert "e" in limiter._buckets
    limiter.allow("later", now=900.0)
    assert "e" not in limiter._buckets
    limiter.reset()
    assert limiter._buckets == {}


def test_stream_slots_count_per_key_and_release_in_any_order() -> None:
    slots = StreamSlots(caps={"token": 2, "ip": 3})
    a = {"token": "t1", "ip": "1.1.1.1"}
    b = {"token": "t2", "ip": "1.1.1.1"}
    assert slots.acquire(a) and slots.acquire(a)
    assert not slots.acquire(a)  # token cap
    assert slots.acquire(b)
    assert not slots.acquire(b)  # ip cap (2 + 1 = 3), nothing was counted for the refusal
    assert slots.count("ip", "1.1.1.1") == 3 and slots.count("token", "t2") == 1
    slots.release(a)
    assert slots.acquire(b)
    assert slots.count("token", "t2") == 2
    slots.release(b)
    slots.release(b)
    slots.release(a)
    assert slots.count("ip", "1.1.1.1") == 0 and slots._open == {}
    slots.release(a)  # releasing below zero never goes negative
    assert slots.count("token", "t1") == 0


def test_bucket_bursts_then_refills() -> None:
    limiter = RateLimiter(limit=2)
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
    limiter = RateLimiter(limit=1)
    assert limiter.allow("a", now=0.0)
    assert limiter.allow("b", now=0.0)
    assert not limiter.allow("a", now=0.0)
    for i in range(10_001):
        limiter.allow(f"old-{i}", now=0.0)
    assert limiter.allow("new", now=120.0)  # triggers a prune of everything idle for over 60s
    assert len(limiter._buckets) == 1
