#!/usr/bin/env bash
# babysit-progress.sh — cross-session judgment store for /babysit-prs.
#
# The classifier (babysit_classify.py) is deliberately STATELESS: it re-derives
# every PR's state from GitHub each sweep (GitHub is the source of truth). But a
# few DECISIONS are judgment that a fresh session otherwise loses — this file is
# where they persist, so a new session (or a rebooted machine) resumes with the
# same continuity instead of re-doing work:
#
#   - cli_reviewed[repo#pr] = {head, at}  — the branch head SHA a local CR-CLI
#       review last covered. The skill re-launches CLI ONLY when the current
#       head differs (author pushed new code) — this makes that rule DURABLE
#       instead of living only in one session's chat context.
#   - known_fp[repo#pr]     = {reason, since} — PRs the classifier keeps flagging
#       HAS_ACTIONABLE that are really a CR ack-reply false-positive (e.g. #540).
#       The skill checks this to avoid re-chewing the same non-finding each sweep.
#       Suppresses the WHOLE PR — see waived_findings below for one adjudicated
#       finding on an otherwise-live PR.
#   - waived_findings[repo#pr][finding-key] = {reason, since, by,
#       authorized_by_human} — finding-
#       level waivers. known_fp above is PR-wide: silencing it to kill one
#       re-raised finding also blinds the PR to every future REAL finding.
#       finding-key is caller-composed (e.g. "path/to/file.py:RULE_ID") and
#       deliberately excludes the head SHA and line number — both move on
#       every push, and the whole point is that an adjudicated finding STAYS
#       adjudicated across a force-push/rebase without re-applying the waiver.
#   - merges[]              = {pr, ticket, at} — rolling log of merges babysit did
#       (last 50), so post-restart reporting has the history.
#
# Store: ~/.claude/babysit-progress.json  (durable, survives sessions/reboot —
# unlike /tmp/babysit-prs-state.json which is stall bookkeeping only).
# All writes are atomic (tmp+mv). Concurrency-safe because the sweep already
# holds babysit-lock.sh; this is a convenience layer, not a second lock.
set -euo pipefail

STORE="${BABYSIT_PROGRESS:-$HOME/.claude/babysit-progress.json}"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

ensure() { [ -f "$STORE" ] || echo '{"cli_reviewed":{},"known_fp":{},"waived_findings":{},"merges":[]}' > "$STORE"; }
save() { # save <jq-program> [args...] ; reads $STORE, writes atomically
  ensure
  local prog="$1"; shift
  local tmp; tmp="$(mktemp)"
  # Say WHY on failure. jq's stderr used to be suppressed entirely so a
  # malformed store couldn't spray into a sweep's output, but that meant a
  # rejected write surfaced only as a bare exit 1 with no reason. One
  # diagnostic here beats repeating a guard at every call site.
  if jq "$@" "$prog" "$STORE" > "$tmp" 2>"$tmp.err"; then
    mv "$tmp" "$STORE"; rm -f "$tmp.err"
  else
    echo "babysit-progress: WRITE REJECTED ($cmd) -- store unchanged: $(head -c 300 "$tmp.err")" >&2
    rm -f "$tmp" "$tmp.err"; return 1
  fi
}

