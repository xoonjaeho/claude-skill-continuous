"""Regression tests for usage_fetch parse + cache robustness.

Guards two confirmed bugs:
  - 2a: a malformed API field (null/non-numeric utilization, unparseable
        resets_at) must raise UsageFetchError, not an escaping TypeError/
        ValueError that crashes the whole probe.
  - 2b: a malformed body must never be written to the shared cache
        (validate-before-cache), and a wrong-shape cached body must fall
        through to a live fetch instead of poisoning the full TTL window.
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import usage_fetch as U  # noqa: E402


VALID_BODY = {
    "five_hour": {"utilization": 96.0, "resets_at": "2026-06-11T10:00:00Z"},
    "seven_day": {"utilization": 40.0, "resets_at": "2026-06-15T00:00:00Z"},
    "extra_usage": {"is_enabled": True},
}


class TestParseBody(unittest.TestCase):
    def test_null_utilization_raises_usage_fetch_error(self):
        body = {"five_hour": {"utilization": None, "resets_at": "2026-06-11T10:00:00Z"}}
        with self.assertRaises(U.UsageFetchError):
            U._parse_body(body, from_cache=False)

    def test_nonnumeric_utilization_raises_usage_fetch_error(self):
        body = {"five_hour": {"utilization": "lots", "resets_at": "2026-06-11T10:00:00Z"}}
        with self.assertRaises(U.UsageFetchError):
            U._parse_body(body, from_cache=False)

    def test_malformed_resets_at_raises_usage_fetch_error(self):
        body = {"five_hour": {"utilization": 96.0, "resets_at": "not-a-date"}}
        with self.assertRaises(U.UsageFetchError):
            U._parse_body(body, from_cache=False)

    def test_missing_five_hour_raises_usage_fetch_error(self):
        with self.assertRaises(U.UsageFetchError):
            U._parse_body({}, from_cache=False)

    def test_non_dict_five_hour_raises_usage_fetch_error(self):
        # API/cache returns five_hour as a string/number → .get() would
        # AttributeError outside the field-coercion try and crash the probe.
        with self.assertRaises(U.UsageFetchError):
            U._parse_body({"five_hour": "oops"}, from_cache=False)
        with self.assertRaises(U.UsageFetchError):
            U._parse_body({"five_hour": 5}, from_cache=False)

    def test_non_dict_body_raises_usage_fetch_error(self):
        with self.assertRaises(U.UsageFetchError):
            U._parse_body([1, 2, 3], from_cache=False)

    def test_non_string_resets_at_raises_usage_fetch_error(self):
        # A non-string non-null resets_at reaches _parse_iso's .replace() →
        # AttributeError, which must be converted, not escape and crash.
        with self.assertRaises(U.UsageFetchError):
            U._parse_body({"five_hour": {"utilization": 96.0, "resets_at": 12345}},
                          from_cache=False)
        with self.assertRaises(U.UsageFetchError):
            U._parse_body({"five_hour": {"utilization": 96.0, "resets_at": ["x"]}},
                          from_cache=False)

    def test_zero_utilization_is_accepted(self):
        # 0% is a valid value and must NOT be rejected as missing/falsy.
        r = U._parse_body(
            {"five_hour": {"utilization": 0, "resets_at": "2026-06-11T10:00:00Z"}},
            from_cache=False,
        )
        self.assertEqual(r.five_hour_util, 0.0)

    def test_valid_body_returns_expected_fields(self):
        r = U._parse_body(VALID_BODY, from_cache=False)
        self.assertEqual(r.five_hour_util, 96.0)
        self.assertEqual(r.five_hour_resets_at, "2026-06-11T10:00:00Z")
        self.assertEqual(r.seven_day_util, 40.0)
        self.assertTrue(r.extra_usage_enabled)
        self.assertFalse(r.from_cache)


class TestFetchUsageCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = (U.C.USAGE_CACHE_ROOT, U.C.USAGE_CACHE_FILE, U.C.USAGE_LOCK_FILE)
        U.C.USAGE_CACHE_ROOT = root
        U.C.USAGE_CACHE_FILE = root / "oauth-usage.json"
        U.C.USAGE_LOCK_FILE = root / "oauth-usage.lock"

    def tearDown(self):
        (U.C.USAGE_CACHE_ROOT, U.C.USAGE_CACHE_FILE, U.C.USAGE_LOCK_FILE) = self._saved
        self._tmp.cleanup()

    def test_malformed_body_not_cached(self):
        malformed = {"five_hour": {"utilization": None, "resets_at": "2026-06-11T10:00:00Z"}}
        with mock.patch.object(U, "_load_access_token", return_value="tok"), \
                mock.patch.object(U, "_http_fetch", return_value=malformed):
            with self.assertRaises(U.UsageFetchError):
                U.fetch_usage(use_cache=True)
        # validate-before-cache: the malformed body must never have been written.
        self.assertFalse(U.C.USAGE_CACHE_FILE.exists())

    def test_corrupt_cache_falls_through_to_live(self):
        # Fresh-but-wrong-shape cached body (valid JSON, missing five_hour).
        U.C.USAGE_CACHE_FILE.write_text(json.dumps({"garbage": 1}), encoding="utf-8")
        with mock.patch.object(U, "_load_access_token", return_value="tok"), \
                mock.patch.object(U, "_http_fetch", return_value=VALID_BODY):
            r = U.fetch_usage(use_cache=True)
        self.assertEqual(r.five_hour_util, 96.0)
        self.assertFalse(r.from_cache)
        # Cache overwritten with the valid live body.
        cached = json.loads(U.C.USAGE_CACHE_FILE.read_text(encoding="utf-8"))
        self.assertIn("five_hour", cached)

    def test_valid_cache_hit_served_without_http(self):
        U.C.USAGE_CACHE_FILE.write_text(json.dumps(VALID_BODY), encoding="utf-8")
        with mock.patch.object(U, "_http_fetch",
                               side_effect=AssertionError("HTTP must not be called on cache hit")):
            r = U.fetch_usage(use_cache=True)
        self.assertEqual(r.five_hour_util, 96.0)
        self.assertTrue(r.from_cache)

    def test_invalid_utf8_cache_falls_through_to_live(self):
        # A cache file with invalid UTF-8 bytes must not crash the read; it
        # should be treated as missing and trigger a live fetch.
        U.C.USAGE_CACHE_FILE.write_bytes(b"\xff\xfe\x00\x01garbage")
        with mock.patch.object(U, "_load_access_token", return_value="tok"), \
                mock.patch.object(U, "_http_fetch", return_value=VALID_BODY):
            r = U.fetch_usage(use_cache=True)
        self.assertEqual(r.five_hour_util, 96.0)
        self.assertFalse(r.from_cache)


class TestHttpFetch(unittest.TestCase):
    def test_timeout_raises_usage_fetch_error(self):
        # A socket read timeout (TimeoutError, an OSError) must surface as
        # UsageFetchError, not escape _http_fetch and crash the probe.
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(U.UsageFetchError):
                U._http_fetch("tok")

    def test_incomplete_read_raises_usage_fetch_error(self):
        # IncompleteRead / BadStatusLine are http.client.HTTPException, not
        # OSError — they must still be converted, not escape and crash the probe.
        with mock.patch("urllib.request.urlopen",
                        side_effect=http.client.IncompleteRead(b"partial")):
            with self.assertRaises(U.UsageFetchError):
                U._http_fetch("tok")

    def test_401_message_carries_marker_for_skill_branch(self):
        # SKILL.md's wake branch keys terminal-vs-transient on the exact
        # substring "401 Unauthorized" in errors[]. Rewording the message would
        # silently break terminal handling — couple the contract here.
        err = urllib.error.HTTPError(U.C.USAGE_API_URL, 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(U.UsageFetchError) as ctx:
                U._http_fetch("tok")
        self.assertIn("401 Unauthorized", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
