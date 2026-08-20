#!/bin/zsh
# babysit-hourly-gate.sh <name> <timeout-s> <prompt...>
#
# Pre-gate for the hourly headless babysit (com.you.claude-babysit-hourly).
# The rule: don't run the background headless babysit when a terminal is already
# running babysit -- UNLESS there's no such terminal, or it's stuck. This is the
# automated form of the README "collision rule" (one driver per skill).
#
# Mechanism: hooks/babysit-heartbeat.py bumps ~/.claude/babysit-heartbeat every
# turn of a live /babysit-prs session. This gate reads that heartbeat's age:
#   fresh (< STALE_S)      -> a terminal is actively babysitting -> SKIP the backup
#   stale (>= STALE_S)     -> terminal stuck / blocked on a prompt -> run the backup
#   missing                -> no terminal running babysit          -> run the backup
# When it does run, HEADLESS_BABYSIT=1 stops the backup's own /babysit-prs from
# writing the heartbeat (which would otherwise look like a live interactive one).
set -u

HEARTBEAT="$HOME/.claude/babysit-heartbeat"
STALE_S=4200          # 70 min. A healthy hourly /loop bumps the
                      # heartbeat every sweep, well inside this window, so it
                      # always suppresses the backup; only a wedged or closed
                      # terminal goes quiet long enough to let the backup through.
LOG="$HOME/.claude/logs/headless-babysit.log"
mkdir -p "$(dirname "$LOG")"

if [ -f "$HEARTBEAT" ]; then
  now=$(date +%s)
  mt=$(stat -f%m "$HEARTBEAT" 2>/dev/null || echo 0)
  age=$(( now - mt ))
  if [ "$age" -lt "$STALE_S" ]; then
    echo "$(date '+%F %T') [babysit] SKIP: interactive babysit alive (heartbeat age ${age}s < ${STALE_S}s)" >>"$LOG"
    exit 0
  fi
  echo "$(date '+%F %T') [babysit] heartbeat stale (age ${age}s >= ${STALE_S}s) -> running backup" >>"$LOG"
else
  echo "$(date '+%F %T') [babysit] no heartbeat -> running backup" >>"$LOG"
fi

export HEADLESS_BABYSIT=1

# Run (not `exec`) so we can reap the lock our own sweep left behind on EVERY
# exit path. A sweep that dies between its Step 0 acquire and its Step 4
# release leaves the lock held; the stale TTL is deliberately sized ABOVE the
# sweep timeout so it can never reap a LIVE sweep, which means it is far too
# slow to stop a dead one eating the next fire. `reap-since` reaps only a lock
# carrying OUR $BABYSIT_LAUNCH_ID, so a live sweep in another session is never
# touched.
#
# Dropping `exec` puts this shell between launchd and the sweep, so a stop
# signal (`launchctl kickstart -k`, logout, shutdown) now lands HERE instead of
# on headless-skill.sh. The trap keeps "every exit path" true on that path too
# -- without it the signal case would be the one exit that skips the reap.
sweep_start=$(date +%s)
export BABYSIT_LAUNCH_ID="backstop-$$-$sweep_start"

child=0
reaped=0
reap_once() {
  [ "$reaped" -eq 1 ] && return 0     # a signal trap also triggers EXIT
  reaped=1
  "$HOME/.claude/hooks/babysit-lock.sh" reap-since "$sweep_start" >>"$LOG" 2>&1
}

# On a signal the reap is SKIPPED, deliberately -- the lock is left to the
# TTL. The sweep inherited our $BABYSIT_LAUNCH_ID, so its live lock is
# indistinguishable from one our own finished sweep left behind, and no
# amount of kill+wait on our DIRECT child proves the whole chain is gone: a
# targeted `kill -TERM <gate-pid>` (a user, a script -- not launchd's
# group-wide signal) TERMs only this shell, our zsh child dies immediately,
# and its `timeout`/`claude` GRANDCHILD -- the process actually holding the
# lock -- keeps running. Reaping then would delete a live mutex and put two
# sweeps on the same clones and quotas, the exact overlap this file exists
# to prevent. Skipping costs at most one TTL window of skipped fires on a
# path that requires an explicit stop signal; reaping wrongly costs a
# duplicate sweep. Fail closed.
on_signal() {
  if [ "$child" -ne 0 ]; then
    kill -TERM "$child" 2>/dev/null
    wait "$child" 2>/dev/null
  fi
  reaped=1   # suppress the EXIT trap's reap too -- same uncertainty
  echo "$(date '+%F %T') [babysit] signal: lock NOT reaped (sweep may still be running); left to the TTL" >>"$LOG"
}
trap 'reap_once' EXIT
trap 'on_signal; exit 143' TERM
trap 'on_signal; exit 130' INT

/bin/zsh "$HOME/.claude/launchd/headless-skill.sh" "$@" &
child=$!
wait "$child"
exit $?
