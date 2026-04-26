"""Single source of truth for /continuous skill constants.

All other modules (hook.py, usage_fetch.py, state.py) import from here.
SKILL.md references these by name; do not hardcode the values elsewhere.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# ─── Wake / marker contract ─────────────────────────────────────────────────

WAKE_PROMPT_FORMAT = "/loop /continuous {threshold} {task}"
"""ScheduleWakeup `prompt`. The `/loop ` prefix is REQUIRED — without it the
wake turn exits /loop dynamic mode and ScheduleWakeup tool becomes unavailable
(silent chain failure)."""

BLOCK_MARKER_REGEX = re.compile(
    r"^\[CONTINUOUS_BLOCKED\] axis=(?P<axis>\w+) "
    r"util=(?P<util>[\d.]+) reset_remaining_sec=(?P<reset>-?\d+)\s*$"
)
"""Hook stderr first-line contract. ASCII-only; field order fixed.
Skill body parses with this regex; named groups are the parse contract.
Mismatch ⇒ delaySeconds fallback = MAX_SLEEP_SEC, parse_fail_count++."""

BLOCK_MARKER_FORMAT = (
    "[CONTINUOUS_BLOCKED] axis={axis} util={util:.1f} "
    "reset_remaining_sec={reset_remaining_sec}"
)
"""Format for emit-side. Hook uses
`BLOCK_MARKER_FORMAT.format(...)`. Round-trip with BLOCK_MARKER_REGEX
must hold; the self-test verifies this."""

DONE_MARKER = "[CONTINUOUS_DONE]"
"""Emitted by skill body on its OWN line (after .strip(), the line must
equal DONE_MARKER exactly). Substring matches inside prose / code fences
do NOT count."""

SELF_PROBE_ENV = "CONTINUOUS_SELF_PROBE"
"""Env-var name. Skill prefixes its OWN Bash commands with
`CONTINUOUS_SELF_PROBE=1 …` for: usage_fetch probes, state read/write,
state delete. Hook detects this in tool_input.command and bypasses
(exit 0). Without this, completion-time state deletion would deadlock
against the hook's own block."""

TEST_BYPASS_ENV = "CONTINUOUS_TEST_BYPASS"
"""Test-mode escape hatch. Independent of SELF_PROBE_ENV."""

# ─── Sleep clamps ────────────────────────────────────────────────────────────

MIN_SLEEP_SEC = 60
"""Floor for ScheduleWakeup delaySeconds. Protects against negative
reset_remaining_sec (stale cache, clock skew)."""

MAX_SLEEP_SEC = 3600
"""ScheduleWakeup hard ceiling per the harness."""

SEVEN_DAY_THRESHOLD = 99
"""Hard terminal threshold for the 7-day axis. The user-supplied threshold
applies ONLY to 5h. 7d uses this constant because:
  - 7d reset is up to 7 days away — sleep-and-retry is wasteful
  - 99% means truly exhausted; below that, even slightly, is normal
  - User of /continuous 95 should not be terminated when their 7d sits
    at 50% — those are different concerns"""


def clamp_sleep(reset_remaining_sec: int, buffer: int = 60) -> int:
    """Single clamp formula used by all sleep branches.

    delay = clamp(reset_remaining_sec + buffer, [MIN_SLEEP_SEC, MAX_SLEEP_SEC])
    """
    return max(MIN_SLEEP_SEC, min(reset_remaining_sec + buffer, MAX_SLEEP_SEC))


# ─── Filesystem layout ──────────────────────────────────────────────────────

CACHE_ROOT = Path.home() / ".claude" / "cache" / "continuous"
"""Per-session state + ttl_health throttle file. Skill-private runtime root."""

USAGE_CACHE_ROOT = Path.home() / ".claude" / "cache" / "usage"
"""Shared OAuth usage cache + lock + policy file. Read by both this skill's
usage_fetch.py and the user's private statusline.py — co-located here so the
two consumers see the same cache (preventing duplicate HTTP fetches and
write races on overlapping windows). The directory and its files are
per-machine runtime artifacts; clones of this repo do not ship them."""

USAGE_CACHE_FILE = USAGE_CACHE_ROOT / "oauth-usage.json"
"""Disk cache for /api/oauth/usage responses. TTL is USAGE_CACHE_TTL_SEC.
Written atomically via tempfile + os.replace; consumed by usage_fetch.py
and statusline.py."""

