# Headless scheduled skills (no open session required)

Session-bound loops (`/loop 1h /babysit-prs`, in-session cron) die with the
session. This rail runs skills on a schedule with **no terminal open**:
launchd (macOS) fires `headless-skill.sh`, which runs `claude -p "<prompt>"`
— a fresh headless Claude Code session per fire that loads your full config
(skills, hooks, settings.json model), does the job, and exits.

## The wrapper handles the launchd gotchas
- launchd has no shell aliases and a minimal PATH — the wrapper calls the real
  `claude` binary with `--dangerously-skip-permissions` explicitly and sets PATH.
- Per-skill lock: overlapping fires of the same skill are skipped.
- Logs to `~/.claude/logs/headless-<name>.log`, rotated at ~1MB; timeout kills hung runs.
- OAuth-authenticated remote MCP servers may be absent headless — give skills a
  CLI/API fallback (see `hooks/reconcile-ticket.sh` for the tracker-API pattern).

## Install
1. Copy an example plist, replace `/Users/you` with your home dir and rename the label.
2. `cp <plist> ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<plist>`
3. Test a fire: `launchctl kickstart gui/$(id -u)/<label>`, then read the log.
4. Disable: `launchctl bootout gui/$(id -u)/<label>`

## The collision rule
The per-skill lock guards against overlapping **headless** fires only — it does
**not** know about an interactive `/loop` running the same skill. Two drivers
sweeping the same PRs/tickets means duplicate review bumps and worktrees. The
baseline rule is **one driver per skill**: don't run a skill's interactive
`/loop` while its headless plist is loaded.

### Automating it: the babysit heartbeat gate
Rather than police that by hand, the babysit example plist routes through
`babysit-hourly-gate.sh` instead of calling `headless-skill.sh` directly, so the
headless backup **stands down whenever a live terminal is already babysitting**
and only fires when there's none — or when that terminal is stuck:

- `hooks/babysit-heartbeat.py` (a `Stop` + `UserPromptSubmit` hook) bumps
  `~/.claude/babysit-heartbeat` every turn a session is *genuinely* running
  `/babysit-prs` (it matches the slash-command invocation record in the
  transcript, not prose — so a session merely discussing babysit never trips it).
- `babysit-hourly-gate.sh` reads that heartbeat's age at each fire:
  fresh (`< 70 min`) → a terminal is actively babysitting → **SKIP**; stale
  (terminal wedged on a prompt / hung) or missing (no terminal) → **run the
  backup**. It runs the backup with `HEADLESS_BABYSIT=1` so the headless
  session's own `/babysit-prs` never writes the heartbeat.
- Once it decides to run, the gate can invoke the sweep **up to twice** in one
  fire: `/babysit-prs` is single-turn under `claude -p`, and a sweep that
  backgrounds its classifier and waits for a notification headless mode never
  sends exits having done nothing (see the EXECUTION MODE section of
  `commands/babysit-prs.md`). The gate detects that shape via
  `hooks/babysit-fire-log.sh verdict` and relaunches exactly once, first
  reaping any orphaned classifier process the forfeited sweep left running (a
  reap that fails to actually kill it blocks the relaunch rather than risk
  running two classifiers at once). A second forfeit in the same fire is not
  retried again — that's logged as a real regression, not papered over.

To use it for another skill, generalize the gate (the heartbeat detection keys
off the skill's command name). To wire the hook, add it to both events in
`settings.json`:

```json
"Stop":            [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/babysit-heartbeat.py", "timeout": 5 }] }],
"UserPromptSubmit":[{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/babysit-heartbeat.py", "timeout": 5 }] }]
```

The 70-minute window comfortably clears a full hourly `/loop` sweep, so a healthy
interactive loop always suppresses the backup; only a genuinely stuck or closed
terminal goes quiet long enough to let it through.

## Caveats
- launchd only fires while the machine is awake; missed calendar fires coalesce
  to one run on wake. Fine for weekly reports; know this for hourly loops.
- Headless sessions bill like any session — pick intervals accordingly.