cmd="${1:-}"; shift || true
case "$cmd" in
  load)
    ensure; cat "$STORE" ;;

  summary)
    ensure
    jq -r '"progress: \(.cli_reviewed|length) cli-reviewed heads, \(.known_fp|length) known-FP, \([.waived_findings // {} | .[] | keys[]]|length) waived findings on \(.waived_findings // {} | length) PRs, \(.merges|length) merges logged"' "$STORE" ;;

  cli-head)            # cli-head <repo#pr>  -> prints last-reviewed head sha ("" if none)
    ensure
    jq -r --arg k "${1:?repo#pr}" '.cli_reviewed[$k].head // ""' "$STORE" ;;

  set-cli-head)        # set-cli-head <repo#pr> <sha>
    # PRESERVE `rounds`. This used to REPLACE the whole record, so one
    # starved-run bookkeeping call could reset the review-budget meter to 0
    # and defeat the round cap outright. `sev`/`total` are deliberately
    # DROPPED instead of carried: no severity was observed for THIS head, and
    # a stale histogram is what a nit exemption would read, so keeping it
    # would grant an exemption no review earned. Dropping it just means "not
    # exempt" — fail-safe.
    save '.cli_reviewed[$k]={head:$h,at:$t,rounds:(.cli_reviewed[$k].rounds // 0)}' \
      --arg k "${1:?repo#pr}" --arg h "${2:?sha}" --arg t "$(ts)"
    echo "cli-reviewed $1 -> $2 (rounds preserved: $(jq -r --arg k "$1" '.cli_reviewed[$k].rounds' "$STORE"))" ;;

  # ---- CR-CLI severity retention ---------------------------------------------
  # The CLI emits a STRUCTURED severity per finding (critical/major/minor/
  # trivial). Record the histogram at harvest time so the question "how bad
  # were the findings" is exact instead of reconstructed from free-prose
  # summary comments after the fact.
  set-cli-review)      # set-cli-review <repo#pr> <sha> <crit> <major> <minor> <trivial>
    # `rounds` MONOTONICALLY increments — it is the review-budget meter behind
    # a max-rounds cap, so it must survive a head change. Resetting it on a
    # new head would make the cap unreachable for exactly the PRs that need
    # it: a PR that churns forever but whose head never moves must still hit
    # the cap eventually.
    #
    # IDEMPOTENCY. The caller passes no review id, and nothing distinguishes a
    # genuine second review from a duplicate call unless THIS hook writes a
    # discriminator — so it writes one: `review_fp`, a content fingerprint of
    # the only thing the caller gives us (head SHA + the severity histogram).
    # A repeat call whose fingerprint matches the stored one is the
    # duplicate-harvest shape and is a no-op on `rounds` (sev/total/head are
    # re-written too, but they're identical anyway — this is not a partial
    # update). ANY change — a new head, OR the same head with a different
    # histogram (a genuine re-review found something new) — produces a
    # different fingerprint and increments normally, so the cap stays
    # reachable.
    #
    # BACKCOMPAT: entries written before this feature have no `review_fp`.
    # `// ""` defaults that to the empty string, which never equals a real
    # fingerprint (sha is `:?`-required to be non-empty), so the first call
    # on a legacy entry always takes the "new round" branch — no jq null
    # error, and no retroactive re-count of rounds already on record. Once
    # that call lands it writes a `review_fp`, so the entry is self-migrated
    # and later true duplicates on it are deduped like any other.
    #
    # WAIVED-FINDING BUDGET. Composes with the idempotency check above rather
    # than replacing it: this runs FIRST, as a second, cheaper short-circuit.
    # If EVERY finding in this histogram fits inside the count of active
    # finding-level waivers on record for the PR, CodeRabbit is re-raising
    # already-adjudicated findings, not doing new work — so it must not spend
    # a round, regardless of whether the histogram matches the last one
    # byte-for-byte (a re-raise can shuffle severity buckets without being
    # new content). A PR with NO waived_findings entry (absent key -> length
    # 0) always falls through to the unchanged idempotency logic — exact
    # backcompat for every existing store.
    ensure
    # `rounds_before`/`rounds_after` bracket the write purely to build the log
    # line below -- the actual dup-vs-new DECISION lives entirely inside the
    # one atomic `save` transaction (single source of truth, no parallel
    # fingerprint logic in bash that could drift from the jq program).
    rounds_before="$(jq -r --arg k "${1:?repo#pr}" '.cli_reviewed[$k].rounds // 0' "$STORE")"
    waived_n="$(jq -r --arg k "${1:?repo#pr}" '(.waived_findings[$k] // {}) | length' "$STORE")"
    save '($h + "|" + $c + "|" + $m + "|" + $n + "|" + $v) as $fp
          | (.cli_reviewed[$k].review_fp // "") as $prev_fp
          | ((.waived_findings[$k] // {}) | length) as $waived_n
          | (($c|tonumber)+($m|tonumber)+($n|tonumber)+($v|tonumber)) as $reported_n
          | (if ($waived_n > 0 and $reported_n <= $waived_n)
             then (.cli_reviewed[$k].rounds // 0)
             elif ($prev_fp != "" and $prev_fp == $fp)
             then (.cli_reviewed[$k].rounds // 0)
             else ((.cli_reviewed[$k].rounds // 0) + 1) end) as $rounds
          | .cli_reviewed[$k] = {head:$h, at:$t,
              sev:{critical:($c|tonumber), major:($m|tonumber),
                   minor:($n|tonumber), trivial:($v|tonumber)},
              total:$reported_n,
              rounds:$rounds,
              review_fp:$fp}' \
      --arg k "${1:?repo#pr}" --arg h "${2:?sha}" --arg t "$(ts)" \
      --arg c "${3:-0}" --arg m "${4:-0}" --arg n "${5:-0}" --arg v "${6:-0}"
    rounds_after="$(jq -r --arg k "$1" '.cli_reviewed[$k].rounds' "$STORE")"
    reported_n=$(( ${3:-0} + ${4:-0} + ${5:-0} + ${6:-0} ))
    if [ "$rounds_after" = "$rounds_before" ] && [ "$waived_n" -gt 0 ] && [ "$reported_n" -le "$waived_n" ]; then
      echo "cli-reviewed $1 -> $2 (crit=${3:-0} major=${4:-0} minor=${5:-0} trivial=${6:-0}, WAIVED — fully covered by $waived_n active waiver(s), round not incremented, still $rounds_after)"
    elif [ "$rounds_after" = "$rounds_before" ]; then
      echo "cli-reviewed $1 -> $2 (crit=${3:-0} major=${4:-0} minor=${5:-0} trivial=${6:-0}, DUPLICATE call — round not incremented, still $rounds_after)"
    else
      echo "cli-reviewed $1 -> $2 (crit=${3:-0} major=${4:-0} minor=${5:-0} trivial=${6:-0}, round $rounds_after)"
    fi ;;

  is-fp)               # is-fp <repo#pr>  -> exit 0 + prints reason if known FP, else exit 1
    ensure
    r="$(jq -r --arg k "${1:?repo#pr}" '.known_fp[$k].reason // ""' "$STORE")"
    [ -n "$r" ] && { echo "$r"; exit 0; } || exit 1 ;;

  add-fp)              # add-fp <repo#pr> <reason>
    save '.known_fp[$k]={reason:$r,since:$t}' \
      --arg k "${1:?repo#pr}" --arg r "${2:?reason}" --arg t "$(ts)"
    echo "known-FP $1 recorded" ;;

  clear-fp)            # clear-fp <repo#pr>  (e.g. a real finding finally landed)
    save 'del(.known_fp[$k])' --arg k "${1:?repo#pr}"
    echo "known-FP $1 cleared" ;;

  # ---- finding-level waivers --------------------------------------------------
  # known_fp above waives an entire PR. That is right for "this whole PR is a
  # false positive" but wrong for "one specific finding was adjudicated away
  # on an otherwise-live PR" -- suppressing the PR to silence one finding also
  # hides every future REAL finding on it. waive/unwaive operate one level
  # deeper: repo#pr -> a caller-composed finding-key. Keep the key STABLE
  # across a push (e.g. "<file>:<rule-id>") -- never a line number or head
  # SHA, both of which move on every commit, and the whole acceptance is that
  # a waiver survives a force-push/rebase without being re-applied.
  waive)                # waive <repo#pr> <finding-key> <reason> [by]
    ensure
    # AUTONOMOUS WAIVING IS BOUNDED (write-time gate). The nit bar — at most
    # 5 findings, zero critical, zero major — applies to waiving too, which
    # is strictly more powerful than skipping a re-review because it is
    # permanent. Enforced HERE, at write time, not merely honoured
    # downstream: the store is written by agents, and the read side cannot
    # un-write a bad record.
    #
    # Fails CLOSED on an unrecorded or unparseable histogram: the bar is a
    # claim ABOUT the findings, and with no recorded review there is nothing
    # to check it against. "We could not look" is not "we looked and it was
    # clean".
    #
    # The escape hatch NAMES the human rather than being a boolean, and that
    # name lands in `by`, so the store records who authorized it:
    #   BABYSIT_WAIVE_AUTHORIZED_BY=<human> babysit-progress.sh waive ...
    #
    # WHAT THIS IS AND IS NOT: an env var is CALLER-CONTROLLED. An agent that
    # can run this hook can also set the variable, so this is NOT
    # cryptographic proof that a human approved anything — nothing in this
    # substrate could be (the same agent can edit the store file directly).
    # What it buys, against the adversary this actually has (an agent taking
    # the cheapest path out of apply-churn): the shortcut stops being silent.
    # The default path refuses; clearing a critical now requires deliberately
    # asserting a named human, and that assertion is written durably into the
    # record where it can be read back and challenged. Deliberateness +
    # audit, not authentication.
    #
    # Validate BOTH required args up front: the guard below names the
    # finding-key in its refusals and runs BEFORE the `save` whose
    # ${2:?finding-key} used to do this, so a missing key died as
    # `$2: unbound variable` instead of saying which argument was missing.
    wk="${1:?repo#pr}"
    wfk="${2:?finding-key}"
    # Trim the override before testing it: a whitespace-only value would pass
    # `[ -z ]` and take the authorized path with by=" " — while the paired
    # Python renderer strips the same variable and would render
    # `authorized-by-human: false`. The two records of one ruling action must
    # agree, so both sides treat blank-after-strip as UNSET.
    auth="$(printf '%s' "${BABYSIT_WAIVE_AUTHORIZED_BY:-}" \
              | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [ -z "$auth" ]; then
      sev="$(jq -r --arg k "$wk" '
        if (.cli_reviewed[$k].sev // null) == null then ""
        else (.cli_reviewed[$k]
              | "\(.sev.critical // 0) \(.sev.major // 0) \(.total //
                   ([.sev.critical, .sev.major, .sev.minor, .sev.trivial]
                    | map(. // 0) | add))") end' \
        "$STORE" 2>/dev/null || true)"
      if [ -z "$sev" ]; then
        echo "waive REFUSED for $wk [$wfk]: no recorded CR-CLI severity, so the" >&2
        echo "  '<=5 findings, zero critical, zero major' bar cannot be established." >&2
        echo "  Record the review first (set-cli-review), or authorize explicitly:" >&2
        echo "    BABYSIT_WAIVE_AUTHORIZED_BY=<human> babysit-progress.sh waive ..." >&2
        exit 1
      fi
      w_crit="$(printf '%s' "$sev" | cut -d' ' -f1)"
      w_maj="$(printf '%s' "$sev" | cut -d' ' -f2)"
      # `.total` is DERIVED from the histogram when absent, never defaulted
      # to 0: a record carrying `sev` but no `total` would otherwise report 0
      # and sail through the ">5" half of the bar — fail-open on exactly the
      # check that bounds how much can be waived at once.
      w_tot="$(printf '%s' "$sev" | cut -d' ' -f3)"
      # A non-integer anywhere means the record is not one we can reason
      # about; same closed direction as the missing case above. Each field
      # independently: a combined pattern lets an EMPTY trailing field
      # through, which then reaches `[ -gt 5 ]` as a bare
      # integer-expression error.
      for w_n in "$w_crit" "$w_maj" "$w_tot"; do
        case "$w_n" in
          ''|*[!0-9]*)
            echo "waive REFUSED for $wk [$wfk]: unparseable severity record ($sev)." >&2
            echo "    BABYSIT_WAIVE_AUTHORIZED_BY=<human> babysit-progress.sh waive ..." >&2
            exit 1 ;;
        esac
      done
      w_bad=""
      [ "$w_crit" -gt 0 ] && w_bad="critical=$w_crit"
      [ "$w_maj"  -gt 0 ] && w_bad="${w_bad:+$w_bad }major=$w_maj"
      [ "$w_tot"  -gt 5 ] && w_bad="${w_bad:+$w_bad }total=$w_tot (>5)"
      if [ -n "$w_bad" ]; then
        echo "waive REFUSED for $wk [$wfk]: $w_bad." >&2
        echo "  Autonomous waiving is <=5 findings, zero critical, zero major." >&2
        echo "  A waive takes the open count toward zero, and zeroing it clears" >&2
        echo "  the merge block -- that is how a critical ships." >&2
        echo "  If a human has adjudicated this, record who:" >&2
        echo "    BABYSIT_WAIVE_AUTHORIZED_BY=<human> babysit-progress.sh waive ..." >&2
        exit 1
      fi
    fi
    # The override wins over a caller-supplied `by`: `by` is just an argument
    # an agent can pass any string to, while the override had to be set on
    # purpose.
    #
    # `authorized_by_human` is that same distinction as a FIELD, and the read
    # side needs it: "an adjudicated finding stays adjudicated -- all the way
    # through to a merge" must keep holding for a critical a HUMAN ruled on.
    # What must not hold is an AGENT waiving a critical to stop apply-churn.
    # `by` alone cannot carry that difference -- an agent can pass any name
    # as an argument -- so the flag records which PATH the write took, not
    # what string was supplied. Absent (legacy or agent-written) reads as NOT
    # authorized: fails closed.
    save '.waived_findings[$k][$fk] =
            {reason:$r, since:$t, by:$b, authorized_by_human:$auth}' \
      --arg k "$wk" --arg fk "$wfk" \
      --arg r "${3:?reason}" --arg t "$(ts)" \
      --arg b "${auth:-${4:-${BABYSIT_WAIVE_BY:-unknown}}}" \
      --argjson auth "$([ -n "$auth" ] && echo true || echo false)"
    echo "waived $wk [$wfk]: $3" ;;

  unwaive)               # unwaive <repo#pr> <finding-key>  (removes ONLY that finding)
    save 'if (.waived_findings[$k] // {}) | has($fk)
          then del(.waived_findings[$k][$fk])
          else . end' \
      --arg k "${1:?repo#pr}" --arg fk "${2:?finding-key}"
    echo "unwaived $1 [$2]" ;;

  log-merge)           # log-merge <repo#pr> <ticket>
    save '.merges += [{pr:$p,ticket:$k,at:$t}] | .merges |= (.[-50:])' \
      --arg p "${1:?repo#pr}" --arg k "${2:-}" --arg t "$(ts)"
    echo "logged merge $1 ($2)" ;;

  *)
    echo "usage: babysit-progress.sh {load|summary|cli-head <pr>|set-cli-head <pr> <sha>|set-cli-review <pr> <sha> <crit> <major> <minor> <trivial>|is-fp <pr>|add-fp <pr> <reason>|clear-fp <pr>|waive <pr> <finding-key> <reason> [by]|unwaive <pr> <finding-key>|log-merge <pr> <ticket>}" >&2
    exit 2 ;;
esac
