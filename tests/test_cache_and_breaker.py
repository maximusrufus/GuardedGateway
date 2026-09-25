from guardedgateway import cache


def test_cache_key_deterministic():
    k1 = cache.cache_key("openai/gpt-4o", [{"role": "user", "content": "hi"}], 100, 0.0)
    k2 = cache.cache_key("openai/gpt-4o", [{"role": "user", "content": "hi"}], 100, 0.0)
    assert k1 == k2


def test_cache_key_differs_on_content():
    k1 = cache.cache_key("m", [{"role": "user", "content": "a"}], 100, 0.0)
    k2 = cache.cache_key("m", [{"role": "user", "content": "b"}], 100, 0.0)
    assert k1 != k2


def test_response_cache_hit_and_miss():
    rc = cache.ResponseCache(ttl_seconds=60.0)
    assert rc.get("k") is None
    rc.set("k", "value")
    assert rc.get("k") == "value"
    assert rc.hits == 1
    assert rc.misses == 1


def test_response_cache_ttl_expiry():
    clock = {"t": 0.0}
    rc = cache.ResponseCache(ttl_seconds=10.0, now=lambda: clock["t"])
    rc.set("k", "v")
    clock["t"] = 5.0
    assert rc.get("k") == "v"
    clock["t"] = 11.0
    assert rc.get("k") is None


def test_circuit_breaker_opens_after_threshold():
    clock = {"t": 0.0}
    cb = cache.CircuitBreaker(failure_threshold=3, backoff_seconds=30.0, now_fn=lambda: clock["t"])
    assert cb.is_open("openai") is False
    cb.record_failure("openai")
    cb.record_failure("openai")
    assert cb.is_open("openai") is False  # 2 failures, threshold is 3
    cb.record_failure("openai")
    assert cb.is_open("openai") is True


def test_circuit_breaker_half_opens_after_backoff():
    clock = {"t": 0.0}
    cb = cache.CircuitBreaker(failure_threshold=1, backoff_seconds=30.0, now_fn=lambda: clock["t"])
    cb.record_failure("openai")
    assert cb.is_open("openai") is True
    clock["t"] = 31.0
    # backoff elapsed: allow exactly one probe through (is_open() returns False)
    assert cb.is_open("openai") is False


def test_circuit_breaker_success_resets():
    clock = {"t": 0.0}
    cb = cache.CircuitBreaker(failure_threshold=2, backoff_seconds=30.0, now_fn=lambda: clock["t"])
    cb.record_failure("openai")
    cb.record_failure("openai")
    assert cb.is_open("openai") is True
    clock["t"] = 31.0
    assert cb.is_open("openai") is False  # half-open probe allowed
    cb.record_success("openai")
    assert cb.is_open("openai") is False
    assert cb._state("openai").failures == 0


def test_circuit_breaker_failed_probe_reopens():
    clock = {"t": 0.0}
    cb = cache.CircuitBreaker(failure_threshold=1, backoff_seconds=10.0, now_fn=lambda: clock["t"])
    cb.record_failure("openai")
    clock["t"] = 11.0
    assert cb.is_open("openai") is False  # half-open probe
    cb.record_failure("openai")  # probe failed
    assert cb.is_open("openai") is True
