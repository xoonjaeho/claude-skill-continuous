"""Detect whether the current turn is running inside `/loop` dynamic mode.

The naive check (ScheduleWakeup tool availability) is unreliable because
deferred tools stay loaded for the rest of the session even after /loop
ends.

Empirical observation from transcript layout: a slash-command invocation
loads the SKILL body as a `type=user, role=user, isMeta=True` text block.
When user types `/loop /continuous 95 task`:
    ... → /loop SKILL body (isMeta) → assistant Skill(continuous) → /continuous SKILL body (isMeta) → ...
When user types `/continuous 95 task` directly:
    ... → /continuous SKILL body (isMeta) → ...

So: walk back from the latest user-text isMeta block (= the /continuous
activation we're in), find the PREVIOUS user-text block. If that previous
block is the /loop SKILL body, we're in /loop mode.

CLI:
  python loop_check.py <session_id>            # exit 0 = in loop, 1 = not
  python loop_check.py <session_id> --explain  # diagnostic
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

LOOP_BODY_HEADER = "# /loop — schedule a recurring or self-paced prompt"
"""Unique header line of the /loop SKILL.md body. Presence in a recent
user-text isMeta block means /loop activated this turn cycle."""

CONTINUOUS_BODY_MARKER = "/continuous — session continuity"
"""Substring that uniquely identifies the /continuous SKILL.md body."""


def _find_transcript(session_id: str) -> Path | None:
    pattern = str(Path.home() / ".claude" / "projects" / "*" / f"{session_id}.jsonl")
    matches = glob.glob(pattern)
    if not matches:
        return None
    # Pick the most recently modified (handles edge case of duplicates).
    return Path(max(matches, key=os.path.getmtime))


def discover_session_id() -> str | None:
    """Find the current session by picking the most recently modified
    transcript file under ~/.claude/projects/*/*.jsonl.

    Note: this is the *transcript* session ID (matches the .jsonl filename),
    NOT the value inside ~/.claude/sessions/<pid>.json — those are different
    UUIDs in current Claude Code builds.
    """
    pattern = str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl")
    matches = glob.glob(pattern)
    if not matches:
        return None
    newest = max(matches, key=os.path.getmtime)
    return Path(newest).stem


def _iter_lines_reverse(path: Path, max_lines: int = 200):
    """Yield up to max_lines from the END of a JSONL file, newest first."""
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in reversed(lines[-max_lines:]):
        yield line


def _extract_text(msg) -> str:
    """Extract a text representation from a transcript message.content.

    IMPORTANT: tool_result blocks are skipped — they appear as user-role
    messages but represent tool output, not the user's actual prompt.
    """
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        parts: list[str] = []
        for block in msg:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_result":
                continue  # NOT the user's prompt
            if block_type == "text" or block_type is None:
                t = block.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    if isinstance(msg, dict):
        return _extract_text(msg.get("content", ""))
    return ""


def _iter_user_text_blocks(path: Path, max_lines: int = 500):
    """Yield (entry, text) for each user-role text-block message, newest first.
    Includes isMeta entries (slash command bodies). Skips tool_results and
    attachments."""
    for line in _iter_lines_reverse(path, max_lines=max_lines):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.get("type") != "user":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _extract_text(msg.get("content", ""))
        if not text.strip():
            continue
        yield entry, text


def is_in_loop(session_id: str) -> tuple[bool, str]:
    """Return (in_loop, reason). Walk back through user-text blocks:

      1. Find the most recent block — must be the /continuous SKILL body
         (we're invoked from it).
      2. Find the block IMMEDIATELY before it. If it's the /loop SKILL body
         → in loop. Else (real user msg, distant /loop, or absent) → not.
    """
    path = _find_transcript(session_id)
    if path is None:
        return False, "no transcript found for session"

    iterator = _iter_user_text_blocks(path)
    try:
        _, current = next(iterator)
    except StopIteration:
        return False, "no user-text blocks in transcript"

    if CONTINUOUS_BODY_MARKER not in current:
        # Latest user text isn't the /continuous body — we shouldn't even
        # be running. Conservative: not in loop.
        return False, "latest user-text is not /continuous SKILL body"

    try:
        _, prev = next(iterator)
    except StopIteration:
        return False, "no user-text block before /continuous body"

    if LOOP_BODY_HEADER in prev:
        return True, "preceding user-text block is /loop SKILL body"
    return False, ("preceding user-text block is not /loop SKILL body "
                   f"(head={prev[:80]!r})")


# ─── CLI ────────────────────────────────────────────────────────────────────

def _cli() -> int:
    p = argparse.ArgumentParser(description="Loop-context detector")
    p.add_argument("session_id", nargs="?",
                   help="transcript session UUID (auto-discovered if omitted)")
    p.add_argument("--explain", action="store_true",
                   help="print decision reasoning")
    args = p.parse_args()

    sid = args.session_id or discover_session_id()
    if not sid:
        print("ERROR: no transcript found", file=sys.stderr)
        return 2

    in_loop, reason = is_in_loop(sid)
    if args.explain:
        print(f"sid:     {sid}")
        print(f"in_loop: {in_loop}")
        print(f"reason:  {reason}")
    return 0 if in_loop else 1


if __name__ == "__main__":
    sys.exit(_cli())
