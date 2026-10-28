"""The password throttle's core algorithm, tested directly against `ratelimit.py` rather than
through the HTTP layer: a single TestClient can't present two different source IPs, and
IP-vs-username independence is exactly what needs proving. The `/login` + `/passwd` wiring
(429 + Retry-After, what a success clears) is covered in test_logging.py.
"""

import pytest

from ccc_head_api import ratelimit

IP, IP2 = ratelimit.ip("1.2.3.4"), ratelimit.ip("9.9.9.9")
ALICE, BOB = ratelimit.user("alice"), ratelimit.user("bob")


@pytest.fixture(autouse=True)
def _clean():
    ratelimit.reset()
    yield
    ratelimit.reset()


def _fail(n, *keys):
    for _ in range(n):
        ratelimit.record_failure(*keys)


class TestLimits:
    @pytest.mark.parametrize("key, limit", [(ALICE, ratelimit.USER_LIMIT), (IP, ratelimit.IP_LIMIT)])
    def test_each_key_blocks_at_its_own_limit(self, key, limit):
        _fail(limit - 1, key)
        assert ratelimit.check(key) is None
        ratelimit.record_failure(key)
        assert 0 < ratelimit.check(key) <= ratelimit.WINDOW

    def test_campus_nat_ip_outlasts_a_username(self):
        """Many people share an eduroam exit IP: their typos must not block it as fast as one account."""
        _fail(ratelimit.USER_LIMIT, IP)
        assert ratelimit.check(IP) is None


class TestDualKeying:
    """Blocked if EITHER key trips — an attacker can't dodge by spraying usernames from one IP, and an
    account can't be starved by guesses spread across many IPs. But the two keys are independent:
    tripping one must not block a caller who only shares the OTHER."""

    def test_a_different_username_from_a_blocked_ip_still_trips(self):
        for i in range(ratelimit.IP_LIMIT):
            ratelimit.record_failure(IP, ratelimit.user(f"u{i}"))
        assert ratelimit.check(IP, BOB) is not None

    def test_the_same_username_from_a_different_ip_still_trips(self):
        _fail(ratelimit.USER_LIMIT, IP, ALICE)
        assert ratelimit.check(IP2, ALICE) is not None

    def test_an_unrelated_ip_and_username_are_unaffected(self):
        _fail(ratelimit.USER_LIMIT, IP, ALICE)
        assert ratelimit.check(IP2, BOB) is None


class TestClearAndExpiry:
    def test_clear_resets_a_key(self):
        _fail(ratelimit.USER_LIMIT, ALICE)
        ratelimit.clear(ALICE)
        assert ratelimit.check(ALICE) is None

    def test_a_failure_older_than_the_window_no_longer_counts(self, monkeypatch):
        """The counting window IS the cooldown: a failure ages out once it's older than that, which is
        also what lets a blocked key clear itself after waiting it out."""
        t = [1000.0]
        monkeypatch.setattr(ratelimit.time, "monotonic", lambda: t[0])
        _fail(ratelimit.USER_LIMIT, ALICE)
        assert ratelimit.check(ALICE) is not None
        t[0] += ratelimit.WINDOW + 1
        assert ratelimit.check(ALICE) is None

    def test_expired_keys_are_dropped_so_spraying_cannot_grow_memory(self, monkeypatch):
        t = [1000.0]
        monkeypatch.setattr(ratelimit.time, "monotonic", lambda: t[0])
        for i in range(100):
            ratelimit.record_failure(ratelimit.user(f"u{i}"))
        t[0] += ratelimit.WINDOW + 1
        ratelimit.record_failure(BOB)
        assert set(ratelimit._failures) == {BOB}
