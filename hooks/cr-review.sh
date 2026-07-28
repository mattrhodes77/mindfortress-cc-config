#!/usr/bin/env bash
# cr-review.sh -- drop-in replacement for `coderabbit review ...` that rotates
# across the seat pool in ~/.claude/cr-seats (see cr-seats.sh).
#
# Why: CodeRabbit rate-limits per developer. All PRlaunch/bulldozer/orchestrate
# workers on this box authenticate as one identity, so they burn one seat's hourly
# bucket and 73% of cr_cli gates get recorded as limit-skips. Rotating across N
# seats multiplies the ceiling by N and stops parallel workers from colliding.
#
#   ~/.claude/hooks/cr-review.sh --base main --plain
#
# A seat that is merely BUSY (a live review holds its lock) is waited out rather
# than skipped -- see the WAIT_MAX block below. Quota exhaustion still fails fast.
#
# Exit codes:
#   0   a seat produced a review (output is on stdout, verbatim)
#   75  every seat is rate-limited/at cap, or still busy after CR_SEAT_WAIT_MAX
#       -> PRlaunch records the authorized skip
#   *   the underlying coderabbit exit code for a non-limit failure
#
# Env knobs: CR_SEAT_WAIT_MAX (default 720s, 0 = old fail-fast), CR_SEAT_POLL (20s)
set -uo pipefail

SEATS_DIR="${CR_SEATS_DIR:-$HOME/.claude/cr-seats}"
HOURLY_MAX="${CR_SEAT_HOURLY_MAX:-5}"
COOLDOWN="${CR_SEAT_COOLDOWN:-900}"     # secs a seat rests after a limit hit
LOCK_STALE="${CR_SEAT_LOCK_STALE:-1800}"
LIMIT_RE='review limit|rate.?limit|limit reached|too many requests|429'

log() { printf 'cr-review: %s\n' "$1" >&2; }
now() { date +%s; }

