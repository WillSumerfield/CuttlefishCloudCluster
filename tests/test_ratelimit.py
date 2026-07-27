"""Phase 8 loose end — the `/login` throttle's core algorithm, tested directly against `ratelimit.py`
rather than through the HTTP layer: a single TestClient can't present two different source IPs, and the
IP-vs-username independence is exactly what needs proving. `/login`'s wiring to this module (429 +
Retry-After, success clears the counters) is covered separately in test_logging.py.
"""

import pytest

from ccc_head_api import ratelimit


@pytest.fixture(autouse=True)
def _clean():
    ratelimit.reset()
    yield
    ratelimit.reset()


class TestThreshold:
    def test_below_threshold_is_not_blocked(self):
        for _ in range(ratelimit.THRESHOLD - 1):
            ratelimit.record_failure("ip:1.2.3.4")
        assert ratelimit.check("ip:1.2.3.4") is None

    def test_at_threshold_is_blocked(self):
        for _ in range(ratelimit.THRESHOLD):
            ratelimit.record_failure("ip:1.2.3.4")
        wait = ratelimit.check("ip:1.2.3.4")
        assert wait is not None
        assert 0 < wait <= ratelimit.COOLDOWN


class TestDualKeying:
    """Blocked if EITHER key trips — an attacker can't dodge by spraying usernames from one IP, and an
    account can't be starved by guesses spread across many IPs. But the two keys are independent:
    tripping one must not block a caller who only shares the OTHER."""

    def test_a_different_username_from_a_blocked_ip_still_trips(self):
        for _ in range(ratelimit.THRESHOLD):
            ratelimit.record_failure("ip:1.2.3.4", "user:alice")
        assert ratelimit.check("ip:1.2.3.4", "user:bob") is not None

    def test_the_same_username_from_a_different_ip_still_trips(self):
        for _ in range(ratelimit.THRESHOLD):
            ratelimit.record_failure("ip:1.2.3.4", "user:alice")
        assert ratelimit.check("ip:9.9.9.9", "user:alice") is not None

    def test_an_unrelated_ip_and_username_are_unaffected(self):
        for _ in range(ratelimit.THRESHOLD):
            ratelimit.record_failure("ip:1.2.3.4", "user:alice")
        assert ratelimit.check("ip:9.9.9.9", "user:bob") is None


class TestClearAndExpiry:
    def test_clear_resets_a_key(self):
        for _ in range(ratelimit.THRESHOLD):
            ratelimit.record_failure("ip:1.2.3.4")
        ratelimit.clear("ip:1.2.3.4")
        assert ratelimit.check("ip:1.2.3.4") is None

    def test_a_failure_older_than_the_cooldown_no_longer_counts(self, monkeypatch):
        """The counting window IS the cooldown: a failure ages out once it's older than that, which is
        also what lets a blocked key clear itself after waiting it out."""
        t = [1000.0]
        monkeypatch.setattr(ratelimit.time, "monotonic", lambda: t[0])
        for _ in range(ratelimit.THRESHOLD):
            ratelimit.record_failure("ip:1.2.3.4")
        assert ratelimit.check("ip:1.2.3.4") is not None
        t[0] += ratelimit.COOLDOWN + 1
        assert ratelimit.check("ip:1.2.3.4") is None
