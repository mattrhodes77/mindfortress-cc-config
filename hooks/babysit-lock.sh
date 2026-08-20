#!/usr/bin/env bash
# babysit-lock.sh — machine/user-wide mutex for /babysit-prs sweeps.
#
# Prevents two sweeps (hourly cron, the launchd plist, a manual invocation, or
# a second terminal) from OVERLAPPING and racing on the shared surface:
#   - /tmp/babysit-prs-state.json  (non-atomic read-modify-write of streak/fp)
#   - git fetch/reset/worktree ops in the shared ~/code/<repo> clones
#   - /tmp/cli-*.pid CR-CLI launches (double-spent quota)
#   - duplicate @coderabbitai bumps
#
# Owner token = CLAUDE_CODE_SESSION_ID: unique per Claude session, and STABLE
# across that session's hourly cron fires (so this session's own sequential
# sweeps never lock each other out — only a DIFFERENT session/terminal does).
# Falls back to host+ppid if the env var is missing.
#
# Lock file: /tmp/babysit-prs.lock (JSON: owner, host, pid, launcher, started,
# heartbeat).
#
# THE TWO INVARIANTS THAT SIZE THE TTL AND MOTIVATE `reap-since`. These are the
# same number pulled in opposite directions, so they are decided together —
# fixing either alone breaks the other:
#
#   L1 — A LIVE SWEEP MUST NEVER LET ITS OWN LOCK EXPIRE.
#        Every launcher caps its sweep BELOW this TTL with `timeout -k` (the
#        headless launcher's cap is an ARGUMENT in its plist — the examples
#        ship 3000s). `-k` is what makes it a bound rather than a request:
#        plain `timeout` sends SIGTERM only, so a wedged sweep that ignores it
#        would outlive the cap and then go stale while still alive. So a sweep
#        is DEAD by started+cap, and TTL = 3600s clears every launcher's cap
#        with margin; a live sweep cannot age out of its own lock EVEN IF IT
#        NEVER REFRESHES AT ALL. That is a structural guarantee rather than an
#        empirical one: `refresh` only ever runs at step boundaries, and a
#        single step can run 15-20 min with no refresh inside it, so a shorter
#        TTL would reap a genuinely healthy sweep mid-step. `refresh` is
#        defence in depth, not the thing standing between a long step and a
#        duplicate sweep landing on top of it.
#
#   L2 — A DEAD SWEEP MUST NEVER EAT A FIRE.
#        With a launch cadence of e.g. 20 min (`9,29,49 * * * *`), a TTL of
#        ANY size that satisfies L1 (>3000s) outlives the cadence it blocks —
#        every crashed sweep would silently skip one or two fires. The TTL
#        therefore CANNOT be the mechanism here ("drop the TTL under the cron
#        period" buys L2 by breaking L1). `reap-since` is the mechanism
#        instead — every launcher calls it on EVERY exit path of the sweep it
#        just ran, so the lock a crashed sweep left behind is gone within
#        seconds and the next fire is never LOCKED.
#
#   Residual: if the LAUNCHER ITSELF dies mid-sweep, nothing calls `reap-since`
#   and the TTL is the only backstop. That window is long, deliberately: a
#   shorter TTL is unsound against L1, and this is the one path where no
#   reaper exists to begin with.
#
# Usage:
#   babysit-lock.sh acquire   exit 0 + "ACQUIRED …"  we now hold it
#                             exit 3 + "LOCKED …"     another LIVE sweep holds it
#                                                     (caller must SKIP the sweep)
#   babysit-lock.sh refresh   exit 0 if we own it (bumps heartbeat), else 3
#   babysit-lock.sh release   exit 0 (removes lock iff we own it, or --force)
#   babysit-lock.sh reap-since <epoch>
#                             exit 0; removes the lock iff it was taken at or
#                             after <epoch> AND carries our launcher id — i.e.
#                             it belongs to the sweep the CALLER just ran. For
#                             launchers, on every exit path.
#   babysit-lock.sh status    prints lock JSON or "UNLOCKED"
set -euo pipefail

LOCK="${BABYSIT_LOCK:-/tmp/babysit-prs.lock}"
TTL="${BABYSIT_LOCK_TTL:-3600}"

now() { date +%s; }
owner_token() { echo "${CLAUDE_CODE_SESSION_ID:-host-$(hostname)-ppid-$PPID}"; }
field() { jq -r --arg k "$2" '.[$k] // ""' "$1" 2>/dev/null || true; }

ME="$(owner_token)"

