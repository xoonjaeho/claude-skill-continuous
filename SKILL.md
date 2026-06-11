---
name: continuous
description: Maintain session work continuity across Claude.ai rate-limit boundaries. Auto-pauses when utilization reaches a threshold, sleeps in ~55-minute chunks via ScheduleWakeup, then resumes the same task in the same conversation. MUST be invoked via /loop dynamic mode (e.g. `/loop /continuous 95 "<task>"`); refuses otherwise.
---

# /continuous — session continuity across rate limits

Complete the user's task while gracefully riding through rate-limit windows.

## Hard rules
1. Run **only** inside `/loop` dynamic mode. First-activation gate is `probe.py` (transcript inspection); wake turns rely on state-file presence (state implies a prior /loop activation in this conversation).
2. ALL skill-internal Bash commands MUST start with `CONTINUOUS_SELF_PROBE=1` — without it the PreToolUse hook blocks YOU.
3. Args are positional: `/continuous [threshold] <task>` — no `key=value`.
4. Wake prompt format is fixed: `/loop /continuous {threshold} {task}`. The leading `/loop ` is **required**.
5. **Wake-turn output discipline**: emit at most one short user-facing line per branch. No "reading state…" / "checking usage…" narration. Every line written here gets re-tokenized on every subsequent wake.