# No pool registered -> behave exactly like the bare CLI so nothing breaks.
# (macOS ships bash 3.2 -- no mapfile, no associative arrays.)
SEATS=()
if [[ -d "$SEATS_DIR" ]]; then
  for d in "$SEATS_DIR"/*/; do
    [[ -f "$d/.coderabbit/auth.json" ]] && SEATS+=("${d%/}")
  done
fi
if (( ${#SEATS[@]} == 0 )); then
  log "no seats registered in $SEATS_DIR -- falling back to the default identity"
  exec coderabbit review "$@"
fi

# Reviews this seat's IDENTITY made in the rolling hour.
#
# `.uses` alone under-counts: it only records launches THIS wrapper made, so any
# bare `coderabbit review` (PRlaunch pre-rotation, a human, another tool) spends
# the account's 5/hr invisibly. That is exactly how a seat reported "0/5 used"
# and was then rejected as rate-limited (2026-07-27).
#
# The CLI itself writes one directory per review under $HOME/.coderabbit/reviews,
# and its mtime is the review time -- an authoritative log we don't have to
# maintain. Count those, and take the MAX of the two signals: `.uses` still
# covers a fresh seat whose reviews/ dir doesn't exist yet, and over-counting
# only makes us back off earlier (safe direction).
reviews_in_window() {
  local d="$1/.coderabbit/reviews"
  [[ -d "$d" ]] || { printf '0'; return; }
  find "$d" -maxdepth 1 -mindepth 1 -type d -mmin -60 2>/dev/null | wc -l | tr -d ' '
}

uses_in_window() {
  local f="$1/.uses" a=0 b
  if [[ -f "$f" ]]; then
    a=$(awk -v c="$(( $(now) - 3600 ))" '$1 >= c' "$f" | wc -l | tr -d ' ')
  fi
  b=$(reviews_in_window "$1")
  (( b > a )) && a="$b"
  printf '%s' "$a"
}

prune_uses() {
  local f="$1/.uses"
  [[ -f "$f" ]] || return 0
  awk -v c="$(( $(now) - 3600 ))" '$1 >= c' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
}

cooling() {
  local f="$1/.limited_until"
  [[ -f "$f" ]] || return 1
  (( $(cat "$f" 2>/dev/null || echo 0) > $(now) ))
}

acquire() {
  local lock="$1/.lock"
  if mkdir "$lock" 2>/dev/null; then printf '%s' "$$" > "$lock/pid"; return 0; fi
  # break a lock whose owner died or that outlived a plausible review
  local age owner
  age=$(( $(now) - $(stat -f %m "$lock" 2>/dev/null || echo 0) ))
  owner=$(cat "$lock/pid" 2>/dev/null || echo 0)
  if (( age > LOCK_STALE )) || ! kill -0 "$owner" 2>/dev/null; then
    rm -rf "$lock"
    mkdir "$lock" 2>/dev/null && { printf '%s' "$$" > "$lock/pid"; return 0; }
  fi
  return 1
}
release() { rm -rf "$1/.lock"; }

# Rank: fewest reviews in the rolling hour first, then least-recently-used.
# Recomputed on every attempt -- usage and locks both move while we wait.
rank_seats() {
  for s in "${SEATS[@]}"; do
    prune_uses "$s"
    # Same reasoning as uses_in_window: a review made outside the wrapper leaves
    # no `.uses` line, so fall back to the newest reviews/ dir mtime or a seat
    # busy elsewhere looks least-recently-used and gets picked first.
    last=$(tail -1 "$s/.uses" 2>/dev/null || echo 0)
    newest=$(find "$s/.coderabbit/reviews" -maxdepth 1 -mindepth 1 -type d -mmin -60 \
               -exec stat -f %m {} \; 2>/dev/null | sort -n | tail -1)
    (( ${newest:-0} > ${last:-0} )) && last="$newest"
    printf '%s\t%s\t%s\n' "$(uses_in_window "$s")" "${last:-0}" "$s"
  done | sort -k1,1n -k2,2n | cut -f3
}

held=""
cleanup() { [[ -n "$held" ]] && release "$held"; }
trap cleanup EXIT INT TERM

# A seat is unavailable for one of three reasons, and they are NOT equivalent:
#
#   at-cap / cooling  the seat's QUOTA is spent. Waiting does not help on any
#                     useful timescale -> give up now, exactly as before.
#   busy              a live review holds the seat's lock. That clears in ~1-3
#                     min (a typical CLI review), so waiting is the right move.
#
# Before this split (2026-07-27) `busy` was treated as terminal: with 2 seats a
# burst of 3 launches always dropped the 3rd *even with 3 quota slots unused*,
# because concurrency (= seat count), not the hourly cap, was binding. Babysit
# fires its CLI launches as a burst, so it could never reach the pool's 10/hr.
#
# So: retry ONLY while at least one seat is merely busy, bounded by WAIT_MAX.
# 1200s sizes the queue babysit actually forms: 10 launches over 2 seats at ~3min
# a review is ~15min of drain, so the last-queued worker waits ~12min. Still well
# under LOCK_STALE, so a genuinely wedged lock is still reclaimed, not waited on.
WAIT_MAX="${CR_SEAT_WAIT_MAX:-1200}" # secs to wait out busy seats (0 = fail fast)
POLL="${CR_SEAT_POLL:-20}"           # secs between attempts

deadline=$(( $(now) + WAIT_MAX ))
announced=0

while :; do
  ORDER=()
  while IFS= read -r s; do
    [[ -n "$s" ]] && ORDER+=("$s")
  done < <(rank_seats)

  skipped=""; busy_seats=0
  for seat in "${ORDER[@]}"; do
    name=$(basename "$seat")

    if cooling "$seat";                              then skipped="$skipped $name:cooling"; continue; fi
    if (( $(uses_in_window "$seat") >= HOURLY_MAX )); then skipped="$skipped $name:at-cap"; continue; fi
    if ! acquire "$seat"; then
      skipped="$skipped $name:busy"; busy_seats=$(( busy_seats + 1 )); continue
    fi
    held="$seat"

    log "using seat '$name' ($(uses_in_window "$seat")/$HOURLY_MAX used this hour)"
    # Buffer the two streams separately: `--agent` mode emits JSON lines on stdout and
    # callers parse them, so CLI stderr must never be folded in. Replayed verbatim below.
    out=$(mktemp -t cr-review-out); err=$(mktemp -t cr-review-err)
    HOME="$seat" coderabbit review "$@" >"$out" 2>"$err"
    rc=$?

    if grep -qiE "$LIMIT_RE" "$out" "$err"; then
      printf '%s' "$(( $(now) + COOLDOWN ))" > "$seat/.limited_until"
      log "seat '$name' is rate-limited -- resting ${COOLDOWN}s, trying the next seat"
      rm -f "$out" "$err"; release "$seat"; held=""; skipped="$skipped $name:limited"
      continue
    fi

    cat "$out"; cat "$err" >&2
    now >> "$seat/.uses"
    rm -f "$seat/.limited_until" "$out" "$err"
    release "$seat"; held=""
    exit "$rc"
  done

  # Nothing acquired. Only a busy seat is worth waiting for.
  (( busy_seats > 0 )) || break
  remaining=$(( deadline - $(now) ))
  (( remaining > 0 )) || { log "waited ${WAIT_MAX}s for a busy seat -- giving up"; break; }

  if (( announced == 0 )); then
    log "all seats busy (${skipped# }) -- queueing, up to ${WAIT_MAX}s for one to free"
    announced=1
  fi
  # Jitter so simultaneously-launched workers don't retry in lockstep and
  # thundering-herd the same lock.
  sleep_for=$(( POLL + RANDOM % 10 ))
  (( sleep_for > remaining )) && sleep_for=$remaining
  sleep "$sleep_for"
done

log "ALL SEATS EXHAUSTED (${skipped:- none available}) -- record the authorized cr_cli skip"
exit 75