USAGE_LOCK_FILE = USAGE_CACHE_ROOT / "oauth-usage.lock"
"""Lock to coalesce concurrent fetches; TTL is USAGE_LOCK_TTL_SEC.
Acquired via O_CREAT|O_EXCL in usage_fetch.py."""

USAGE_POLICY_FILE = USAGE_CACHE_ROOT / ".policy.json"
"""Optional per-user override for cache_ttl_sec / lock_ttl_sec. If absent
or unreadable, fallback to baked-in defaults (60 / 30). Loaded once at
module import — changes require process restart."""

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
"""OAuth token source. Reloaded on every fetch (no token caching).
Claude Code's main process is the refresh authority."""


def state_file_for(session_id: str) -> Path:
    """Path to the per-session state file."""
    return CACHE_ROOT / f"{session_id}.json"


NON_STATE_FILENAMES = frozenset({"ttl_health.json"})
"""CACHE_ROOT files that are NOT per-session state. State scanners must
skip these — otherwise reap() destroys the throttle file (ttl_health.json)
and hook._find_active_state_file picks it up as a phantom session.
The OAuth usage cache/lock previously lived in CACHE_ROOT but moved to
USAGE_CACHE_ROOT, so they no longer need protection here."""


# ─── HTTP / API ─────────────────────────────────────────────────────────────

USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_API_HEADERS_TEMPLATE = {
    # Update if Anthropic publishes a new oauth beta version — symptom of
    # mismatch is unexpected 401/400 from /api/oauth/usage.
    "anthropic-beta": "oauth-2025-04-20",
    "User-Agent": "continuous-skill/1.0",
}
USAGE_API_TIMEOUT_SEC = 8


def _load_policy() -> dict:
    """Read optional .policy.json override. Returns empty dict on any failure
    — caller falls back to baked-in defaults. Loaded once at module import
    time (changes require process restart)."""
    try:
        return json.loads(USAGE_POLICY_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}


_POLICY = _load_policy()

USAGE_CACHE_TTL_SEC = int(_POLICY.get("cache_ttl_sec", 60))
"""Disk cache TTL. Default 60s. Override via
~/.claude/cache/usage/.policy.json key 'cache_ttl_sec'."""

USAGE_LOCK_TTL_SEC = int(_POLICY.get("lock_ttl_sec", 30))
"""Lock TTL. Default 30s. Override via .policy.json key 'lock_ttl_sec'."""


# ─── Self-test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    items = {
        "WAKE_PROMPT_FORMAT": WAKE_PROMPT_FORMAT,
        "BLOCK_MARKER_REGEX": BLOCK_MARKER_REGEX.pattern,
        "DONE_MARKER": DONE_MARKER,
        "SELF_PROBE_ENV": SELF_PROBE_ENV,
        "TEST_BYPASS_ENV": TEST_BYPASS_ENV,
        "MIN_SLEEP_SEC": MIN_SLEEP_SEC,
        "MAX_SLEEP_SEC": MAX_SLEEP_SEC,
        "CACHE_ROOT": str(CACHE_ROOT),
        "USAGE_CACHE_ROOT": str(USAGE_CACHE_ROOT),
        "USAGE_CACHE_FILE": str(USAGE_CACHE_FILE),
        "USAGE_LOCK_FILE": str(USAGE_LOCK_FILE),
        "USAGE_POLICY_FILE": str(USAGE_POLICY_FILE),
        "USAGE_CACHE_TTL_SEC": USAGE_CACHE_TTL_SEC,
        "USAGE_LOCK_TTL_SEC": USAGE_LOCK_TTL_SEC,
        "CREDENTIALS_FILE": str(CREDENTIALS_FILE),
        "USAGE_API_URL": USAGE_API_URL,
    }
    for k, v in items.items():
        print(f"{k} = {v!r}")
    assert clamp_sleep(0) == MIN_SLEEP_SEC
    assert clamp_sleep(-1000) == MIN_SLEEP_SEC
    assert clamp_sleep(10_000) == MAX_SLEEP_SEC
    assert clamp_sleep(1800) == 1860
    print("clamp_sleep: OK")
    emitted = BLOCK_MARKER_FORMAT.format(
        axis="five_hour", util=96.0, reset_remaining_sec=1800
    )
    m = BLOCK_MARKER_REGEX.match(emitted)
    assert m is not None, f"regex failed to match emit: {emitted!r}"
    assert m.group("axis") == "five_hour"
    assert m.group("util") == "96.0"
    assert m.group("reset") == "1800"
    print("marker round-trip: OK")
