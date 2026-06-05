"""Measure prompt-cache TTL behavior in this user's transcripts.

Three signals:

  (a) Write-tier ratio — `cache_creation.ephemeral_1h_input_tokens` vs
      `cache_creation.ephemeral_5m_input_tokens`. The ONLY direct evidence
      of which TTL tier Anthropic is granting. `cache_read_input_tokens` is
      a single flat field — per-tier read breakdown is not exposed.

  (b) Wake-turn cache hit — for assistant turns immediately following a
      `/loop /continuous …` user message, the ratio of `cache_read_input_tokens`
      to `input_tokens`. Large ratio = cache survived the sleep window. NOTE:
      unreliable for the boundary-miss mode — see (c).

  (c) Wake-turn re-write fraction — `eph_1h / (cache_read + eph_1h)`, computed
      PER wake (worst wake, not aggregated — an intermittent miss must not be
      diluted). Targets the boundary-miss mode that (b) is blind to: when a
      sleep overruns the 1h TTL the context expires and is RE-WRITTEN to the 1h
      tier on wake — `eph_1h` spikes toward full-context size while
      `input_tokens` stays tiny, so (b) stays high and looks healthy (a measured
      3649s miss scored 12,854 on (b) yet re-wrote ~80% of context). A wake
      fraction > 0.5 flags it; remedy is to lower MAX_SLEEP_SEC below the 1h TTL.
      DETECTION: a wake lands as a user entry with isMeta=True whose text starts
      with "/loop /continuous" (validated against real ScheduleWakeup injections;
      doc bodies are isMeta too but start with headers, so startswith excludes
      them, and typed first-activations do not produce such an entry — so no
      cold-cache false positive). Not yet exercised end-to-end on a completed
      `/loop /continuous` wake, so treat the first live numbers as first-run data.

Run periodically (e.g. monthly) to detect regression. Anthropic has silently
flipped default cache TTL behavior in the past, and certain telemetry-disable
configurations are known to break the 1h tier. If write_ratio drops below ~5:1,
wake-turn re-write fraction exceeds ~0.5, or wake-turn read/input collapses,
cache is no longer surviving sleeps.

CLI:
  python measure_cache_ttl.py                # human-readable report
  python measure_cache_ttl.py --json         # raw scan data
  python measure_cache_ttl.py --health       # compact health JSON (used by probe.py)
  python measure_cache_ttl.py --transcripts 20
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path


def _scan(transcripts: int) -> dict:
    files = glob.glob(str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl"))
    files.sort(key=os.path.getmtime, reverse=True)
    files = files[:transcripts]

    totals = {"1h_write": 0, "5m_write": 0, "entries": 0}
    wakes: list[dict] = []
    files_scanned: list[str] = []

    for f in files:
        files_scanned.append(os.path.basename(f))
        prev_was_loop_continuous = False
        try:
            with open(f, encoding="utf-8") as fp:
                for line in fp:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    totals["entries"] += 1
                    msg = e.get("message") or {}
                    usage = msg.get("usage") or {}
                    cc = usage.get("cache_creation") or {}
                    totals["1h_write"] += cc.get("ephemeral_1h_input_tokens", 0)
                    totals["5m_write"] += cc.get("ephemeral_5m_input_tokens", 0)

                    # Wake-turn detection
                    content = msg.get("content", "")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") in ("text", None):
                                text += b.get("text", "") or ""
                    # Wake detection. A ScheduleWakeup-fired wake is injected
                    # as a user entry with isMeta=True whose content IS the wake
                    # prompt — so its text STARTS WITH the WAKE_PROMPT_FORMAT
                    # prefix "/loop /continuous" (verified against real wake
                    # injections in transcripts). SKILL doc bodies are also
                    # isMeta=True and mention "/loop /continuous" in prose, but
                    # start with frontmatter/headers, so startswith (not the old
                    # substring `in`) excludes them. The earlier `not isMeta`
                    # test was inverted — it dropped every real wake (all are
                    # isMeta=True), which is why no wake ever scored.
                    if (e.get("type") == "user"
                            and e.get("isMeta")
                            and text.strip().startswith("/loop /continuous")):
                        prev_was_loop_continuous = True
                        continue
                    if prev_was_loop_continuous and e.get("type") == "assistant":
                        wakes.append({
                            "input": usage.get("input_tokens", 0),
                            "read": usage.get("cache_read_input_tokens", 0),
                            "eph1h": cc.get("ephemeral_1h_input_tokens", 0),
                        })
                        prev_was_loop_continuous = False
        except OSError:
            continue

    return {"totals": totals, "wakes": wakes, "files": files_scanned}


def _report(data: dict) -> str:
    t = data["totals"]
    wakes = data["wakes"]
    write_ratio = t["1h_write"] / max(t["5m_write"], 1)

    rewrite_frac = 0.0
    if wakes:
        avg_input = sum(w["input"] for w in wakes) / len(wakes)
        avg_read = sum(w["read"] for w in wakes) / len(wakes)
        per_wake_frac = [w.get("eph1h", 0) / max(w["read"] + w.get("eph1h", 0), 1)
                         for w in wakes]
        rewrite_frac = max(per_wake_frac)  # worst wake, not aggregate (no dilution)
        miss_wakes = sum(1 for fr in per_wake_frac if fr > 0.5)
        hits = sum(1 for w in wakes if w["read"] > w["input"])
        wake_summary = (
            f"wake-turn assistant entries: {len(wakes)} "
            f"(cache hits: {hits}/{len(wakes)})\n"
            f"  avg input_tokens     = {avg_input:>10.0f}\n"
            f"  avg cache_read_input = {avg_read:>10.0f}\n"
            f"  read/input ratio     = {avg_read / max(avg_input, 1):>10.0f}\n"
            f"  worst re-write frac  = {rewrite_frac:>10.1%}  ({miss_wakes}/{len(wakes)} wakes >50% = boundary miss)"
        )
    else:
        wake_summary = "wake-turn assistant entries: 0 (no /loop /continuous activity yet)"

    health = "OK"
    notes = []
    if write_ratio < 5:
        health = "REGRESSION SUSPECTED"
        notes.append("write_ratio < 5: 1h TTL may not be applied")
    if wakes and (sum(w["read"] for w in wakes) / max(sum(w["input"] for w in wakes), 1)) < 10:
        health = "REGRESSION SUSPECTED"
        notes.append("wake-turn read/input < 10: cache not surviving sleeps")
    if rewrite_frac > 0.5:
        health = "REGRESSION SUSPECTED"
        notes.append("wake-turn re-write > 50%: sleep overrunning 1h TTL (boundary miss)")

    out = [
        f"transcripts scanned : {len(data['files'])}",
        f"total entries       : {t['entries']}",
        f"cache_creation tokens (write tier):",
        f"  ephemeral_1h_input  = {t['1h_write']:>12,}",
        f"  ephemeral_5m_input  = {t['5m_write']:>12,}",
        f"  write tier ratio    = {write_ratio:>12.1f}  (1h : 5m)",
        wake_summary,
        f"health              : {health}",
    ]
    for n in notes:
        out.append(f"  - {n}")
    return "\n".join(out)


def health_check(transcripts: int = 10) -> dict:
    """Compact result for programmatic use (probe.py throttled call).

    Returns:
        {"status": "ok"|"regression", "write_ratio": float,
         "wake_read_input_ratio": float|None, "wake_rewrite_fraction": float|None,
         "wake_hits": "9/10"|..., "notes": [...], "recommended_action": str|None}

    `recommended_action` is the one-line user-facing remediation hint shown
    alongside the regression notification.
    """
    data = _scan(transcripts)
    t = data["totals"]
    wakes = data["wakes"]
    write_ratio = t["1h_write"] / max(t["5m_write"], 1)
    notes: list[str] = []
    actions: list[str] = []
    status = "ok"
    if t["1h_write"] + t["5m_write"] == 0:
        return {"status": "ok", "write_ratio": None,
                "wake_read_input_ratio": None, "wake_rewrite_fraction": None,
                "wake_hits": "0/0",
                "notes": ["no cache writes in scanned transcripts"],
                "recommended_action": None}
    if write_ratio < 5:
        status = "regression"
        notes.append("write tier ratio < 5 — 1h TTL may not be applied")
        actions.append("Check that DISABLE_TELEMETRY and "
                       "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC are NOT set")
    wake_ratio: float | None = None
    wake_rewrite_fraction: float | None = None
    wake_hits = f"0/{len(wakes)}"
    if wakes:
        total_in = sum(w["input"] for w in wakes)
        total_rd = sum(w["read"] for w in wakes)
        wake_ratio = total_rd / max(total_in, 1)
        # Per-wake, NOT aggregate: one intermittent boundary miss among many
        # hits must not be diluted below threshold by sum/sum. (Assumes captured
        # wakes are post-sleep resumes — a first activation has a cold cache and
        # scores ~1.0; the wake detector must exclude first-activations before
        # this is relied on. See module docstring caveats.)
        per_wake_frac = [w.get("eph1h", 0) / max(w["read"] + w.get("eph1h", 0), 1)
                         for w in wakes]
        wake_rewrite_fraction = max(per_wake_frac)
        miss_wakes = sum(1 for fr in per_wake_frac if fr > 0.5)
        hits = sum(1 for w in wakes if w["read"] > w["input"])
        wake_hits = f"{hits}/{len(wakes)}"
        if miss_wakes:
            status = "regression"
            notes.append(
                f"{miss_wakes}/{len(wakes)} wake(s) re-wrote >50% of context "
                f"(worst {wake_rewrite_fraction:.0%}) — sleep overrunning the "
                "1h cache TTL (boundary miss)")
            actions.append("Lower MAX_SLEEP_SEC below the 1h (3600s) cache TTL")
        if wake_ratio < 10:
            status = "regression"
            notes.append("wake-turn read/input < 10 — cache not surviving sleeps")
            actions.append("Try `export ENABLE_PROMPT_CACHING_1H=1` and restart "
                           "Claude Code, then re-run measure_cache_ttl.py")
    recommended_action = " | ".join(actions) if actions else None
    return {"status": status, "write_ratio": round(write_ratio, 1),
            "wake_read_input_ratio": round(wake_ratio, 1) if wake_ratio is not None else None,
            "wake_rewrite_fraction": round(wake_rewrite_fraction, 3) if wake_rewrite_fraction is not None else None,
            "wake_hits": wake_hits, "notes": notes,
            "recommended_action": recommended_action}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--transcripts", type=int, default=10,
                   help="number of recent transcripts to scan (default 10)")
    p.add_argument("--json", action="store_true",
                   help="emit raw JSON instead of human report")
    p.add_argument("--health", action="store_true",
                   help="emit compact health-check JSON (used by probe.py)")
    args = p.parse_args()

    if args.health:
        print(json.dumps(health_check(args.transcripts)))
        return 0
    data = _scan(args.transcripts)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(_report(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
