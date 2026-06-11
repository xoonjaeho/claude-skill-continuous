"""OAuth /api/oauth/usage client with disk cache and lock.

Reloads ~/.claude/.credentials.json on every fetch — Claude Code's main
process is the refresh authority. On 401, fail loud (do not attempt
self-refresh).

CLI test harness:
  python usage_fetch.py                # live fetch
  python usage_fetch.py --mock '<json>' # parse mock body
  python usage_fetch.py --mock-401     # simulate 401
  python usage_fetch.py --no-cache     # bypass disk cache
  python usage_fetch.py --brief        # 5-field projection (used by SKILL)

Used by:
  - hook.py (rate-limit gate)
  - skill body (initial probe, wake-turn re-check) — must set
    CONTINUOUS_SELF_PROBE=1 in the parent Bash command line so the
    hook does not block the probe itself.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import constants as C  # noqa: E402


class UsageFetchError(Exception):
    """Raised on terminal fetch failures (401, parse error, network)."""


@dataclass
class UsageResult:
    five_hour_util: float
    five_hour_resets_at: str          # ISO 8601 w/ TZ
    five_hour_reset_remaining_sec: int
    seven_day_util: float
    seven_day_resets_at: str | None
    seven_day_reset_remaining_sec: int | None
    extra_usage_enabled: bool
    fetched_at: str                   # ISO 8601 UTC
    from_cache: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Token ──────────────────────────────────────────────────────────────────

def _load_access_token() -> str:
    """Reload the access token from .credentials.json each call."""
    try:
        data = json.loads(C.CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise UsageFetchError(f"credentials file missing: {C.CREDENTIALS_FILE}") from e
    except json.JSONDecodeError as e:
        raise UsageFetchError(f"credentials file malformed: {e}") from e
    tok = (data.get("claudeAiOauth") or {}).get("accessToken")
    if not tok:
        raise UsageFetchError("claudeAiOauth.accessToken missing in credentials")
    return tok


# ─── Cache + lock ───────────────────────────────────────────────────────────

def _read_cache() -> dict[str, Any] | None:
    """Return cached usage dict if present and fresh; else None.

    Negative age (clock skew, file copied with future mtime) is treated as
    stale rather than fresh — better to over-fetch than to serve stale data.
    """
    try:
        st = C.USAGE_CACHE_FILE.stat()
    except OSError:
        # FileNotFoundError (no cache yet) or a transient PermissionError /
        # Windows AV-lock — either way, fall through to a live fetch.
        return None
    age = time.time() - st.st_mtime
    if age < 0 or age >= C.USAGE_CACHE_TTL_SEC:
        return None
    try:
        return json.loads(C.USAGE_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers both JSONDecodeError and a UnicodeDecodeError from a
        # cache file with invalid UTF-8 bytes (disk corruption / foreign writer).
        return None


def _write_cache(payload: dict[str, Any]) -> None:
    C.USAGE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    # Atomic write with PID-suffixed tmp. The shared cache file is written by
    # multiple processes (this skill + the user's statusline if configured);
    # a per-process tmp name eliminates tmp-collision in the rare race where
    # the advisory lock fails to serialize them.
    tmp = C.USAGE_CACHE_FILE.with_suffix(
        C.USAGE_CACHE_FILE.suffix + f".tmp.{os.getpid()}"
    )
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, C.USAGE_CACHE_FILE)
    except OSError as e:
        print(f"[usage_fetch] cache write failed: {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _take_lock() -> bool:
    """Atomically claim the lock. Returns True if we got it, False if another
    process beat us to it.

    Uses O_CREAT|O_EXCL — POSIX-atomic on local filesystems and the standard
    pattern for cross-process mutual exclusion without an extra dependency.
    Stale-lock recovery: if an existing lock is older than USAGE_LOCK_TTL_SEC,
    delete it and retry once (the previous holder almost certainly died).
    """
    C.USAGE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(str(C.USAGE_LOCK_FILE),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(int(time.time())).encode("ascii"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            # Existing lock — check if it's stale.
            try:
                age = time.time() - C.USAGE_LOCK_FILE.stat().st_mtime
            except FileNotFoundError:
                continue  # raced with the holder's release; retry
            if age < 0 or age >= C.USAGE_LOCK_TTL_SEC:
                try:
                    C.USAGE_LOCK_FILE.unlink(missing_ok=True)
                    continue
                except OSError:
                    return False
            return False
        except OSError:
            return False
    return False


def _release_lock() -> None:
    try:
        C.USAGE_LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ─── HTTP ───────────────────────────────────────────────────────────────────

def _http_fetch(token: str) -> dict[str, Any]:
    headers = dict(C.USAGE_API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(C.USAGE_API_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=C.USAGE_API_TIMEOUT_SEC) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise UsageFetchError(
                "401 Unauthorized — accessToken expired or revoked. "
                "Claude Code's background refresh did not catch this. "
                "Re-login or restart Claude Code may be required."
            ) from e
        raise UsageFetchError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
    except urllib.error.URLError as e:
        raise UsageFetchError(f"network error: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise UsageFetchError(f"response not JSON: {e}") from e
    except http.client.HTTPException as e:
        # IncompleteRead / BadStatusLine / LineTooLong on a truncated or
        # malformed response are HTTPException-only (not OSError/URLError), so
        # they would otherwise escape and crash the probe.
        raise UsageFetchError(f"malformed HTTP response: {e}") from e
    except OSError as e:
        # Read timeouts surface as socket.timeout (TimeoutError), which is an
        # OSError but not always a URLError; without this catch it escapes and
        # crashes the probe (probe._do_usage catches only UsageFetchError).
        raise UsageFetchError(f"network error: {e}") from e


# ─── Parse ──────────────────────────────────────────────────────────────────

def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # Python 3.9/3.10 do not accept the trailing 'Z' shorthand. Normalize.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _reset_remaining_sec(reset_at: datetime | None) -> int | None:
    if reset_at is None:
        return None
    now = datetime.now(timezone.utc)
    return int((reset_at - now).total_seconds())


def _parse_body(body: Any, from_cache: bool) -> UsageResult:
    # A non-dict body or section (API/cache returns a string/list/number) must
    # degrade to a clean UsageFetchError — `.get()` on a non-dict raises
    # AttributeError, which is outside the field-coercion try below and would
    # escape to crash the whole probe.
    if not isinstance(body, dict):
        raise UsageFetchError(f"usage body is not an object: {type(body).__name__}")
    fh = body.get("five_hour") or {}
    sd = body.get("seven_day") or {}
    eu = body.get("extra_usage") or {}

    if not (isinstance(fh, dict) and isinstance(sd, dict) and isinstance(eu, dict)):
        raise UsageFetchError(
            f"usage sections must be objects: "
            f"five_hour={fh!r} seven_day={sd!r} extra_usage={eu!r}"
        )

    if fh.get("utilization") is None or fh.get("resets_at") is None:
        raise UsageFetchError(f"five_hour fields missing or null: {fh!r}")

    # A malformed field type (non-numeric utilization, unparseable resets_at)
    # must degrade to a clean UsageFetchError, not an escaping TypeError/
    # ValueError. probe.py's _do_usage catches only UsageFetchError; anything
    # else kills the whole probe (no JSON emitted, no usage=null degrade).
    try:
        fh_reset = _parse_iso(fh["resets_at"])
        sd_reset = _parse_iso(sd.get("resets_at"))
        return UsageResult(
            five_hour_util=float(fh["utilization"]),
            five_hour_resets_at=fh["resets_at"],
            five_hour_reset_remaining_sec=_reset_remaining_sec(fh_reset) or 0,
            seven_day_util=float(sd.get("utilization") or 0.0),
            seven_day_resets_at=sd.get("resets_at"),
            seven_day_reset_remaining_sec=_reset_remaining_sec(sd_reset),
            extra_usage_enabled=bool(eu.get("is_enabled", False)),
            fetched_at=datetime.now(timezone.utc).isoformat(),
            from_cache=from_cache,
        )
    except (ValueError, TypeError, AttributeError) as e:
        # AttributeError covers a non-string resets_at reaching _parse_iso's
        # .replace() (the presence check only rejects None, not e.g. an int).
        raise UsageFetchError(f"malformed usage field: {e}") from e


# ─── Public API ─────────────────────────────────────────────────────────────

def fetch_usage(use_cache: bool = True) -> UsageResult:
    """Get current usage. Honors disk cache (USAGE_CACHE_TTL_SEC) and lock.

    On 401 raises UsageFetchError immediately — caller (hook or skill body)
    decides to fail-loud and tear down state.
    """
    if use_cache:
        cached = _read_cache()
        if cached is not None:
            try:
                return _parse_body(cached, from_cache=True)
            except UsageFetchError:
                # Wrong-shape cached body must not poison the whole TTL window:
                # fall through to a live fetch instead of failing every read.
                pass

    # Race-free lock acquisition via O_CREAT|O_EXCL.
    got_lock = _take_lock()
    if not got_lock:
        # Another process is fetching; serve stale if available, else wait
        # briefly and try one more time before falling through to a parallel
        # fetch (worst case: one extra HTTP call).
        try:
            stale = json.loads(C.USAGE_CACHE_FILE.read_text(encoding="utf-8"))
            return _parse_body(stale, from_cache=True)
        except (OSError, ValueError, UsageFetchError):
            time.sleep(1)
            got_lock = _take_lock()

    try:
        token = _load_access_token()
        body = _http_fetch(token)
        # Validate BEFORE caching: a malformed body must never reach the shared
        # cache, or it would re-fail every read for the full TTL window.
        result = _parse_body(body, from_cache=False)
        _write_cache(body)
        return result
    finally:
        if got_lock:
            _release_lock()


# ─── CLI test harness ───────────────────────────────────────────────────────

def to_brief(r: UsageResult) -> dict[str, Any]:
    """Slim 5-field projection for skill body consumption.

    Drops fields the skill's decision tree never reads
    (extra_usage_enabled, fetched_at, from_cache, five_hour_resets_at).
    """
    return {
        "five_hour_util": r.five_hour_util,
        "five_hour_reset_remaining_sec": r.five_hour_reset_remaining_sec,
        "seven_day_util": r.seven_day_util,
        "seven_day_resets_at": r.seven_day_resets_at,
        "seven_day_reset_remaining_sec": r.seven_day_reset_remaining_sec,
    }


def _cli() -> int:
    p = argparse.ArgumentParser(description="Usage fetcher test harness")
    p.add_argument("--mock", help="JSON body to parse instead of live fetch")
    p.add_argument("--mock-401", action="store_true", help="simulate 401")
    p.add_argument("--no-cache", action="store_true", help="bypass disk cache")
    p.add_argument("--brief", action="store_true",
                   help="emit only the 5 fields the skill body actually reads")
    args = p.parse_args()

    try:
        if args.mock_401:
            raise UsageFetchError(
                "401 Unauthorized — accessToken expired (mock)"
            )
        if args.mock:
            body = json.loads(args.mock)
            result = _parse_body(body, from_cache=False)
        else:
            result = fetch_usage(use_cache=not args.no_cache)
    except UsageFetchError as e:
        print(f"FETCH_ERROR: {e}", file=sys.stderr)
        return 1

    payload = to_brief(result) if args.brief else result.to_dict()
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
