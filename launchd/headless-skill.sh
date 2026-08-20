#!/bin/zsh
# headless-skill.sh <name> <timeout-seconds> <prompt...>
# Run a Claude Code skill headlessly (no open session) from launchd.
#
# - Fresh `claude -p` session per fire: full ~/.claude config (skills, hooks,
#   settings.json model) loads; session exits when the prompt completes.
# - flock-style guard: a second fire of the SAME name while one is running is
#   skipped (prevents self-overlap on slow runs). It does NOT guard against a
#   concurrent INTERACTIVE session running the same skill — don't enable a
#   headless loop for a skill you also run via /loop.
# - Logs to ~/.claude/logs/headless-<name>.log (rotated at ~1MB).
# - MCP caveat: OAuth-authenticated remote MCP servers may be absent headless.
#   Skills that need Linear should fall back to the GraphQL pattern in
#   hooks/reconcile-ticket.sh ($LINEAR_API_KEY / $LINEAR_KEY_FILE — see hooks/reconcile-ticket.sh).
set -u
NAME="${1:?usage: headless-skill.sh <name> <timeout-s> <prompt...>}"
TIMEOUT="${2:?timeout seconds required}"
shift 2
PROMPT="$*"

CLAUDE_BIN="$HOME/.local/bin/claude"
LOGDIR="$HOME/.claude/logs"
LOG="$LOGDIR/headless-$NAME.log"
LOCK="/tmp/headless-skill-$NAME.lock"
mkdir -p "$LOGDIR"

# rotate log at ~1MB
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  mv "$LOG" "$LOG.1"
fi

# self-overlap guard (mkdir is atomic; stale lock >2*TIMEOUT is reclaimed)
if ! mkdir "$LOCK" 2>/dev/null; then
  age=$(( $(date +%s) - $(stat -f%m "$LOCK" 2>/dev/null || echo 0) ))
  if [ "$age" -lt $(( TIMEOUT * 2 )) ]; then
    echo "$(date '+%F %T') [$NAME] SKIP: previous run still holds $LOCK (age ${age}s)" >>"$LOG"
    exit 0
  fi
  echo "$(date '+%F %T') [$NAME] reclaiming stale lock (age ${age}s)" >>"$LOG"
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "=== $(date '+%F %T') [$NAME] fire: $PROMPT"
  # launchd PATH is minimal — give claude the tools its Bash calls expect
  export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  # `-k 60` makes the cap a BOUND, not a request: plain `timeout` sends
  # SIGTERM only, and a wedged claude that ignores it would outlive the cap —
  # then go stale at the lock TTL while still alive, which is exactly the
  # live-sweep-reaped overlap the TTL sizing (hooks/babysit-lock.sh L1) rules
  # out. SIGKILL 60s after the deadline closes that path.
  timeout -k 60 "$TIMEOUT" "$CLAUDE_BIN" -p "$PROMPT" --dangerously-skip-permissions
  rc=$?
  echo "=== $(date '+%F %T') [$NAME] exit=$rc"
} >>"$LOG" 2>&1
