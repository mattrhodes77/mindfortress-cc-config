#!/usr/bin/env bash
# babysit-fire-log.sh — record a sweep's fire/exit pair, and detect a
# single-turn forfeit.
#
# `fire`/`exit` write a plain audit trail to ~/.claude/logs/headless-babysit.log
# — there is no downstream consumer parsing that log in this repo (adapt one if
# you build a stall detector against it); they exist so a headless sweep leaves
# a human-readable record of when it started and how it ended. `verdict` is the
# load-bearing subcommand: it is what launchd/babysit-hourly-gate.sh calls to
# decide whether a sweep that exited "successfully" actually did anything.
#
# Usage (from a launcher, around its sweep):
#   babysit-fire-log.sh fire "/babysit-prs no-loop"
#   babysit-fire-log.sh exit <rc>
#   babysit-fire-log.sh verdict <sweep_start_epoch> <rc> <duration_s> [--lock <outcome>]
#
# ---------------------------------------------------------------------------
# `verdict` — the single-turn FORFEIT detector
#
# A sweep run headlessly (launchd/babysit-hourly-gate.sh -> headless-skill.sh
# -> `claude -p`) is single-turn. When the model stops emitting tool calls the
# process EXITS -- there is no notifier and no second turn. A sweep that
# backgrounds its classifier and "waits for the completion notification" is
# therefore over the moment it stops working: it exits rc=0, well under a real
# sweep's runtime, having done nothing. Neither the exit code, the pipeline
# status, nor the fire/exit pairing can see it on their own --
# `commands/babysit-prs.md`'s EXECUTION MODE section states the constraint so
# it stops happening, and this subcommand catches the residue so a recurrence
# is loud instead of silent.
#
# The signature is the conjunction of two things the launcher already has:
#   * the sweep exited rc=0                   (a non-zero rc is already loud)
#   * it ran under BABYSIT_FORFEIT_MAX_S      (default 150s; a real sweep runs
#                                               far longer)
#   * it wrote NO Step 3 `sweep` ledger row at/after its own start
#
# FAIL DIRECTION IS THE WHOLE DESIGN. Over-firing is worse than the bug: a
# detector that flags healthy sweeps relaunches them and DOUBLES the load on a
# queue that is already working. So: only a CONFIDENT absence yields FORFEIT.
# An unreadable ledger, an unparseable `ts`, or a jq failure all resolve to OK
# -- "I could not tell" is never allowed to mean "it did nothing".
#
# The field is `ts` (ISO8601 UTC, `Z`), written by hooks/ledger-append.sh. Read
# it under any other name and the detector inverts. That coupling is pinned
# end-to-end by tests/test_babysit_fire_log.py, which drives the REAL append
# hook and asserts the REASON, not just the exit code -- an exit-code-only
# assertion stays green under exactly that drift, because "row found" and "row
# present but undatable" both return OK.
#
# KNOWN RESIDUAL (documented, not fixed here). The row is matched on
# `ts >= sweep_start`, not on a sweep identity, so a row written by a DIFFERENT
# launcher's sweep during our window would be read as ours and suppress a real
# forfeit. Two things keep that off the table in practice: the launcher calls
# `verdict` IMMEDIATELY after its own sweep exits, so almost nothing can have
# landed in between, and the sweep mutex plus the heartbeat gate mean
# concurrent sweeps are already the exception. It is also the SAFE direction --
# it can only make the detector miss a forfeit (costing one cycle, the status
# quo), never make it relaunch a healthy sweep (costing double load on a
# working queue). Tagging rows with a launch id would close it properly, but
# that changes a ledger shape other tooling parses.
#
# A sweep that never HELD THE LOCK cannot have forfeited anything: a sweep that
# hits the mutex at Step 0 and skips exits rc=0 in well under the forfeit
# threshold, with no ledger row -- byte-identical to the forfeit signature.
# Ownership is the discriminator, and the launcher already computed it one line
# earlier via `babysit-lock.sh reap-since`:
#   REAPED / REAP FAILED -> we DID acquire at Step 0 and died before Step 4's
#                           release. A forfeit is live; fall through.
#   KEPT                 -> the lock is provably NOT ours, so we never got
#                           past Step 0. Nothing was forfeited.
#   UNLOCKED / unknown   -> genuinely ambiguous (a healthy sweep that released
#                           normally looks identical to a skip whose blocker
#                           finished first). Fall through to the ledger check
#                           rather than invent a verdict -- and note the
#                           blocker's own row, if it landed after our start,
#                           already resolves that case to OK.
# This check sits ABOVE the duration gate deliberately: ownership is decisive
# on its own, so a skip is never a forfeit at ANY duration.
#
# Exit codes: 0 = OK (healthy, or not the forfeit shape), 1 = FORFEIT, 2 = usage.
# ---------------------------------------------------------------------------
set -euo pipefail

