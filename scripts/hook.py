"""PreToolUse hook for /continuous skill.

Emits a single-line ASCII marker on stderr and exits 2 when the active
session's utilization exceeds its threshold. Otherwise exits 0.

Bypass priority (first match wins → exit 0):
  1. tool_input.command contains TEST_BYPASS_ENV=1   (test mode)
  2. tool_input.command contains SELF_PROBE_ENV=1    (skill self-ops)
  3. tool_name == "ScheduleWakeup"                   (self-block prevention)
  4. No active session state file                    (unrelated session)

Wrapped in try/except: any internal error emits a diagnostic to stderr but
exits 0 — the hook MUST NOT block the user's work on its own bugs.

CLI self-test:
  python hook.py --self-test --mock-util 96 --mock-axis five_hour --mock-reset 1800
  CONTINUOUS_TEST_BYPASS=1 python hook.py --self-test --mock-util 99
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import constants as C  # noqa: E402


# ─── Bypass detection ───────────────────────────────────────────────────────

def _command_string(tool_input: dict) -> str:
    """Best-effort extraction of the Bash command string from tool_input."""
    if not isinstance(tool_input, dict):
        return ""
    return str(tool_input.get("command", ""))


def _leading_env_names(cmd: str) -> set[str]:
    """Names of env vars assigned at the start of `cmd`. Empty if no leading
    assignments. Walks variables one at a time."""
    names: set[str] = set()
    pos = 0
    pat = re.compile(r"\s*([A-Z_][A-Z0-9_]*)=\S+\s+")
    while True:
        m = pat.match(cmd, pos)
        if not m:
            break
        names.add(m.group(1))
        pos = m.end()
    return names


def _bypass_reason(tool_name: str, tool_input: dict) -> str | None:
    cmd = _command_string(tool_input)
    # Env-var form (works for hook subprocess inheriting parent env)
    if os.environ.get(C.TEST_BYPASS_ENV) == "1":
        return "test_bypass_env"
    if os.environ.get(C.SELF_PROBE_ENV) == "1":
        return "self_probe_env"
    # Inline-prefix form (works for skill body's Bash commands).
    # Only match assignments at the START of the command — quoted/echoed
    # mentions of the env var inside the body do NOT count.
    leading = _leading_env_names(cmd)
    if C.TEST_BYPASS_ENV in leading:
        return "test_bypass"
    if C.SELF_PROBE_ENV in leading:
        return "self_probe"
    if tool_name == "ScheduleWakeup":
        return "schedule_wakeup"
    return None


# ─── Active session lookup ──────────────────────────────────────────────────

def _find_active_state_file(payload_session_id: str | None) -> Path | None:
    """Locate the state file for the current Claude Code session.

    Strategy:
      - If payload provides session_id, use that filename.
      - Else: scan cache dir; if exactly one state file exists, use it
        (single-session machine — the common case). With multiple, we can't
        disambiguate without payload help, so return None (no blocking).
    """
    if not C.CACHE_ROOT.exists():
        return None
    if payload_session_id:
        f = C.state_file_for(payload_session_id)
        return f if f.exists() else None
    candidates = [
        f for f in C.CACHE_ROOT.glob("*.json")
        if f.name not in C.NON_STATE_FILENAMES
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


# ─── Marker emit ────────────────────────────────────────────────────────────

def _emit_block(axis: str, util: float, reset_remaining_sec: int) -> int:
    line = C.BLOCK_MARKER_FORMAT.format(
        axis=axis, util=util, reset_remaining_sec=reset_remaining_sec
    )
    print(line, file=sys.stderr)
    return 2


# ─── Threshold check ────────────────────────────────────────────────────────

def _check(threshold: int, mock: dict | None = None) -> tuple[str, float, int] | None:
    """Returns (axis, util, reset_remaining_sec) if over threshold, else None.

    `mock` is for self-test and must contain {axis, util, reset}.
    """
    if mock is not None:
        ax, ut, rs = mock["axis"], float(mock["util"]), int(mock["reset"])
        if ut >= threshold:
            return ax, ut, rs
        return None

    # Live fetch. usage_fetch will call urllib directly (not via Claude tools),
    # so no recursion risk.
    import usage_fetch  # noqa: WPS433
    try:
        u = usage_fetch.fetch_usage(use_cache=True)
    except usage_fetch.UsageFetchError as e:
        # Hook itself does not block on fetch failure — let work continue.
        # Emit a diagnostic so it surfaces; the skill body decides what to do.
        print(f"[continuous-hook] usage fetch failed: {e}", file=sys.stderr)
        return None

    # 7d uses the hard terminal constant (not user threshold).
    # 5h uses user-supplied threshold.
    if u.seven_day_util >= C.SEVEN_DAY_THRESHOLD:
        return ("seven_day", u.seven_day_util,
                u.seven_day_reset_remaining_sec or 0)
    if u.five_hour_util >= threshold:
        return ("five_hour", u.five_hour_util,
                u.five_hour_reset_remaining_sec)
    return None


# ─── Main ───────────────────────────────────────────────────────────────────

def _main_from_payload(payload: dict, mock: dict | None = None) -> int:
    tool_name = payload.get("tool_name") or payload.get("tool") or ""
    tool_input = payload.get("tool_input") or payload.get("input") or {}

    # 1+2+3: bypass paths
    reason = _bypass_reason(tool_name, tool_input)
    if reason is not None:
        return 0

    # 4: no active state
    state_file = _find_active_state_file(payload.get("session_id"))
    if state_file is None:
        return 0

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        threshold = int(state["threshold"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[continuous-hook] state unreadable, skipping: {e}",
              file=sys.stderr)
        return 0

    over = _check(threshold, mock=mock)
    if over is None:
        return 0
    axis, util, reset_remaining = over
    return _emit_block(axis, util, reset_remaining)


def main() -> int:
    # CLI self-test path
    if "--self-test" in sys.argv:
        from datetime import datetime, timezone
        p = argparse.ArgumentParser()
        p.add_argument("--self-test", action="store_true")
        p.add_argument("--mock-util", type=float, required=True)
        p.add_argument("--mock-axis", default="five_hour")
        p.add_argument("--mock-reset", type=int, default=1800)
        p.add_argument("--threshold", type=int, default=95)
        p.add_argument("--cmd", default="echo test",
                       help="simulated tool_input.command")
        p.add_argument("--tool", default="Bash",
                       help="simulated tool_name")
        a = p.parse_args()
        # session_id pins lookup to the fake state file — without it, the
        # test fails when other real /continuous state files are present.
        fake_sid = "self-test-aaaa"
        payload = {
            "tool_name": a.tool,
            "tool_input": {"command": a.cmd},
            "session_id": fake_sid,
        }
        # Set up a fake state file for the duration of the test.
        C.CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        fake_state = {
            "session_id": fake_sid,
            "threshold": a.threshold,
            "task": "self-test",
            "pid": os.getpid(),
            "created_at": now_iso,
            "last_action": "initial",
            "last_action_at": now_iso,
            "retry_60s": 0,
            "parse_fail_count": 0,
            "block_count": 0,
        }
        # Honor TEST_BYPASS env: that supersedes everything (consistent with
        # the bypass spec).
        if os.environ.get(C.TEST_BYPASS_ENV) == "1":
            return 0
        path = C.state_file_for(fake_sid)
        path.write_text(json.dumps(fake_state), encoding="utf-8")
        try:
            return _main_from_payload(
                payload,
                mock={
                    "axis": a.mock_axis,
                    "util": a.mock_util,
                    "reset": a.mock_reset,
                },
            )
        finally:
            path.unlink(missing_ok=True)

    # Production path: read JSON payload from stdin.
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:  # noqa: BLE001
        # Hook own bug → diagnostic + pass-through
        print(f"[continuous-hook] payload parse failed: {e}", file=sys.stderr)
        return 0

    try:
        return _main_from_payload(payload)
    except Exception as e:  # noqa: BLE001
        print(f"[continuous-hook] internal error: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