## Args parse
- Tokens: `[threshold] <task...>`
- First token is integer in 1..99 → use as `threshold`; rest is `task`.
- Else → `threshold = 95`; entire ARGUMENTS is `task`.
- Empty `task` → refuse: `Task description required. Usage: /loop /continuous [N] "<task>"`. End.
- The task string is **never** interpolated directly into a Bash command line below. It is always passed via the heredoc form shown in §First activation (delimiter `CONTINUOUS_TASK_BOUNDARY_7F3A1E_EOF`, single-quoted to disable `$`/`` ` ``/command substitution). The delimiter is a long random-looking string so a user-supplied task containing it as a standalone line is effectively impossible. The wake prompt (a `ScheduleWakeup` tool argument, not a shell string) carries the task verbatim; that path is safe by construction.

The threshold applies to the **5-hour axis only**. The 7-day axis uses `SEVEN_DAY_THRESHOLD = 99` (terminal).

## Step 1 — single probe (FIRST action)

Run exactly one Bash:

```bash
CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/probe.py
```

Parse the JSON. Fields: `sid`, `turn_type` (`first|wake|unknown`), `in_loop`, `loop_reason`, `state` (dict|null), `usage` (5-field brief|null), `ttl_health` (`{status, write_ratio, wake_hits, recommended_action, should_notify, ...}|null`, throttled to once per 30 days), `errors[]`.

### TTL regression handling (ttl_health.status == "regression")

- **`should_notify == true`** (fresh measurement just happened): cache caching is silently broken. Notify out-of-band so the user sees it even if away from terminal. Run BOTH:
  1. `PushNotification(message="/continuous: cache TTL regression — {notes joined with '; '}. Action: {recommended_action}.")` — best-effort, skip silently if tool unavailable.
  2. **Prepend one line** to whichever branch you take below: `Cache TTL regression: {notes}. Action: {recommended_action}. (push sent, see scripts/measure_cache_ttl.py for full report.)`
- **`should_notify == false`** (cached read; user was already notified within the last 30 days): silent. Do not push, do not prepend. The user is presumed to be aware and either has not fixed it yet or is in the middle of fixing.
- `status != "regression"` or `ttl_health == null`: ignore the field entirely.

## Step 2 — branch by turn_type

### turn_type == "first"

Required: `in_loop == true`. If false, output exactly:
```
/continuous works only inside /loop dynamic mode.
Re-invoke as:
  /loop /continuous {threshold} "{task}"
```
End.

If `usage == null`: report `errors[0]` and end (cannot decide without usage).

Initialize state, then evaluate usage. Use the heredoc form so the task string passes safely regardless of shell metacharacters:

```bash
CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --init {SID} {THRESHOLD} --task-stdin <<'CONTINUOUS_TASK_BOUNDARY_7F3A1E_EOF'
{TASK}
CONTINUOUS_TASK_BOUNDARY_7F3A1E_EOF
```

(The single-quoted `'CONTINUOUS_TASK_BOUNDARY_7F3A1E_EOF'` delimiter suppresses `$`/`` ` ``/`\` expansion in the body.)

- `seven_day_util >= 99` → §Seven-day block.
- `five_hour_util >= threshold` → §Initial sleep.
- Else → one line: `Starting: {task}`. Proceed with the task.

### turn_type == "wake"

Loop-context check is skipped: state existence implies the prior activation was in /loop, and the wake prompt carries the `/loop ` prefix anyway. (Edge case: if the user manually re-typed `/continuous` outside /loop while a state file exists, this is treated as authorial intent to resume — not a misuse.)

Decide from `usage`:
- `usage == null` (usage fetch failed this turn — see `errors`):
  - any `errors` entry contains `401 Unauthorized` → token expired, not self-recoverable. `PushNotification(message="/continuous paused: usage 401 (token expired). Restart Claude Code, then re-invoke /loop /continuous.")` (skip if tool unavailable), then `CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --delete {SID}`, output one line `Usage 401 (token expired). Ending; re-invoke after restart.`, end. Do **not** ScheduleWakeup.
  - else → transient (network blip, malformed response). `CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --transition {SID} retry_60s --inc retry_60s`, then `ScheduleWakeup(delaySeconds=60, prompt="/loop /continuous {threshold} {task}", reason="usage fetch failed, 60s retry")`, output `Usage fetch failed; 60s retry.` This retries indefinitely at 60s so a longer outage does not terminate the task; only a 401 (above) ends it.
- `seven_day_util >= 99` → §Seven-day block.
- `five_hour_util >= threshold`:
  - `state.retry_60s >= 3` → §Block sleep (treat as fresh; reset_retry).
  - `five_hour_reset_remaining_sec <= 60` → §Retry buffer.
  - Else → §Block sleep.
- Else → §Resume.

### turn_type == "unknown"

Multiple state files exist or sid lookup failed. Run:

```bash
CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --reap
```

Then re-run the probe. If the second probe still returns `turn_type == "unknown"`, output one line `Cannot disambiguate session; ending.` and end without calling ScheduleWakeup.

## Branches (transitions via state.py — no inline python -c)

All sleep branches share this pattern: update state, schedule wake, emit one line.

### §Block sleep (over threshold, normal)
```bash
CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --transition {SID} block_sleep --inc block_count --reset-retry
```
- `delay = max(60, min(reset_remaining_sec + 60, 3300))` — the 3300 cap stays under the 1h prompt-cache TTL; a full 3600s sleep overruns it and re-writes the whole context on wake instead of reading from cache (see `constants.MAX_SLEEP_SEC`).
- `ScheduleWakeup(delaySeconds=delay, prompt="/loop /continuous {threshold} {task}", reason="rate limit hit, sleeping {minutes}m")`
- Output: `Rate limit ({util}%). Sleeping {minutes}m.`

### §Retry buffer (reset within 60s)
```bash
CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --transition {SID} retry_60s --inc retry_60s
```
- `ScheduleWakeup(delaySeconds=60, prompt=…, reason="near reset, 60s buffer (try {n}/3)")`
- Output: `Near reset; 60s buffer.`

### §Initial sleep (already over at first activation)
```bash
CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --transition {SID} initial_sleep --inc block_count --reset-retry
```
- Same delay formula and wake call.
- Output: `Already over at start ({util}%). Sleeping {minutes}m.`

### §Seven-day block (terminal)
- One-line announce + reset clock:
  `Weekly limit ({util}%). Reset at {seven_day_resets_at} (~{H}h {M}m). Re-invoke /loop /continuous after reset.`
- `H = seven_day_reset_remaining_sec // 3600`, `M = (seven_day_reset_remaining_sec % 3600) // 60`.
- `PushNotification(message="/continuous terminated: 7d limit at {util}%. Reset in {H}h {M}m.")` (skip if tool unavailable).
- `state.py --delete {SID}`.
- Do **not** call ScheduleWakeup.

### §Resume (under threshold on wake)
```bash
CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --transition {SID} resume --reset-retry
```
- No user-facing output. Continue the task immediately, referencing prior progress visible in transcript.

## Block-marker handling during work

If a tool call fails with stderr matching `BLOCK_MARKER_REGEX` (see `constants.py`):

1. Parse `axis`, `util`, `reset_remaining_sec`.
2. `axis == "seven_day"` → §Seven-day block.
3. Else → §Block sleep with the parsed values (skip the usage probe — marker is authoritative).

If parse fails:
```bash
CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --transition {SID} parse_fail_sleep --inc parse_fail_count
```
`ScheduleWakeup(delaySeconds=3300, prompt=…, reason="marker parse failed, fallback 55m sleep")`. End.

## Completion

When the task is genuinely done:

1. Output `[CONTINUOUS_DONE]` on its own line — exactly, no surrounding text.
2. `CONTINUOUS_SELF_PROBE=1 python ~/.claude/skills/continuous/scripts/state.py --delete {SID}`
3. Do **not** call ScheduleWakeup. End.

## Reminders
- Never call ScheduleWakeup outside the sleep paths.
- Never call ScheduleWakeup with a prompt missing `/loop ` prefix.
- Never write state without the `CONTINUOUS_SELF_PROBE=1` prefix.
- Wake-turn text is permanent transcript bloat — keep it to one line.

## Configuration override

OAuth usage cache TTLs are user-configurable via the optional `~/.claude/cache/usage/.policy.json` (keys `cache_ttl_sec`, `lock_ttl_sec`; defaults 60 / 30s). Loaded once at module import — process restart required for changes. See README §Configuration.
