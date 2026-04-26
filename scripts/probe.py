"""Unified probe for /continuous skill body.

Combines loop_check + state read + usage fetch + TTL health into a single
JSON object the skill body parses to decide its branch.

The skill body invokes this in two modes:
  - first activation:  python probe.py            (does loop_check)
  - wake turn:         python probe.py --wake     (skips loop_check;
                                                   state existence already
                                                   implies /loop context)

Output schema:

    {
      "sid": "...",                       # transcript session id (always)
      "turn_type": "first"|"wake"|"unknown",
      "in_loop": true|false|null,         # null when skipped (--wake)
      "loop_reason": "...",               # explain string from loop_check
      "state": {...}|null,                # full state dict, or null on first
      "usage": {                          # 5-field brief, see usage_fetch.to_brief
        "five_hour_util": ...,
        "five_hour_reset_remaining_sec": ...,
        "seven_day_util": ...,
        "seven_day_resets_at": "..."|null,
        "seven_day_reset_remaining_sec": ...|null
      } | null,                           # null only on usage_fetch error
      "ttl_health": {...}|null,           # cache TTL health, throttled to ~30 days
      "errors": ["..."]                   # non-fatal errors per subsystem
    }

Exit code is always 0 unless the unified probe itself crashes — the skill
body inspects errors[] and decides per-axis (so a usage 401 doesn't kill
the loop_check signal).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import constants as C  # noqa: E402
import loop_check as L  # noqa: E402
import measure_cache_ttl as M  # noqa: E402
import state as S  # noqa: E402
import usage_fetch as U  # noqa: E402


TTL_HEALTH_CACHE = C.CACHE_ROOT / "ttl_health.json"
TTL_HEALTH_TTL_SEC = 30 * 86400  # re-check at most once per 30 days


def _ttl_health_throttled(errors: list[str]) -> dict | None:
    """Run measure_cache_ttl.health_check at most once per 30 days.
    Cached result lives at ~/.claude/cache/continuous/ttl_health.json.

    `should_notify` semantics: true ONLY on the turn the measurement was
    freshly taken AND status is "regression". Cached reads always return
    false. Throttle (30 days) becomes the natural notification cool-off —
    one push per regression episode, not one per wake.

    Returns None on internal failure (non-fatal — skill continues)."""
    try:
        if TTL_HEALTH_CACHE.exists():
            age = time.time() - TTL_HEALTH_CACHE.stat().st_mtime
            # Negative age (clock skew) is treated as expired — re-measure
            # rather than trust a future-mtime file.
            if 0 <= age < TTL_HEALTH_TTL_SEC:
                cached = json.loads(TTL_HEALTH_CACHE.read_text(encoding="utf-8"))
                cached["should_notify"] = False  # cached read never re-notifies
                return cached
        result = M.health_check(transcripts=10)
        result["should_notify"] = (result.get("status") == "regression")
        C.CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        # Atomic write: a crash mid-write would otherwise leave partial JSON
        # that the next probe treats as cached and ignores re-measurement.
        tmp = TTL_HEALTH_CACHE.with_suffix(TTL_HEALTH_CACHE.suffix + ".tmp")
        tmp.write_text(json.dumps(result), encoding="utf-8")
        os.replace(tmp, TTL_HEALTH_CACHE)
        return result
    except Exception as e:  # noqa: BLE001
        errors.append(f"ttl_health: {e}")
        return None


def _do_loop_check(sid: str | None) -> tuple[bool | None, str]:
    if sid is None:
        return False, "no sid"
    try:
        return L.is_in_loop(sid)
    except Exception as e:  # noqa: BLE001
        return False, f"loop_check raised: {e}"


def _pick_state(sid: str | None, errors: list[str]) -> tuple[dict | None, str]:
    """Returns (state_dict | None, turn_type).

    turn_type:
      - 'first' if no state file matches the current sid
      - 'wake' if a state file matches sid exactly
      - 'unknown' if sid is missing entirely (caller decides)

    Strict sid matching: a state file from a different (leaked) session is
    NOT treated as "wake" for this conversation. Stale leaks must be cleaned
    by reap, not silently picked up.
    """
    sids = S.list_states()
    if not sids:
        return None, "first"
    if sid is None:
        errors.append(f"sid unknown but state files present: {sids}")
        return None, "unknown"
    if sid in sids:
        st = S.read(sid)
        if st is None:
            errors.append(f"state file for {sid} unreadable")
            return None, "wake"
        return st.to_dict(), "wake"
    # Other sessions' state files exist but ours doesn't → first activation
    # for this conversation. Other sids will be reaped by age policy.
    return None, "first"


def _do_usage(errors: list[str]) -> dict | None:
    try:
        result = U.fetch_usage(use_cache=True)
    except U.UsageFetchError as e:
        errors.append(f"usage fetch: {e}")
        return None
    return U.to_brief(result)


def main() -> int:
    p = argparse.ArgumentParser(description="Unified /continuous probe")
    p.add_argument("--wake", action="store_true",
                   help="skip loop_check (caller is on a wake turn — state "
                        "existence already implies /loop context)")
    args = p.parse_args()

    errors: list[str] = []
    sid = L.discover_session_id()
    if sid is None:
        errors.append("no transcript found")

    if args.wake:
        in_loop: bool | None = None
        reason = "skipped (--wake)"
    else:
        in_loop, reason = _do_loop_check(sid)

    state_dict, turn_type = _pick_state(sid, errors)
    usage = _do_usage(errors)
    ttl_health = _ttl_health_throttled(errors)

    payload = {
        "sid": sid,
        "turn_type": turn_type,
        "in_loop": in_loop,
        "loop_reason": reason,
        "state": state_dict,
        "usage": usage,
        "ttl_health": ttl_health,
        "errors": errors,
    }
    print(json.dumps(payload, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