case "${1:-}" in
  acquire)
    if [ -f "$LOCK" ]; then
      lo="$(field "$LOCK" owner)"
      hb="$(field "$LOCK" heartbeat)"
      # Same sanitization reap-since gives `started`: a corrupt heartbeat
      # ("12abc") would crash the arithmetic under set -e (acquire exits 1 --
      # neither ACQUIRED nor LOCKED -- and the corrupt lock can never
      # self-heal), and an absurd all-digit value would make `age` negative
      # and the lock permanently un-expirable. Both degrade to 0 -> stale ->
      # reaped on this acquire.
      case "$hb" in ''|*[!0-9]*) hb=0 ;; esac
      [ "${#hb}" -gt 12 ] && hb=0
      age=$(( $(now) - hb ))
      if [ "$lo" != "$ME" ] && [ "$age" -lt "$TTL" ]; then
        echo "LOCKED owner=$lo age=${age}s ttl=${TTL}s"
        exit 3
      fi
      # else: stale (age>=TTL) OR already ours -> (re)take it below
      [ "$lo" != "$ME" ] && echo "REAPED stale lock owner=$lo age=${age}s" >&2
    fi
    # `launcher` = $BABYSIT_LAUNCH_ID, set by whichever launcher started this
    # sweep and inherited through `claude -p` into this Bash call. It is what
    # makes `reap-since` safe (see there); empty when a sweep was started by
    # hand, which fails that reap CLOSED.
    printf '{"owner":"%s","host":"%s","pid":%s,"launcher":"%s","started":%s,"heartbeat":%s}\n' \
      "$ME" "$(hostname)" "$PPID" "${BABYSIT_LAUNCH_ID:-}" "$(now)" "$(now)" > "$LOCK"
    echo "ACQUIRED owner=$ME"
    exit 0
    ;;
  refresh)
    [ -f "$LOCK" ] || { echo "UNLOCKED"; exit 3; }
    lo="$(field "$LOCK" owner)"
    [ "$lo" = "$ME" ] || { echo "NOT-OWNER owner=$lo"; exit 3; }
    tmp="$(mktemp)"
    jq --arg hb "$(now)" '.heartbeat=($hb|tonumber)' "$LOCK" > "$tmp" && mv "$tmp" "$LOCK"
    echo "REFRESHED heartbeat=$(now)"
    exit 0
    ;;
  release)
    if [ -f "$LOCK" ]; then
      lo="$(field "$LOCK" owner)"
      if [ "$lo" = "$ME" ] || [ "${2:-}" = "--force" ]; then
        rm -f "$LOCK"; echo "RELEASED owner=$lo"
      else
        echo "NOT-OWNER owner=$lo (left intact)"
      fi
    else
      echo "UNLOCKED"
    fi
    exit 0
    ;;
  reap-since)
    # Remove the lock IFF it is the one the CALLER's own just-finished sweep
    # acquired. TWO conditions, both required:
    #
    #   1. the lock's `launcher` equals OUR $BABYSIT_LAUNCH_ID, and
    #   2. it was taken at or after <epoch> (the moment we launched the sweep).
    #
    # Condition 1 is load-bearing and cannot be dropped for condition 2 alone.
    # `started >= since` says only "this lock is younger than my launch", which
    # a DIFFERENT session's LIVE sweep satisfies routinely: our sweep hits
    # `LOCKED` at Step 0 and exits in ~60s (rc=0), while the sweep that blocked
    # it acquired seconds after we launched — reaping on the time bound alone
    # would then delete a live mutex and run two sweeps on the same clones,
    # CR-CLI quota and bump budget. Overlap across fires and sessions is
    # explicitly normal here (commands/babysit-prs.md), so that is a routine
    # path, not a corner. Condition 2 is kept as a second bound in case a
    # launcher id is ever reused.
    #
    # NEVER keys off the lock's `pid`: it records $PPID of the acquiring shell,
    # which exits immediately, so a LIVE sweep's lock routinely names a dead pid.
    #
    # Fails CLOSED — an unlabeled lock (a sweep started by hand, or one from a
    # release predating this field) is KEPT and left to the TTL. Backs the file
    # up before removing and always says what it did; a silent reap is how an
    # unexplained skipped fire happens.
    since="${2:-}"
    case "$since" in
      ''|*[!0-9]*) echo "usage: babysit-lock.sh reap-since <epoch>" >&2; exit 2 ;;
    esac
    if [ ! -f "$LOCK" ]; then
      echo "UNLOCKED (nothing to reap)"
      exit 0
    fi
    started="$(field "$LOCK" started)"
    # Length-cap before the arithmetic test: a huge all-digit value passes the
    # pattern filter but makes `[ -ge ]` print a raw "integer expression
    # expected" error into the launchd log before falling through.
    case "$started" in
      ''|*[!0-9]*) started=0 ;;
    esac
    [ "${#started}" -gt 12 ] && started=0
    lock_launcher="$(field "$LOCK" launcher)"
    mine="${BABYSIT_LAUNCH_ID:-}"
    if [ -z "$mine" ] || [ -z "$lock_launcher" ] || [ "$lock_launcher" != "$mine" ]; then
      echo "KEPT lock (launcher='$lock_launcher' ours='$mine') — not provably our sweep's; leaving it to the TTL"
    elif [ "$started" -lt "$since" ]; then
      echo "KEPT lock (started=$started < since=$since) — predates our sweep, another sweep owns it"
    else
      # Never cp onto the backup path directly: $LOCK sits in world-writable
      # /tmp, so $LOCK.reaped can be a (re-planted, raceable) symlink and cp
      # writes THROUGH a symlink. cp into a mktemp file we own, then
      # rename(2) it over the path -- rename replaces the symlink ITSELF,
      # never following it, and is atomic.
      backup="(backup FAILED)"
      if bt="$(mktemp "$LOCK.reaped.XXXXXX" 2>/dev/null)"; then
        if cp -f "$LOCK" "$bt" 2>/dev/null && mv -f "$bt" "$LOCK.reaped" 2>/dev/null; then
          backup="$LOCK.reaped"
        else
          rm -f "$bt"
        fi
      fi
      if rm -f "$LOCK"; then
        echo "REAPED our sweep's lock (launcher=$mine started=$started >= since=$since; backup: $backup)"
      else
        echo "REAP FAILED — $LOCK still held (rm refused); leaving it to the TTL" >&2
      fi
    fi
    exit 0
    ;;
  status)
    [ -f "$LOCK" ] && cat "$LOCK" || echo "UNLOCKED"
    exit 0
    ;;
  *)
    echo "usage: babysit-lock.sh {acquire|refresh|release|reap-since <epoch>|status}" >&2
    exit 2
    ;;
esac
