"""Tests for constants.clamp_sleep and the sub-TTL sleep ceiling.

The ceiling test is the regression guard for the perf fix in 65dffe3: if
MAX_SLEEP_SEC is ever raised back to 3600 (the deployed-stale value), wakes
overrun the 1h prompt-cache TTL and re-write the whole context instead of
reading it from cache.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import constants as C  # noqa: E402


class TestClampSleep(unittest.TestCase):
    def test_floors_negative_reset_to_min(self):
        # Stale cache / clock skew can yield a negative reset; never sleep <60s.
        self.assertEqual(C.clamp_sleep(-1000), C.MIN_SLEEP_SEC)

    def test_floors_zero_reset_to_min(self):
        self.assertEqual(C.clamp_sleep(0), C.MIN_SLEEP_SEC)

    def test_caps_large_reset_at_max(self):
        self.assertEqual(C.clamp_sleep(10_000), C.MAX_SLEEP_SEC)

    def test_adds_default_buffer_in_normal_range(self):
        self.assertEqual(C.clamp_sleep(1800), 1860)

    def test_just_under_cap_keeps_buffer(self):
        # reset+buffer == MAX exactly: stays at MAX, not clamped away.
        self.assertEqual(C.clamp_sleep(C.MAX_SLEEP_SEC - 60), C.MAX_SLEEP_SEC)


class TestSleepCeilingBelowCacheTTL(unittest.TestCase):
    def test_max_sleep_stays_below_one_hour_ttl(self):
        # The whole point of the cap: a full 3600s sleep overruns the 1h
        # prompt-cache TTL. Raising MAX_SLEEP_SEC back to 3600 must fail here.
        self.assertLess(C.MAX_SLEEP_SEC, 3600)

    def test_clamp_never_exceeds_one_hour_ttl(self):
        for reset in (0, 1800, 3600, 86_400):
            self.assertLess(C.clamp_sleep(reset), 3600)


if __name__ == "__main__":
    unittest.main()