LOG="${BABYSIT_LOG:-$HOME/.claude/logs/headless-babysit.log}"
mkdir -p "$(dirname "$LOG")"

case "${1:-}" in
  fire)
    printf '=== %s [babysit] fire: %s\n' "$(date '+%F %T')" "${2:-/babysit-prs no-loop}" >>"$LOG"
    ;;
  exit)
    # Validate EXACTLY what a reader's `exit=(-?\d+)` regex can parse. A value
    # it cannot match -- say `1-2` from a mis-captured $pipestatus -- would
    # write a line that looks like an exit but pairs with nothing. Reject it
    # here instead.
    rc="${2:-}"
    digits="${rc#-}"
    case "$digits" in
      ''|*[!0-9]*) echo "usage: babysit-fire-log.sh exit <rc>" >&2; exit 2 ;;
    esac
    printf '=== %s [babysit] exit=%s\n' "$(date '+%F %T')" "$rc" >>"$LOG"
    ;;
  verdict)
    start="${2:-}"; rc="${3:-}"; dur="${4:-}"; lock_outcome="unknown"
    shift 4 2>/dev/null || true
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --lock)
          lock_outcome="${2:-}"
          case "$lock_outcome" in
            reaped|failed|kept|unlocked|unknown) ;;
            *) echo "usage: --lock <reaped|failed|kept|unlocked|unknown>" >&2; exit 2 ;;
          esac
          shift 2 ;;
        *) echo "usage: babysit-fire-log.sh verdict <sweep_start_epoch> <rc> <duration_s> [--lock <outcome>]" >&2; exit 2 ;;
      esac
    done
    # Validate before deciding anything: a mis-captured $pipestatus must produce
    # a usage error, never a verdict. Same stance as `exit` above.
    case "$start" in ''|*[!0-9]*) echo "usage: babysit-fire-log.sh verdict <sweep_start_epoch> <rc> <duration_s>" >&2; exit 2 ;; esac
    case "$dur"   in ''|*[!0-9]*) echo "usage: babysit-fire-log.sh verdict <sweep_start_epoch> <rc> <duration_s>" >&2; exit 2 ;; esac
    case "${rc#-}" in ''|*[!0-9]*) echo "usage: babysit-fire-log.sh verdict <sweep_start_epoch> <rc> <duration_s>" >&2; exit 2 ;; esac

    # Validate the override too. Unvalidated, a typo'd BABYSIT_FORFEIT_MAX_S makes
    # the `-ge` below fail under `set -e`, so the hook dies with no output and no
    # verdict -- a silent failure inside the guard whose whole job is ending
    # silent failures. Fall back to the default and say so on stderr.
    max_s="${BABYSIT_FORFEIT_MAX_S:-150}"
    case "$max_s" in
      ''|*[!0-9]*)
        echo "babysit-fire-log: BABYSIT_FORFEIT_MAX_S=$max_s is not a number; using 150" >&2
        max_s=150 ;;
    esac

    # A non-zero rc is ALREADY loud and the launcher's own `case` owns it.
    # Relaunching here would be actively harmful -- a rate-limit/outage exit
    # can return non-zero within seconds, and an immediate retry just burns
    # the next fire against the same failure.
    if [ "$rc" -ne 0 ]; then
      echo "OK rc=$rc dur=${dur}s — non-zero exit is not a silent forfeit; rc handling owns it"
      exit 0
    fi

    # A sweep that NEVER HELD THE LOCK cannot have forfeited anything.
    #
    # A sweep that hits the mutex at Step 0 and skips exits rc=0 in well under
    # the forfeit threshold with no ledger row -- byte-identical to the forfeit
    # signature. Relaunching it just collides again.
    #
    # Ownership is the discriminator, and the launcher already computed it one
    # line earlier via `babysit-lock.sh reap-since`:
    #   REAPED / REAP FAILED -> we DID acquire at Step 0 and died before Step 4's
    #                           release. A forfeit is live; fall through.
    #   KEPT                 -> the lock is provably NOT ours, so we never got
    #                           past Step 0. Nothing was forfeited.
    #   UNLOCKED / unknown   -> genuinely ambiguous (a healthy sweep that released
    #                           normally looks identical to a skip whose blocker
    #                           finished first). Fall through to the ledger check
    #                           rather than invent a verdict -- and note the
    #                           blocker's own row, if it landed after our start,
    #                           already resolves that case to OK.
    #
    # This check sits ABOVE the duration gate deliberately: ownership is decisive
    # on its own, so a skip is never a forfeit at ANY duration.
    if [ "$lock_outcome" = "kept" ]; then
      echo "OK LOCKED skip — the lock is another sweep's; ours never acquired it, so nothing was forfeited"
      exit 0
    fi

    # Only the SHORT shape is auto-relaunchable. A sweep that ran a long time
    # and wrote no row failed some other way and may have done partial work
    # (merges, pushes, CR-CLI launches); re-running that blindly is a different
    # and riskier action than re-running one that provably did nothing.
    if [ "$dur" -ge "$max_s" ]; then
      echo "OK dur=${dur}s >= ${max_s}s — not the forfeit shape"
      exit 0
    fi

    ledger="$HOME/.claude/automation-ledger.jsonl"

    # Absence is only evidence when the WRITER was available. Step 3's append is
    # a guarded no-op --
    #   [ -x ~/.claude/hooks/ledger-append.sh ] && ~/.claude/hooks/ledger-append.sh …
    # -- so on a machine without that helper a perfectly healthy sweep writes no
    # row by design, and reading that as "did nothing" would relaunch healthy
    # sweeps on every short cycle. Check the capability before trusting silence.
    if [ ! -x "$HOME/.claude/hooks/ledger-append.sh" ]; then
      echo "OK no ledger writer configured (Step 3's append is a guarded no-op) — absence is not evidence"
      exit 0
    fi

    if [ ! -e "$ledger" ]; then
      echo "FORFEIT rc=0 dur=${dur}s < ${max_s}s and no ledger at all — sweep exited having done nothing"
      exit 1
    fi
    if [ ! -r "$ledger" ]; then
      echo "OK ledger unreadable — cannot prove absence, failing toward healthy"
      exit 0
    fi

    # Classify every babysit `sweep` row: AFTER (dated at/after our start),
    # BEFORE (a previous sweep's), UNDATED (present but its `ts` will not parse).
    # `-R` + `try fromjson` so a truncated or non-JSON line is skipped rather
    # than aborting the scan — this ledger is append-only from several writers.
    rows=""
    if ! rows="$(jq -R -r --argjson start "$start" '
          (try fromjson catch null) as $o
          | if ($o | type) != "object" then empty
            elif ($o.skill? != "babysit" or $o.event? != "sweep") then empty
            else (($o.ts? // "") | try fromdateiso8601 catch null) as $t
              | if   $t == null    then "UNDATED"
                elif $t >= $start  then "AFTER"
                else                    "BEFORE" end
            end' "$ledger" 2>/dev/null)"; then
      echo "OK ledger scan failed — cannot prove absence, failing toward healthy"
      exit 0
    fi

    case "$rows" in
      *AFTER*)
        echo "OK sweep wrote its Step 3 ledger row (dur=${dur}s)"
        exit 0 ;;
      *UNDATED*)
        # A babysit sweep row IS present; only its date is unreadable. Declaring
        # a forfeit on a formatting problem is exactly the over-fire this guard
        # must not commit.
        echo "OK a babysit sweep row is present but undatable — failing toward healthy"
        exit 0 ;;
      *)
        echo "FORFEIT rc=0 dur=${dur}s < ${max_s}s and no sweep ledger row since sweep_start — sweep exited having done nothing"
        exit 1 ;;
    esac
    ;;
  *)
    echo "usage: babysit-fire-log.sh {fire <prompt>|exit <rc>|verdict <sweep_start_epoch> <rc> <duration_s>}" >&2
    exit 2
    ;;
esac
