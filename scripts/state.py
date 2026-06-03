"""Per-session state file lifecycle for /continuous skill.

File: ~/.claude/cache/continuous/<session_id>.json

The skill body MUST invoke read/write/delete via Bash with the
CONTINUOUS_SELF_PROBE=1 prefix so the PreToolUse hook bypasses these
operations (otherwise completion-time delete deadlocks against the very
guard the skill set up).

CLI:
  python state.py --create-fake --pid=99999 --age-days=8
  python state.py --reap
  python state.py --show <session_id>
  python state.py --list
  python state.py --init <session_id> <threshold> --task-stdin   # task via stdin (recommended)
  python state.py --transition <session_id> <action> [--inc <field>...] [--reset-retry]
  python state.py --delete <session_id>

The positional `--init <sid> <thr> <task>` form also exists but is argparse-
hostile to tasks beginning with `-` and unsafe with shell metacharacters when
invoked from the SKILL body. Prefer `--task-stdin`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import constants as C  # noqa: E402


STALE_AGE_DAYS = 7
LAST_ACTION_VALUES = {
    "initial", "initial_sleep", "work",
    "block_sleep", "retry_60s", "parse_fail_sleep", "resume",
}


@dataclass
class State:
    session_id: str
    threshold: int
    task: str
    pid: int
    created_at: str                  # ISO 8601 w/ TZ
    last_action: str = "initial"
    last_action_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    retry_60s: int = 0
    parse_fail_count: int = 0
    block_count: int = 0

    def __post_init__(self) -> None:
        if self.last_action not in LAST_ACTION_VALUES:
            raise ValueError(f"invalid last_action: {self.last_action}")
        if not (1 <= self.threshold <= 99):
            raise ValueError(f"threshold out of range: {self.threshold}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        # Tolerate unknown fields for forward compat; require core ones.
        required = {"session_id", "threshold", "task", "pid", "created_at"}
        missing = required - set(data)
        if missing:
            raise ValueError(f"state missing required fields: {sorted(missing)}")
        return cls(
            session_id=data["session_id"],
            threshold=int(data["threshold"]),
            task=data["task"],
            pid=int(data["pid"]),
            created_at=data["created_at"],
            last_action=data.get("last_action", "initial"),
            last_action_at=data.get(
                "last_action_at",
                datetime.now(timezone.utc).isoformat(),
            ),
            retry_60s=int(data.get("retry_60s", 0)),
            parse_fail_count=int(data.get("parse_fail_count", 0)),
            block_count=int(data.get("block_count", 0)),
        )


# ─── I/O ────────────────────────────────────────────────────────────────────

def write(state: State) -> Path:
    """Persist state atomically. Caller is responsible for SELF_PROBE prefix
    when this runs via Bash from the skill body.

    Uses tmp-file + os.replace so a crash mid-write cannot leave partial JSON
    that the hook would log as 'state unreadable, skipping'."""
    C.CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    path = C.state_file_for(state.session_id)
    state.last_action_at = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read(session_id: str) -> State | None:
    """Return state for this session, or None if missing/corrupt."""
    path = C.state_file_for(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, ValueError) as e:
        # Corrupt state file: treat as missing. Stale reap will clean up.
        print(f"[state] {session_id} unreadable, treating as missing: {e}",
              file=sys.stderr)
        return None
    try:
        return State.from_dict(data)
    except ValueError as e:
        print(f"[state] {session_id} schema mismatch: {e}", file=sys.stderr)
        return None


def delete(session_id: str) -> bool:
    """Remove the state file. Returns True on success or already-absent.
    On Windows file-lock / AV-hold failures, emits diagnostic but does NOT
    raise — next activation's stale reap will handle it."""
    path = C.state_file_for(session_id)
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as e:
        print(f"[state] delete failed for {session_id}: {e} "
              f"(will be reaped on next activation)",
              file=sys.stderr)
        return False


# ─── Stale reap ─────────────────────────────────────────────────────────────

