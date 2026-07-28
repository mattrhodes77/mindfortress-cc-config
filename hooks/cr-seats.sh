#!/usr/bin/env bash
# cr-seats.sh -- manage the CodeRabbit seat pool used by cr-review.sh.
#
# CodeRabbit rate-limits PER DEVELOPER, not per org (docs: Pro = 5 PR/hr + 5 IDE/hr
# + 5 CLI/hr, three separate rolling buckets per seat). Every worker on this machine
# authenticates as one identity, so all of them share one bucket. One seat dir per
# teammate turns that into N buckets.
#
# The CLI reads credentials from $HOME/.coderabbit/auth.json, so a seat is just a
# fake HOME:  ~/.claude/cr-seats/<name>/.coderabbit/auth.json
#
#   cr-seats.sh add <name> <cr-api-key>   register a seat from an agentic API key
#   cr-seats.sh adopt <name>              register a seat from the current ~/.coderabbit
#   cr-seats.sh list                      show seats + hourly usage
#   cr-seats.sh remove <name>             drop a seat
set -euo pipefail

SEATS_DIR="${CR_SEATS_DIR:-$HOME/.claude/cr-seats}"
HOURLY_MAX="${CR_SEAT_HOURLY_MAX:-5}"

die() { printf 'cr-seats: %s\n' "$1" >&2; exit 1; }

seat_home() { printf '%s/%s' "$SEATS_DIR" "$1"; }

# Reviews this seat's identity made in the last 3600s.
#
# `.uses` only records launches cr-review.sh made, so a bare `coderabbit review`
# spends the account's hourly allowance without leaving a line — which is how a
# seat displayed "0/5 used" and was then rejected as rate-limited (2026-07-27).
# The CLI writes one dir per review under $HOME/.coderabbit/reviews and its mtime
# is the review time, so count those too and report the MAX (must match
# cr-review.sh's uses_in_window, or `list` and the router disagree).
recent_uses() {
  local uses="$1/.uses" revs="$1/.coderabbit/reviews" now cutoff a=0 b=0
  now=$(date +%s); cutoff=$((now - 3600))
  [[ -f "$uses" ]] && a=$(awk -v c="$cutoff" '$1 >= c' "$uses" | wc -l | tr -d ' ')
  [[ -d "$revs" ]] && b=$(find "$revs" -maxdepth 1 -mindepth 1 -type d -mmin -60 2>/dev/null | wc -l | tr -d ' ')
  (( b > a )) && a="$b"
  printf '%s' "$a"
}

link_gitconfig() {
  # coderabbit shells out to git; give the seat HOME the real git identity/config.
  local h="$1"
  [[ -e "$h/.gitconfig" ]] || [[ ! -f "$HOME/.gitconfig" ]] || ln -s "$HOME/.gitconfig" "$h/.gitconfig"
}

cmd_add() {
  local name="${1:-}" key="${2:-}"
  [[ -n "$name" && -n "$key" ]] || die "usage: cr-seats.sh add <name> <cr-api-key>"
  local h; h=$(seat_home "$name")
  mkdir -p "$h"; chmod 700 "$h"
  link_gitconfig "$h"
  HOME="$h" coderabbit auth login --api-key "$key" >/dev/null 2>&1 \
    || die "auth login failed for '$name' -- check the key"
  [[ -f "$h/.coderabbit/auth.json" ]] || die "no auth.json written for '$name'"
  chmod 600 "$h/.coderabbit/auth.json"
  printf 'added seat: %s\n' "$name"
  HOME="$h" coderabbit auth status 2>&1 | sed 's/^/  /'
}

cmd_adopt() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "usage: cr-seats.sh adopt <name>"
  [[ -f "$HOME/.coderabbit/auth.json" ]] || die "no ~/.coderabbit/auth.json to adopt"
  local h; h=$(seat_home "$name")
  mkdir -p "$h/.coderabbit"; chmod 700 "$h"
  cp "$HOME/.coderabbit/auth.json" "$h/.coderabbit/auth.json"
  chmod 600 "$h/.coderabbit/auth.json"
  link_gitconfig "$h"
  printf 'adopted current credentials as seat: %s\n' "$name"
  HOME="$h" coderabbit auth status 2>&1 | sed 's/^/  /'
}

cmd_remove() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "usage: cr-seats.sh remove <name>"
  local h; h=$(seat_home "$name")
  [[ -d "$h" ]] || die "no such seat: $name"
  rm -rf "$h"
  printf 'removed seat: %s\n' "$name"
}

cmd_list() {
  [[ -d "$SEATS_DIR" ]] || { printf 'no seats registered (%s)\n' "$SEATS_DIR"; return; }
  local found=0 now; now=$(date +%s)
  printf '%-12s %-28s %-10s %s\n' SEAT ACCOUNT 'USED/HR' STATE
  for h in "$SEATS_DIR"/*/; do
    [[ -f "$h/.coderabbit/auth.json" ]] || continue
    found=1
    local name acct used state until
    name=$(basename "$h")
    acct=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("user",{}).get("user_name","?"))' \
             "$h/.coderabbit/auth.json" 2>/dev/null || echo '?')
    used=$(recent_uses "${h%/}")
    state=ready
    until=0; [[ -f "$h/.limited_until" ]] && until=$(cat "$h/.limited_until")
    if (( until > now )); then state="cooling ($((until - now))s)"
    elif (( used >= HOURLY_MAX )); then state="at cap"
    fi
    [[ -d "$h/.lock" ]] && state="$state, busy"
    printf '%-12s %-28s %-10s %s\n' "$name" "$acct" "$used/$HOURLY_MAX" "$state"
  done
  (( found )) || printf 'no seats registered (%s)\n' "$SEATS_DIR"
}

case "${1:-list}" in
  add)    shift; cmd_add "$@" ;;
  adopt)  shift; cmd_adopt "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  list)   cmd_list ;;
  *)      die "unknown command '${1}' (add|adopt|list|remove)" ;;
esac
