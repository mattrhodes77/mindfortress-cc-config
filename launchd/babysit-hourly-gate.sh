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
#
# RETRY-ONCE-ON-FORFEIT. A sweep run headlessly under `claude -p` is single-turn
# (see the EXECUTION MODE section of commands/babysit-prs.md): the moment the
# model stops emitting tool calls the process exits. A sweep that backgrounds its
# classifier and waits for a completion notification headless mode never sends
# therefore exits rc=0, well under a real sweep's runtime, having done nothing --
# and every other health signal (exit code, pipeline status) reads healthy. This
# gate is the one launcher that actually fires the headless cadence, so it is the
# one place that can catch and recover from that shape:
#   - after the sweep exits, classify the reap outcome (were we the ones who
#     held the lock?) and hand both the exit shape AND that ownership to
#     hooks/babysit-fire-log.sh's `verdict` subcommand, which is the ONE
#     implementation of the forfeit signature (rc=0, short run, no Step 3
#     ledger row since sweep start, lock provably ours).
#   - on FORFEIT: reap any orphaned classifier process the forfeited sweep left
#     running (it backgrounded the classifier and never came back to read its
#     output), THEN relaunch exactly once. An orphan reap that fails to actually
#     kill the process BLOCKS the relaunch -- starting a second sweep on top of
#     a live classifier would double-spend the same review quota and race to
#     write the same state file.
#   - a SECOND forfeit is not retried again. That is a real regression in the
#     EXECUTION MODE prose, not a blip, and must stay visible rather than be
#     papered over by an unbounded retry loop.
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
#
# $BABYSIT_LAUNCH_ID identifies THIS gate invocation to every attempt it runs
# (both the first sweep and, on a FORFEIT, the relaunch) -- set ONCE here, not
# per attempt, exactly like every other launcher: it is what lets `reap-since`
# tell "a lock our own attempt acquired" apart from a concurrent sweep
# elsewhere that merely started after we did.
gate_start=$(date +%s)
export BABYSIT_LAUNCH_ID="backstop-$$-$gate_start"

child=0
attempt_start=0
reaped=0
REAP_OUTCOME=unknown

# Reap the lock OUR just-finished (or just-signaled) attempt left behind.
# Idempotent per attempt via reaped -- reset to 0 before each attempt
# starts, so a relaunch gets its own fresh reap bound to its own start time.
reap_once() {
  [ "$reaped" -eq 1 ] && return 0
  reaped=1
  local out
  out="$("$HOME/.claude/hooks/babysit-lock.sh" reap-since "$attempt_start" 2>&1)"
  print -r -- "$out" >>"$LOG"
  # Classify the outcome the same way every other launcher does, so `verdict`
  # can tell a real forfeit (we held the lock, REAPED/REAP FAILED) apart from a
  # routine LOCKED skip (KEPT -- the lock was never ours to forfeit).
  case "$out" in
    "REAP FAILED"*) REAP_OUTCOME=failed ;;
    REAPED*)        REAP_OUTCOME=reaped ;;
    KEPT*)          REAP_OUTCOME=kept ;;
    UNLOCKED*)      REAP_OUTCOME=unlocked ;;
    *)              REAP_OUTCOME=unknown ;;
  esac
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
  reaped=1   # suppress this attempt's reap too -- same uncertainty
  echo "$(date '+%F %T') [babysit] signal: lock NOT reaped (sweep may still be running); left to the TTL" >>"$LOG"
}
trap 'reap_once' EXIT
trap 'on_signal; exit 143' TERM
trap 'on_signal; exit 130' INT

attempt=1
recovery=none
rc=1

