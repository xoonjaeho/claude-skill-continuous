# /continuous — Session Continuity Across Rate Limits

A Claude Code skill that keeps a long-running task alive across Claude.ai's 5-hour rate-limit boundaries. When utilization reaches a threshold, the skill pauses (via `ScheduleWakeup`), sleeps in 1-hour chunks, and automatically resumes the same task in the same conversation when the limit resets.

## Quick start

```
/loop /continuous 95 "<your task>"
```

- `95` is the 5-hour utilization threshold (default 95). Below this, work proceeds.
- Must be invoked through `/loop` — `/continuous` alone refuses (loop detection by transcript inspection).
- Quote the task if it has spaces.

Examples:
```
/loop /continuous 95 "refactor the rendering pipeline"
/loop /continuous 90 "fix all type errors in the api package"
/loop /continuous "answer pending PRs"      # threshold defaults to 95
```

## How it works

```
[invocation]
   /loop /continuous 95 "task"
   ├─ probe.py: inspects transcript — refuses if not in /loop
   ├─ writes per-session state.json
   └─ probes /api/oauth/usage

[normal operation]
   each tool call → PreToolUse hook (scripts/hook.py)
   ├─ if active state + 5h util ≥ threshold → blocks (exit 2 + stderr marker)
   └─ otherwise passes through

[on block]
   skill body parses marker
   ├─ axis=five_hour → ScheduleWakeup(min(reset+60, 3600), same prompt)
   └─ axis=seven_day (≥99% only) → terminate, push notify, no wakeup

[on wake]
   transcript context preserved → re-checks usage → resumes work
```

## Files

```
continuous/
├── README.md              this file
├── LICENSE                MIT
├── SKILL.md               skill body Claude follows at runtime
└── scripts/
    ├── constants.py         single source of truth (markers, regex, paths)
    ├── usage_fetch.py       OAuth /api/oauth/usage client + disk cache
    ├── state.py             per-session state lifecycle
    ├── probe.py             unified turn-start probe
    ├── measure_cache_ttl.py prompt-cache TTL health monitor
    ├── hook.py              PreToolUse hook
    └── loop_check.py        /loop-context detector (transcript-based)
```

State and cache live at `~/.claude/cache/continuous/`:
- `<session_id>.json` — per-session state
- `usage.json` — 60s API response cache
- `usage.lock` — 30s lock to coalesce concurrent fetches
- `ttl_health.json` — cached TTL health-check (~30 day throttle)

## Install

### 1. Place the skill

Copy or clone this repo to `~/.claude/skills/continuous/`:

```bash
git clone https://github.com/xoonjaeho/claude-skill-continuous ~/.claude/skills/continuous
```

The directory name `continuous` is significant — it must match the slash command (`/continuous`).

### 2. Register the PreToolUse hook

Open `~/.claude/settings.json`, find `hooks.PreToolUse` (create the key if missing), and append:

**macOS / Linux:**
```json
{
  "matcher": "*",
  "hooks": [
    {
      "type": "command",
      "command": "python \"$HOME/.claude/skills/continuous/scripts/hook.py\""
    }
  ]
}
```

**Windows:**
```json
{
  "matcher": "*",
  "hooks": [
    {
      "type": "command",
      "command": "python \"%USERPROFILE%\\.claude\\skills\\continuous\\scripts\\hook.py\""
    }
  ]
}
```

The matcher `*` fires the hook on every tool call. The hook short-circuits to exit 0 unless an active `/continuous` session state file exists, so it has no effect outside this skill.

### 3. Verify

Run this from any shell — same command works on bash, zsh, cmd.exe, and PowerShell:

```
python -c "import json,pathlib; p=pathlib.Path.home()/'.claude'/'settings.json'; s=json.loads(p.read_text(encoding='utf-8')); hooks=[h for e in s.get('hooks',{}).get('PreToolUse',[]) for h in e.get('hooks',[]) if 'continuous' in h.get('command','')]; print('registered' if hooks else 'NOT registered')"
```

Expect `registered`. If you see `NOT registered`, re-check the JSON edit in step 2.

### 4. Disable / remove

Either:
- Remove the hook entry from `settings.json`, or
- Delete `~/.claude/skills/continuous/` (cache at `~/.claude/cache/continuous/` may also be cleared).

## Configuration

The skill is intentionally minimal. There are two thresholds:

| Axis | Threshold | Source | Effect on hit |
|---|---|---|---|
| 5-hour | user-supplied (default 95) | command arg | `ScheduleWakeup` 1h sleep, retry |
| 7-day  | hardcoded 99 | `constants.py:SEVEN_DAY_THRESHOLD` | terminal — no resume |