def _is_stale(state: State, now_ts: float) -> tuple[bool, str]:
    """Return (stale?, reason).

    Note: PID liveness check is intentionally not used. State is written by
    an ephemeral `python -c` subprocess, so `state.pid` is dead immediately
    after write — that signal is unreliable. Age-based reap is the sole
    criterion: if a state file is older than STALE_AGE_DAYS, the conversation
    has either finished (DONE_MARKER missed) or been abandoned. The
    session_id is the real identity (matches transcript filename).
    """
    try:
        created_ts = datetime.fromisoformat(
            state.created_at.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return True, f"unparseable created_at: {state.created_at!r}"
    age_days = (now_ts - created_ts) / 86400
    if age_days > STALE_AGE_DAYS:
        return True, f"age {age_days:.1f}d > {STALE_AGE_DAYS}d"
    return False, ""


def reap() -> dict[str, Any]:
    """Scan state dir; remove stale entries. Returns summary."""
    if not C.CACHE_ROOT.exists():
        return {"scanned": 0, "reaped": 0, "remaining": 0, "details": []}

    now_ts = time.time()
    scanned = 0
    reaped: list[dict[str, Any]] = []
    remaining: list[str] = []

    for f in C.CACHE_ROOT.glob("*.json"):
        if f.name in C.NON_STATE_FILENAMES:
            continue
        scanned += 1
        sid = f.stem
        st = read(sid)
        if st is None:
            # Corrupt/unreadable — treat as stale.
            f.unlink(missing_ok=True)
            reaped.append({"session_id": sid, "reason": "unreadable"})
            continue
        is_stale, reason = _is_stale(st, now_ts)
        if is_stale:
            delete(sid)
            reaped.append({"session_id": sid, "reason": reason})
        else:
            remaining.append(sid)

    return {
        "scanned": scanned,
        "reaped": len(reaped),
        "remaining": len(remaining),
        "details": reaped,
    }


def list_states() -> list[str]:
    if not C.CACHE_ROOT.exists():
        return []
    return [
        f.stem for f in C.CACHE_ROOT.glob("*.json")
        if f.name not in C.NON_STATE_FILENAMES
    ]


# ─── CLI test harness ───────────────────────────────────────────────────────

def _create_fake(pid: int, age_days: float, session_id: str) -> Path:
    created = datetime.now(timezone.utc).timestamp() - age_days * 86400
    iso = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
    state = State(
        session_id=session_id,
        threshold=95,
        task="fake-test",
        pid=pid,
        created_at=iso,
        last_action="initial",
    )
    return write(state)


VALID_INC_FIELDS = {"block_count", "retry_60s", "parse_fail_count"}


def _init_state(session_id: str, threshold: int, task: str) -> Path:
    """Create a fresh state file for a first activation.

    Belt-and-suspenders: SKILL.md already refuses empty task at the args-parse
    layer, but if that contract ever drifts, an empty/whitespace-only task
    would persist a useless state file and waste a wake cycle.
    """
    if not task.strip():
        raise ValueError("task is empty or whitespace-only")
    st = State(
        session_id=session_id,
        threshold=threshold,
        task=task,
        pid=os.getpid(),
        created_at=datetime.now(timezone.utc).isoformat(),
        last_action="initial",
    )
    return write(st)


def _transition(session_id: str, action: str,
                inc: list[str], reset_retry: bool) -> dict[str, Any] | None:
    """Atomic state mutation. Returns updated state dict, or None if missing."""
    st = read(session_id)
    if st is None:
        return None
    if action not in LAST_ACTION_VALUES:
        raise ValueError(f"invalid action: {action}")
    st.last_action = action
    if reset_retry:
        st.retry_60s = 0
    for field_name in inc:
        if field_name not in VALID_INC_FIELDS:
            raise ValueError(f"cannot inc field: {field_name}")
        setattr(st, field_name, getattr(st, field_name) + 1)
    write(st)
    return st.to_dict()


def _cli() -> int:
    p = argparse.ArgumentParser(description="State lifecycle test harness")
    p.add_argument("--create-fake", action="store_true")
    p.add_argument("--pid", type=int, default=99999)
    p.add_argument("--age-days", type=float, default=0.0)
    p.add_argument("--session-id", default="test-session-aaaaaaaa")
    p.add_argument("--reap", action="store_true")
    p.add_argument("--show", metavar="SID")
    p.add_argument("--list", action="store_true")
    p.add_argument("--delete", metavar="SID")
    p.add_argument("--init", nargs="+", metavar="ARG",
                   help="create initial state file. Forms: "
                        "--init SID THRESHOLD TASK  (positional, unsafe with shell metacharacters), or "
                        "--init SID THRESHOLD --task-stdin  (recommended; reads task from stdin)")
    p.add_argument("--task-stdin", action="store_true",
                   help="with --init: read task from stdin instead of as a positional arg")
    p.add_argument("--transition", nargs=2, metavar=("SID", "ACTION"),
                   help="set last_action; pair with --inc/--reset-retry")
    p.add_argument("--inc", action="append", default=[],
                   help="counter to increment (block_count|retry_60s|parse_fail_count); "
                        "may be repeated")
    p.add_argument("--reset-retry", action="store_true",
                   help="zero retry_60s during the transition")
    args = p.parse_args()

    if args.init:
        if args.task_stdin:
            if len(args.init) != 2:
                p.error("--init with --task-stdin takes exactly 2 args: SID THRESHOLD")
            sid, thr_s = args.init
            # Strip trailing newline that POSIX heredocs append before
            # the EOF terminator. Do NOT use .strip() — leading whitespace
            # is legal in tasks and must be preserved.
            # Read raw bytes and decode UTF-8 explicitly: on Windows the default
            # stdin text encoding (cp949/mbcs) mangles the UTF-8 heredoc bytes of
            # a non-ASCII task into lone surrogates, which later trip an API
            # content filter when the task rides into the wake prompt.
            task = sys.stdin.buffer.read().decode("utf-8").rstrip("\n")
        else:
            if len(args.init) != 3:
                p.error("--init takes exactly 3 args: SID THRESHOLD TASK")
            sid, thr_s, task = args.init
        path = _init_state(sid, int(thr_s), task)
        print(json.dumps({"ok": True, "path": str(path), "session_id": sid}))
        return 0
    if args.transition:
        sid, action = args.transition
        result = _transition(sid, action, args.inc, args.reset_retry)
        if result is None:
            print(json.dumps({"ok": False, "reason": "state missing"}),
                  file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "state": result}))
        return 0
    if args.create_fake:
        path = _create_fake(args.pid, args.age_days, args.session_id)
        print(f"created: {path}")
        return 0
    if args.reap:
        result = reap()
        print(json.dumps(result, indent=2))
        return 0
    if args.show:
        st = read(args.show)
        print(json.dumps(st.to_dict(), indent=2) if st else "null")
        return 0
    if args.list:
        for sid in list_states():
            print(sid)
        return 0
    if args.delete:
        ok = delete(args.delete)
        print("deleted" if ok else "delete failed (see stderr)")
        return 0 if ok else 1

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