while true; do
  attempt_start=$(date +%s)
  reaped=0
  REAP_OUTCOME=unknown

  /bin/zsh "$HOME/.claude/launchd/headless-skill.sh" "$@" &
  child=$!
  wait "$child"
  rc=$?
  dur=$(( $(date +%s) - attempt_start ))
  child=0

  reap_once   # explicit, per-attempt -- do not wait for the script's own EXIT trap

  verdict_out="$("$HOME/.claude/hooks/babysit-fire-log.sh" verdict "$attempt_start" "$rc" "$dur" --lock "$REAP_OUTCOME" 2>&1)"
  verdict_rc=$?
  echo "$(date '+%F %T') [babysit] verdict (attempt=$attempt rc=$rc dur=${dur}s): $verdict_out" >>"$LOG"

  if [ "$verdict_rc" -ne 1 ]; then
    recovery=none
    break
  fi

  # FORFEIT. Exactly one retry -- see the file header.
  if [ $attempt -ge 2 ]; then
    echo "$(date '+%F %T') [babysit] FORFEIT AGAIN on attempt $attempt -- NOT retrying; this cycle is lost." >>"$LOG"
    echo "$(date '+%F %T') [babysit] a second forfeit means the EXECUTION MODE rule in commands/babysit-prs.md is not holding. Do not raise the retry count; fix the rule." >>"$LOG"
    recovery=failed
    break
  fi

  echo "$(date '+%F %T') [babysit] FORFEIT on attempt $attempt -- reaping orphaned classifier(s) before relaunch" >>"$LOG"

  # Reap the forfeited sweep's ORPHANED classifier before relaunching.
  #
  # A forfeit is precisely the shape that strands one: the sweep backgrounded
  # its classifier, stopped emitting tool calls, and the `claude -p` process
  # exited -- leaving the child running with no consumer. Relaunching on top
  # of that would run TWO classifiers at once, both spending review quota and
  # both racing to write the same state file.
  #
  # PPID 1 is the whole safety property: a process is only killed if it has
  # been reparented to init, i.e. its launching sweep is provably gone. A
  # classifier belonging to a LIVE sweep (another terminal, a concurrent
  # invocation) still has its real parent and is never matched -- which is
  # why this checks the parent, not just the command name.
  orphan_blocked=0
  for cpid in ${(f)"$(pgrep -f 'babysit_classify.py' 2>/dev/null || true)"}; do
    [ -n "$cpid" ] || continue
    cppid=$(ps -o ppid= -p "$cpid" 2>/dev/null | tr -d ' ')
    if [ "$cppid" = "1" ]; then
      echo "$(date '+%F %T') [babysit] reaping orphaned classifier pid=$cpid (parent sweep is gone)" >>"$LOG"
      # WAIT for it to actually die. `kill` only queues a SIGTERM and returns
      # immediately, so continuing here would let a slow or unresponsive
      # classifier overlap attempt 2 -- the very race this reap exists to
      # prevent. Bounded grace, then SIGKILL, then confirm.
      kill "$cpid" 2>/dev/null || true
      for _ in {1..10}; do kill -0 "$cpid" 2>/dev/null || break; sleep 1; done
      if kill -0 "$cpid" 2>/dev/null; then
        echo "$(date '+%F %T') [babysit] classifier pid=$cpid ignored SIGTERM after 10s -- SIGKILL" >>"$LOG"
        kill -9 "$cpid" 2>/dev/null || true
        for _ in {1..5}; do kill -0 "$cpid" 2>/dev/null || break; sleep 1; done
      fi
      if kill -0 "$cpid" 2>/dev/null; then
        echo "$(date '+%F %T') [babysit] WARNING: classifier pid=$cpid still alive -- NOT relaunching on top of it" >>"$LOG"
        orphan_blocked=1
      fi
    fi
  done

  if [ "$orphan_blocked" -eq 1 ]; then
    echo "$(date '+%F %T') [babysit] relaunch BLOCKED by unreaped orphan classifier" >>"$LOG"
    recovery=blocked_by_orphan
    break
  fi

  attempt=2
  recovery=relaunched
  echo "$(date '+%F %T') [babysit] relaunching once (attempt=$attempt)" >>"$LOG"
done

echo "$(date '+%F %T') [babysit] cycle done attempt=$attempt recovery=$recovery rc=$rc" >>"$LOG"
exit $rc