7-day is hardcoded because reset is up to 7 days away — sleep-and-retry is wasteful at that scale, terminal is the right behavior.

## Cache TTL alignment

`ScheduleWakeup` sleeps up to 1h. Anthropic's prompt-cache TTL is also 1h on Pro/Max plans with telemetry on (auto-applied on recent Claude Code builds). TTL is a **last-access timer**, so each wake's read refreshes the window — `/continuous` cycles re-enter with cache hit, not miss. **No skill change is needed for this to work**; the table below is for detecting future regression.

| Signal (run `python scripts/measure_cache_ttl.py`) | Healthy | Regression hint | Mitigation |
|---|---|---|---|
| `write tier ratio` (1h : 5m) | ≥ 5 | < 5 | Telemetry coupling — make sure `DISABLE_TELEMETRY` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` are NOT set |
| wake-turn `read/input ratio` | ≫ 10 | ≤ 10 | Anthropic-side default may have flipped to 5min. Try `export ENABLE_PROMPT_CACHING_1H=1` and re-measure (note: this var is documented for API key / Bedrock / Vertex / Foundry; OAuth/Pro/Max coverage is untested upstream so it may be a no-op) |

`cache_read_input_tokens` is a single flat field — per-tier read breakdown is not exposed by Claude Code transcripts, so 1h vs 5m read split must be inferred from the write-tier ratio. Recommend a monthly re-run of the measurement script.

The skill auto-runs the health check (throttled to once per 30 days) on each `/continuous` activation. On regression the user is notified via `PushNotification` and a one-line warning is prepended to the wake/start message.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/continuous works only inside /loop dynamic mode.` | invoked without `/loop ` prefix | re-invoke via `/loop /continuous …` |
| 401 from usage fetch | `accessToken` expired | restart Claude Code (it refreshes credentials) |
| Hook blocks unrelated session | state file leaked from prior crash, or two Claude Code sessions running simultaneously where one has `/continuous` active | run `python scripts/state.py --reap` (or wait — auto-reaps after 7d). The hook only auto-disambiguates between sessions when the tool payload includes `session_id`; without it, a single leftover state file applies to all sessions on the machine. |
| `parse_fail_count` increasing | hook stderr marker drifted | check `BLOCK_MARKER_FORMAT` matches `BLOCK_MARKER_REGEX` in `constants.py` |
| Hook errors block all tools | `settings.json` path wrong | edit `settings.json` externally to fix path; cannot be fixed from inside Claude Code |

### Test mode bypass

```bash
CONTINUOUS_TEST_BYPASS=1 <your-bash-command>
```
or set `CONTINUOUS_TEST_BYPASS=1` in the shell that launches Claude Code. The hook honors both forms and returns exit 0.

### Component-level diagnostics

```bash
python scripts/usage_fetch.py                          # current usage (live)
python scripts/usage_fetch.py --mock '{"five_hour":{"utilization":97,"resets_at":"2030-01-01T00:00:00Z"}}'
python scripts/state.py --list                         # active sessions
python scripts/state.py --show <sid>                   # inspect one
python scripts/state.py --reap                         # remove stale (>7d)
python scripts/state.py --delete <sid>                 # remove a specific session
python scripts/hook.py --self-test --mock-util 96 --threshold 95
python scripts/loop_check.py --explain                 # is current turn in /loop?
python scripts/measure_cache_ttl.py                    # human-readable TTL report
python scripts/constants.py                            # print all constants + self-test
```

## Limits / out-of-scope

- 7-day threshold is not user-configurable (hardcoded 99)
- `key=value` style args (e.g. `round=N`) are not parsed — positional only
- No multi-session limit coordination (each `/loop /continuous` is independent)
- No mid-turn pause: Claude can only pause between turns, not within a single response
- Falls back to no-op when `/loop` exits — task continuation requires user re-invocation only after 7d-terminal events

## Dependencies

- Python 3.9+ (stdlib only — no `requests`, no `dateutil`, no third-party)
- Claude Code build with `ScheduleWakeup` tool support and the built-in `/loop` dynamic mode
- OAuth credentials at `~/.claude/.credentials.json` (Pro/Max subscription)

`/loop` is a Claude Code built-in command, not a separate install. `loop_check.py` detects it by matching the literal SKILL.md header line `# /loop — schedule a recurring or self-paced prompt` in the transcript. If Claude Code changes that header upstream, edit `LOOP_BODY_HEADER` in `scripts/loop_check.py` to match.

`PushNotification` is a Claude Code deferred tool routed through whichever channel the user has configured (e.g. an OS notification, a chat plugin, etc.). If unavailable, the skill silently skips the push and still emits the in-terminal warning.

## License

MIT — see [LICENSE](LICENSE).
