#!/usr/bin/env python3
"""babysit_classify.py — deterministic classifier/planner for /babysit-prs.

Moves ALL classification + planning out of the babysit-prs skill prose into
tested Python. Stdlib only; shells out to `gh`. Emits ONE JSON document on
stdout. The skill keeps only judgment work (applying fixes, CLI harvest, prose).

Every hard rule encoded here patches a past model mistake — see the regression
tests in tests/ and the rule map in commands/babysit-prs.md:
  * credit exhaustion == RATE_LIMITED, an hourly-refill grind, NEVER a wall
    (bump it, never auto-stop) — 2026-06-28 incident.
  * UNSTABLE + a failing test check is RED, never cosmetic-yellow — 2026-06-16.
  * empty gh output after retries is FETCH_FAIL, never NO_REVIEW_YET/green
    (transient throttle under parallel bursts) — 2026-06-17.

Usage:
  python3 babysit_classify.py sweep [--repos a,b] [--state PATH] [--gh-bin gh]

Test/determinism env overrides (unset in production):
  BABYSIT_NOW             ISO8601 UTC "now"
  BABYSIT_RETRY_BACKOFF   float seconds between gh retries (0 in tests)
  BABYSIT_QUIET_OVERRIDE  short-circuit is_owner_quiet -> "yes:..."/"no:..."
  BABYSIT_REPOS_ROOT      root for the recent-commit quiet scan (default ~/code)
  BABYSIT_CONCURRENCY     worker threads for per-PR classification (default 5)
  BABYSIT_PROGRESS        path to hooks/babysit-progress.sh's JSON store, read
                          by the waiver-store loaders below (default
                          ~/.claude/babysit-progress.json)
  BABYSIT_WAIVE_BY        default `by` label for the ruling-comment subcommand
  BABYSIT_RULING_AUTHORS  comma-separated GitHub logins whose PR comments may
                          carry rulings (overrides RULING_AUTHORS_DEFAULT;
                          empty keeps the team-config default)
  BABYSIT_WAIVE_AUTHORIZED_BY  set (to the human's name) when a HUMAN
                          authorized the ruling — read by BOTH the store write
                          (hooks/babysit-progress.sh waive) and the
                          ruling-comment renderer, so the two records of one
                          ruling action cannot disagree
"""
import argparse
import concurrent.futures
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    ZoneInfo = None

# ---- TEAM CONFIG (edit these for your org) ---------------------------------
OWNER_DEFAULT = "your-org"          # GitHub org swept by `gh search prs --owner`
# GitHub logins allowed to author the comments this classifier TRUSTS — both
# ruling comments and CR-CLI harvest comments (comma-separated; overridden by
# $BABYSIT_RULING_AUTHORS). Both markers are public knowledge — they are in
# this source file — so on a public repo ANY account could post a
# marker-carrying comment: a spoofed ruling claiming `authorized-by-human:
# true`, or a spoofed "no findings" harvest that greens an unreviewed PR. The
# author gate is what keeps both channels' provenance to YOUR account(s): a
# comment from any other login is never parsed, however well-formed. An EMPTY
# list disables comment harvesting entirely (fail closed).
RULING_AUTHORS_DEFAULT = "your-github-login"

# ---- caps / limits (all lifted verbatim from the skill doc) ----------------
STALL_LIMIT = 12       # consecutive frozen non-credit sweeps before STALLED
BUMP_CAP = 3           # cloud @coderabbitai review bumps per sweep
REBASE_CAP = 3         # Step 4.7 auto-rebases per sweep
CI_TRIAGE_CAP = 2      # Step 4.6 red-CI triages per sweep
CLI_CAP = 3            # Step 4.5 CR-CLI launches per sweep
GH_ATTEMPTS = 4        # retry-on-empty attempts for EVERY gh call
STALE_SESSION_H = 6    # >6h since last_iter_at -> fresh session, reset streak

# ---- greens gate: cosmetic allowlist + RED regex (Step 5) ------------------
# COSMETIC = known non-gating deploy checks ONLY (explicit allowlist).
COSMETIC_RE = re.compile(r"(vercel|preview)", re.I)  # add your known non-gating deploy-check names
# RED regex ALWAYS wins: a check matching this is a real test/build/lint gate,
# never cosmetic — even if it also matches the cosmetic allowlist.
RED_RE = re.compile(
    r"(pytest|ci|test|build|lint|ruff|black|eslint|codegen|mypy|tsc|check)", re.I
)
FAIL_CONCL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"}

# ---- CR message phrasings (Step 2) -----------------------------------------
# Rate-limit AND credit-exhaustion phrasings are ONE state (RATE_LIMITED).
RATE_PHRASES = [
    "rate limit exceeded", "rate limited", "rate-limited",
    "prepaid credits", "credits have been exhausted",
    "ran out of credits", "out of credits", "credit balance is",
]
NO_ACTIONABLE = "no actionable comments were generated"
REVIEW_TRIGGERED = "review triggered"
AUTO_DISABLED = "auto reviews are disabled"  # stacked PR -> CR-CLI target
# CR emits these ONLY after a review actually completed. A `@coderabbitai review`
# bump on an unchanged commit returns "Review finished / does not re-review already
# reviewed commits" — which becomes the NEWEST comment and BURIES the original
# "no actionable comments were generated" summary. Treat them as positive "CR
# reviewed" signals so a reviewed-clean PR isn't mis-tagged NO_REVIEW_YET and
# bumped forever (which only buries the verdict deeper).
REVIEW_DONE_ACKS = ["review finished", "does not re-review already reviewed commits"]

# ---- CR-CLI harvest comments (the local-review channel) --------------------
# The header babysit writes when it posts a CR-CLI harvest. Authored by the
# sweep itself, not CodeRabbit, so nothing in the CR-state machine can match
# it — hence the dedicated cli_findings_open pass in classify_pr.
CLI_HARVEST_MARKER = "coderabbit cli review (local)"
# "**0 findings** — clean local review" / "3 findings, all author-lane".
# ONE optional adjective is allowed between the count and the noun: the
# harvest is LLM-narrated, and "**2 minor findings, both applied**" is a shape
# it actually writes. Exactly one `\w+` is permitted, not `\w+*`: an unbounded
# run would let a digit anywhere upstream reach across a whole sentence and
# bind to an unrelated "finding" later in the body.
CLI_FINDING_COUNT_RE = re.compile(r"(\d+)\s*\*{0,2}\s*(?:\w+\s+)?\*{0,2}finding")
# WHICH COMMIT the harvest actually reviewed: "Reviewed head `239c122` — ...".
# The harvest states this itself; parsing the comment for its finding COUNT
# while ignoring the SHA sitting beside it is how an unreviewed head once
# reached the merge gate. Backticks are optional; CR CLI has emitted both.
CLI_REVIEWED_HEAD_RE = re.compile(r"reviewed\s+head\s+[`'\"]?([0-9a-f]{7,40})", re.I)
# How many findings the harvest says are STILL UNAPPLIED. Preferred over the
# total: babysit routinely applies findings and posts the comment afterwards,
# so a total-based read files an already-fixed PR as needing a human.
CLI_UNAPPLIED_RE = re.compile(r"unapplied\s*=\s*(\d+)", re.I)
# An explicit ZERO, stated the two ways the harvest actually states it. The
# clean-pass comment carries a severity line + "**no findings.** Clean." —
# there is no digit before "finding" and no `unapplied=`, so the two regexes
# above both miss and _findings_left would return None ("unparseable -> stay
# open"), filing every clean CLI pass as unapplied findings.
CLI_SEVERITY_PAIR_RE = re.compile(r"\b(critical|major|minor|trivial)\s*=\s*(\d+)", re.I)
CLI_ZERO_FINDINGS_RE = re.compile(
    r"\bno\s+(?:actionable|new|remaining|outstanding|open|further|additional)?\s*findings?\b",
    re.I)
CLI_SEVERITY_KEYS = frozenset(("critical", "major", "minor", "trivial"))
# A CRITICAL, counted — not the mere presence of the word. `"critical" in
# body` matched "**1 finding, 0 critical.**" and every harvest carrying the
# machine-readable `severity: critical=0 ...` line. Accept only a positive
# count or an explicit critical marker/bullet; a COUNT is authoritative and
# markers only vote when no count exists.
CLI_CRITICAL_COUNTS = (
    re.compile(r"critical\s*=\s*(\d+)", re.I),          # severity: critical=2
    re.compile(r"(\d+)\s*\*{0,2}\s*critical\b", re.I),  # "6 findings, 2 CRITICAL"
)
CLI_CRITICAL_MARKERS = (
    re.compile(r"\*\*\s*(?:🔴\s*)?critical\b", re.I),   # a **critical** bullet
    re.compile(r"🔴"),                                   # CR's own red marker
)


def _sha_eq(a, b):
    """Do two git SHAs name the same commit? UNKNOWN on either side is False.

    Abbreviations vary by producer (the store keeps 9 chars, `headRefOid` is
    the full 40, CR CLI prints 7-9), so compare on the shorter length with a
    7-char floor — below that a prefix match is not evidence.

    Returning False for an unknown side is the whole point: every caller is
    asking a merge-gate question, and "I cannot tell" must never read as
    "yes".
    """
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


def _states_zero_findings(body):
    """True when the harvest explicitly says it found nothing.

    The severity line is GROUND TRUTH WHENEVER IT IS PRESENT — it decides both
    ways, and the prose is not consulted at all. The prose fallback exists only
    for a harvest that carries no severity line (an older shape).

    The two must not be independent OR-ed signals. The harvest body is
    LLM-narrated free prose, so `\\bno\\s+findings?\\b` matches sentences that
    are not a verdict — "1 finding. no findings were auto-applied", "2
    findings, no findings above major". OR-ing let those read as CLEAN despite
    their own `critical=1`/`major=2` line, and a false zero here is not a
    cosmetic miss: cli_open goes None -> cli_clean_at_head -> `strict` ->
    owner lane -> auto-merge and deploy, carrying an unread critical.
    Subordinating prose to the severity line is strictly narrowing: it only
    ever removes a path that returned 0.

    Two further collisions, both measured before the fix: a body with NO
    severity line still reached the prose branch, so `**1 finding.** no
    findings were auto-applied.` returned 0 — prose is therefore accepted only
    when the body carries no numeric count anywhere. And a stray partial
    mention was read as a whole verdict: `critical=0. Reviewed — **2
    findings.**` returned 0 — all four keys must be present before the line
    counts as the machine-readable histogram; a partial one is prose.
    """
    pairs = CLI_SEVERITY_PAIR_RE.findall(body)
    if pairs:
        if {name.lower() for name, _ in pairs} == CLI_SEVERITY_KEYS:
            return all(int(count) == 0 for _, count in pairs)
        return False
    return (CLI_FINDING_COUNT_RE.search(body) is None
            and bool(CLI_ZERO_FINDINGS_RE.search(body)))


def _has_critical(body):
    """True only when the harvest actually reports a critical.

    Precedence matters and cost two wrong human lists upstream. A bare
    `"critical" in body` matched "0 critical"; a marker voting independently
    flagged a body saying `critical=0 major=1` off the 🔴 on its MAJOR bullet.
    So: if the body states a count, that count IS the answer. Markers are the
    fallback for older harvests that predate the machine-readable line.
    """
    for rx in CLI_CRITICAL_COUNTS:
        m = rx.search(body)
        if m:
            return int(m.group(1)) > 0
    return any(rx.search(body) for rx in CLI_CRITICAL_MARKERS)


def _findings_left(body):
    """How many findings the harvest says REMAIN. None when unparseable.

    `unapplied=N` wins over the total: babysit applies findings and posts the
    comment afterwards, so the total overstates what is actually left for a
    human.

    ONE parse, deliberately. cli_open and cli_clean_at_head are complementary
    answers to the same question and BOTH gate a merge — duplicating the
    precedence rule meant a change to one copy alone could let a PR with
    unapplied findings reach `strict`.
    """
    mu = CLI_UNAPPLIED_RE.search(body)
    if mu:
        return int(mu.group(1))
    # An explicit zero, before the total: the clean-pass harvest states zero
    # in prose/severity only, so without this it parses as None and stays open
    # forever. `unapplied=` still outranks it — a body claiming both
    # `unapplied=3` and "no findings" is contradictory, and surfacing is the
    # safe direction.
    if _states_zero_findings(body):
        return 0
    m = CLI_FINDING_COUNT_RE.search(body)
    return int(m.group(1)) if m else None


# ---- human-ruling harvest ---------------------------------------------------
# The finding-level waiver store (load_waived_findings below) had no INPUT
# PATH: nothing converted a ruling a human makes ON THE PR into a
# waived_findings entry, so a finding the operator refuted with evidence in a
# PR comment re-entered the human queue every sweep, forever.
#
# The house rule rules out any design where a human types into GitHub or
# clicks "Resolve conversation" by hand: a human ruling is made INSIDE an
# agent session, and the session performs the one repeatable recording
# action, which is always BOTH of:
#   1. `babysit-progress.sh waive <repo>#<pr> <finding-key> "<reason>" <by>`
#      -- the durable store write (see load_waived_findings below).
#   2. Posting THIS structured comment to the PR via `gh` (never typed by a
#      human) -- the natural place, with the evidence, where reviewers can
#      see it, AND durability: harvest_ruling_comments() below re-derives the
#      same waiver straight from GitHub, so a ruling made in a session that
#      never touches this machine's store, or a store that was lost/reset,
#      still self-heals on the very next sweep.
# render_ruling_comment is the producer (commands/babysit-prs.md pipes its
# stdout into `gh pr comment`); harvest_ruling_comments is the consumer
# classify_pr calls. Keeping both in this one module means the format can
# never drift out of sync with its own parser.
#
# v2: v1 carried only `by`, and `by` is a caller-supplied string an agent can
# set to anything -- the exact reason the authorized-waiver rule refuses to
# read it as authorization. So a v1 comment can never clear a CRITICAL: there
# is nothing in it that distinguishes "a human ruled" from "an agent typed a
# human's name". v2 adds `authorized-by-human`, sourced from the SAME env var
# the authorized store write uses (BABYSIT_WAIVE_AUTHORIZED_BY, see
# hooks/babysit-progress.sh `waive`), so one ruling action still produces two
# records that agree -- and the comment cannot assert more authority than the
# store write taken beside it.
#
# v1 is still PARSED, never rejected: rulings already posted on open PRs must
# keep suppressing their (non-critical) findings. It simply reads as
# unauthorized, which is the fail-closed direction.
RULING_MARKER = "<!-- babysit:ruling v2 -->"
RULING_MARKER_V1 = "<!-- babysit:ruling v1 -->"
RULING_FINDING_RE = re.compile(r"^-\s*finding:\s*`?([^`\n]+?)`?\s*$", re.I | re.M)
RULING_REASON_RE = re.compile(r"^-\s*reason:\s*(.+)$", re.I | re.M)
RULING_BY_RE = re.compile(r"^-\s*by:\s*(.+)$", re.I | re.M)
RULING_AUTH_RE = re.compile(r"^-\s*authorized-by-human:\s*(\S+)\s*$", re.I | re.M)


def _one_line(s):
    """Collapse newlines out of a caller-supplied field before it is
    interpolated into the ruling comment. The parser is line-oriented, so a
    newline in `reason` or `by` would let a caller FORGE parser lines --
    verified attack: a reason of "nit\\n- authorized-by-human: true" renders a
    second authorization line that outranks the genuine `false` below it,
    clearing a CRITICAL with no BABYSIT_WAIVE_AUTHORIZED_BY set. A field can
    carry any prose it likes, on one line."""
    return re.sub(r"[\r\n]+", " ", str(s)).strip()


def render_ruling_comment(finding_key, reason, by, authorized_by_human=False):
    """The exact machine-readable body an agent session posts via `gh pr
    comment` when a human rules WAIVE on a finding. `<repo>#<pr>` is not
    embedded -- the comment already lives on that PR. finding_key/reason/by
    are exactly what `babysit-progress.sh waive <repo>#<pr> <finding-key>
    "<reason>" <by>` already takes, so one ruling action produces both durable
    records (the store write and this comment) from the same three inputs.
    Every caller-supplied field is collapsed to one line (_one_line) so no
    argument can smuggle extra parser lines into the body."""
    return (
        f"{RULING_MARKER}\n"
        f"**Babysit ruling: WAIVE**\n\n"
        f"- finding: `{_one_line(finding_key)}`\n"
        f"- reason: {_one_line(reason)}\n"
        f"- by: {_one_line(by)}\n"
        f"- authorized-by-human: {'true' if authorized_by_human else 'false'}\n"
    )


def _ruling_authors():
    """The set of GitHub logins allowed to author a ruling comment, lowered.

    $BABYSIT_RULING_AUTHORS (comma-separated) wins over the
    RULING_AUTHORS_DEFAULT team config. Empty -> empty set -> NO comment
    parses, which is the fail-closed direction: an unconfigured install must
    not let arbitrary commenters mint waivers.
    """
    raw = os.environ.get("BABYSIT_RULING_AUTHORS", "").strip() or RULING_AUTHORS_DEFAULT
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def harvest_ruling_comments(issues):
    """{finding-key: {reason, by, authorized_by_human, via}} harvested from
    structured ruling comments on a PR -- the on-GitHub mirror of
    load_waived_findings(), read from `issues` (the PR's issue-comment feed
    classify_pr already fetches). TWO gates, both required, both fail-closed:

    - AUTHOR: only a comment authored by a login in _ruling_authors() is
      considered at all. The marker is public knowledge (it is in this source
      file), so on a public repo ANY account could post a marker-carrying
      comment claiming `authorized-by-human: true` -- and the whole point of
      the authorization floor is that the comment must never be a cheaper
      route to clearing a finding than the operator's own ruling action.
    - MARKER: only a comment carrying the EXACT marker this module writes
      (render_ruling_comment) is parsed -- an arbitrary human-typed comment,
      however similarly worded, is NOT a ruling.

    A comment missing the finding line is skipped, not fatal -- same
    never-crash-the-sweep contract as the rest of this module.

    If more than one ruling exists for the same finding-key (a revised ruling
    with an updated reason), the NEWEST wins by `created_at` -- resolved
    explicitly here rather than assumed from list order, since the list's
    sort order is a caller/API convention, not a guarantee this function
    should depend on."""
    authors = _ruling_authors()
    best = {}  # finding-key -> (created_at, record)
    for c in issues or []:
        login = (((c.get("user") or {}).get("login")) or "").lower()
        if login not in authors:
            continue          # only the operator's own account(s) may rule
        body = c.get("body") or ""
        v2 = RULING_MARKER in body
        if not v2 and RULING_MARKER_V1 not in body:
            continue
        fm = RULING_FINDING_RE.search(body)
        if not fm:
            continue
        key = fm.group(1).strip()
        if not key:
            continue
        rm = RULING_REASON_RE.search(body)
        bm = RULING_BY_RE.search(body)
        # findall, not search: a body carrying MORE than one authorization
        # line was tampered with (the renderer emits exactly one), and the
        # only safe reading of a tampered authorization claim is no. This is
        # the parser-side half of the _one_line defence -- either alone can
        # be sidestepped (a hand-crafted comment skips the renderer).
        auths = RULING_AUTH_RE.findall(body) if v2 else []
        # Only the exact lowercase literal `true` authorizes -- not `TRUE`,
        # `yes`, or `1`. Absent, `false`, a v1 comment, and every other
        # spelling read UNAUTHORIZED. This is deliberately stricter than a
        # tolerant parse would be: render_ruling_comment always writes
        # lowercase `true`, so any OTHER spelling reaching here was typed by
        # hand into the one field that opens a CRITICAL, and the safe answer
        # to a hand-edited authorization claim is no. A human who really
        # means it re-runs the ruling with BABYSIT_WAIVE_AUTHORIZED_BY set;
        # the cost of failing closed is one blocked PR, the cost of failing
        # open is an unread critical shipped to prod.
        rec = {
            "reason": rm.group(1).strip() if rm else "",
            "by": bm.group(1).strip() if bm else "",
            "authorized_by_human": (len(auths) == 1
                                    and auths[0].strip() == "true"),
            "via": "pr_comment",
        }
        at = c.get("created_at") or ""
        if key not in best or at > best[key][0]:
            best[key] = (at, rec)
    return {k: rec for k, (_, rec) in best.items()}


# ---- lane bucketing (Step 5 table) -----------------------------------------
# Lane bucketing config (edit for your org):
#   OWNER_LANE_REPOS      repos where all clean PRs are yours to merge
#   TEAM_CARVEOUT_REPOS   shared repos where PRs matching TEAM_CARVEOUT_KEYWORD
#                         (in labels/branch/title) belong to the team's lane
#   SECONDARY_PREFIXES    repo-name prefixes for a product with a feature→develop
#                         →main flow (develop-target greens are your call; a
#                         main-target green is a cohort/stack-root unblocker)
OWNER_LANE_REPOS = {
    "acme-api", "acme-frontend", "acme-agents",
}
TEAM_CARVEOUT_REPOS = {"acme-platform-api", "acme-platform-web"}
TEAM_CARVEOUT_KEYWORD = "studio"
SECONDARY_PREFIXES = ("acme-writer",)

# CR states considered PENDING (loop can still advance them) vs TERMINAL.
PENDING_STATES = {
    "HAS_ACTIONABLE", "RATE_LIMITED", "NO_REVIEW_YET",
    "TRIGGERED_WAITING", "STACKED_BLOCKED", "FETCH_FAIL",
}


# ============================================================================
# time helpers (BABYSIT_NOW makes every time-dependent branch deterministic)
# ============================================================================
def parse_iso(s):
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def now_dt():
    v = os.environ.get("BABYSIT_NOW", "").strip()
    if v:
        dt = parse_iso(v)
        if dt:
            return dt
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backoff_s():
    try:
        return float(os.environ.get("BABYSIT_RETRY_BACKOFF", "1.5"))
    except ValueError:
        return 1.5


def age_min(ts, now):
    dt = ts if isinstance(ts, datetime) else parse_iso(ts)
    if not dt:
        return None
    return (now - dt).total_seconds() / 60.0


def older_than(ts, now, mins):
    """True if ts is older than `mins` minutes (a missing ts counts as old)."""
    a = age_min(ts, now)
    return a is None or a > mins


# ============================================================================
# gh runner — retry-on-empty for EVERY call (transient-throttle guard)
# ============================================================================
def gh_raw(gh_bin, args, attempts=GH_ATTEMPTS):
    """Run gh, retrying while stdout is empty. Returns stdout str, or None
    (FETCH_FAIL sentinel) after `attempts` empties. A successful list endpoint
    prints at least '[]' (non-empty); only a transient failure prints nothing —
    that distinction is what tells real-NO_CR from a miss."""
    back = backoff_s()
    for i in range(attempts):
        try:
            p = subprocess.run(
                [gh_bin] + [str(a) for a in args],
                capture_output=True, text=True, timeout=60,
            )
        except Exception:
            p = None
        if p is not None and p.returncode == 0 and p.stdout.strip():
            return p.stdout
        if i < attempts - 1 and back > 0:
            time.sleep(back * (i + 1))
    return None


def gh_json(gh_bin, args, attempts=GH_ATTEMPTS):
    raw = gh_raw(gh_bin, args, attempts)
    if raw is None:
        return None  # FETCH_FAIL — never coerce to [] / NO_CR / green
    try:
        return json.loads(raw)
    except Exception:
        return None


def is_cr(obj):
    login = ((obj or {}).get("user") or {}).get("login", "") or ""
    return bool(re.search("coderabbit", login, re.I))


# ============================================================================
# greens gate helpers
# ============================================================================
def failing_check_names(scr):
    out = []
    for c in scr or []:
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        if concl in FAIL_CONCL:
            out.append(c.get("name") or c.get("context") or "?")
    return out


def check_is_cosmetic(name):
    # RED always wins: allowlisted AND not a real test/build/lint gate.
    return bool(COSMETIC_RE.search(name)) and not bool(RED_RE.search(name))


# ============================================================================
# lane bucketing
# ============================================================================
def lane_of(repo, labels, branch, title, base):
    r = (repo or "").lower()
    txt = " ".join(labels).lower()
    kw = TEAM_CARVEOUT_KEYWORD
    is_team = (kw in txt or kw in (branch or "").lower()
               or kw in (title or "").lower())
    if r in TEAM_CARVEOUT_REPOS:
        return "team" if is_team else "owner"
    if r.startswith(SECONDARY_PREFIXES):
        return "secondary_cohort" if (base or "") in ("main", "master") else "secondary"
    if r in OWNER_LANE_REPOS:
        return "owner"
    return "owner"  # utility / unknown default to the owner's lane


# ============================================================================
# per-PR classification
# ============================================================================
def fetch_view(gh_bin, owner, repo, num):
    fields = ("state,headRefName,headRefOid,mergeable,mergeStateStatus,"
              "baseRefName,statusCheckRollup")
    v = gh_json(gh_bin, ["-R", f"{owner}/{repo}", "pr", "view", num, "--json", fields])
    if v is None:
        return None
    # UNKNOWN -> GitHub still computing; re-poll once (Step 5).
    if v.get("mergeable") == "UNKNOWN" or v.get("mergeStateStatus") == "UNKNOWN":
        b = backoff_s()
        if b > 0:
            time.sleep(min(b, 2))
        v2 = gh_json(gh_bin, ["-R", f"{owner}/{repo}", "pr", "view", num, "--json", fields])
        if v2 is not None:
            v = v2
    return v


def _fetch_fail(repo, pr):
    return {
        "repo": repo, "number": pr["number"], "branch": "",
        "state": "FETCH_FAIL", "mergeable": None, "mss": None,
        "failing_checks": [], "red_failing": [], "tier": "", "lane": "owner",
        "last_cr_activity": "", "base": "",
        "created_at": pr.get("createdAt", ""), "cr_inline_count": 0,
        "blurb": pr.get("title", ""),
        # Same shape as a full entry — the doc promises these keys on EVERY
        # prs[] row, and a renderer must never KeyError on a transient
        # FETCH_FAIL.
        "cli_findings_open": None, "ruled_via_pr_comment": [], "green_via": "",
    }


def classify_pr(gh_bin, pr, now, review_recs=None, waived_findings=None):
    owner, repo, num = pr["_owner"], pr["_repo"], str(pr["number"])
    labels = [(l.get("name") or "") for l in (pr.get("labels") or [])]

    view = fetch_view(gh_bin, owner, repo, num)
    if view is None:
        return _fetch_fail(repo, pr)

    branch = view.get("headRefName") or ""
    base = view.get("baseRefName") or ""
    mergeable = view.get("mergeable")
    mss = view.get("mergeStateStatus")
    scr = view.get("statusCheckRollup") or []

    reviews = gh_json(gh_bin, ["api", f"repos/{owner}/{repo}/pulls/{num}/reviews?per_page=100"])
    inline = gh_json(gh_bin, ["api", f"repos/{owner}/{repo}/pulls/{num}/comments?per_page=100&sort=created&direction=desc"])
    issues = gh_json(gh_bin, ["api", f"repos/{owner}/{repo}/issues/{num}/comments?per_page=100&sort=created&direction=desc"])
    # empty-after-retries on ANY CR-data fetch -> FETCH_FAIL, never NO_CR/green.
    if reviews is None or inline is None or issues is None:
        e = _fetch_fail(repo, pr)
        e["branch"] = branch
        e["base"] = base
        return e

    cr_reviews = [r for r in reviews if is_cr(r)]
    cr_inline = [c for c in inline if is_cr(c)]
    cr_issues = [c for c in issues if is_cr(c)]

    def cts(c):
        return c.get("created_at") or c.get("submitted_at")

    # last push on the branch — needed to age inline comments AND harvest
    # comments. A CR-CLI harvest comment needs ageing against last_push
    # exactly like an inline finding does, and it can be the ONLY thing on
    # the PR — gating the fetch on cloud-CR artifacts alone left last_push
    # None there, so every harvest looked permanently unaddressed no matter
    # how many fixes landed.
    # SAME author gate as harvest_ruling_comments, for the same reason: the
    # harvest marker is public knowledge, and an un-gated harvest channel
    # lets ANY commenter on a public repo either green a PR (post a fake
    # "no findings" harvest at the live head) or bury a real critical one (a
    # newer fake harvest wins max-by-created_at below). Only comments your
    # own automation authored count as harvests.
    _trusted = _ruling_authors()
    cli_harvests = [
        c for c in issues
        if CLI_HARVEST_MARKER in ((c.get("body") or "").lower())
        and ((((c.get("user") or {}).get("login")) or "").lower() in _trusted)
    ]
    last_push = None
    if cr_inline or cr_reviews or cli_harvests:
        cd = gh_json(gh_bin, ["api", f"repos/{owner}/{repo}/commits/{branch}"])
        if cd is None:
            e = _fetch_fail(repo, pr)  # can't age inline -> can't classify
            e["branch"] = branch
            e["base"] = base
            return e
        last_push = parse_iso(((cd.get("commit") or {}).get("committer") or {}).get("date"))

    # Only thread-ROOT comments are findings. CR's in-thread REPLIES are
    # conversation — "Skipped: comment is from another GitHub bot" acks, or
    # withdrawal replies — and CR posts them whenever anyone answers in a
    # thread, so counting them re-flags an already-settled PR on every sweep
    # until the head happens to move. A root CR explicitly withdrew (reply
    # carrying <review_comment_withdrawn>) is dead regardless of age.
    withdrawn_roots = {
        c.get("in_reply_to_id")
        for c in cr_inline
        if c.get("in_reply_to_id") and "review_comment_withdrawn" in (c.get("body") or "")
    }
    actionable = []
    if cr_inline and last_push:
        for c in cr_inline:
            if c.get("in_reply_to_id"):
                continue  # reply, not a finding
            if c.get("id") in withdrawn_roots:
                continue  # CR withdrew this finding in-thread
            t = parse_iso(cts(c))
            if t and t > last_push:
                actionable.append(c)

    # last CR activity timestamp (bump rotation + fingerprint).
    times = []
    for c in cr_inline + cr_issues:
        t = parse_iso(cts(c))
        if t:
            times.append(t)
    for r in cr_reviews:
        t = parse_iso(r.get("submitted_at"))
        if t:
            times.append(t)
    last_cr = iso(max(times)) if times else ""

    def latest(lst, key):
        cand = [(parse_iso(x.get(key)), x) for x in lst if parse_iso(x.get(key))]
        return max(cand, key=lambda p: p[0])[1] if cand else None

    li = latest(cr_issues, "created_at")
    li_body = ((li or {}).get("body") or "").lower()
    lr = latest(cr_reviews, "submitted_at")
    lr_body = ((lr or {}).get("body") or "").lower()

    def has_rate(t):
        return any(p in t for p in RATE_PHRASES)

    # A completed "0 actionable" verdict can sit ANYWHERE in the comment history —
    # a later bump-ack often becomes the newest comment and buries it. So scan ALL
    # CR comments/reviews for it, not just the latest (li_body/lr_body).
    def _body(c):
        return (c.get("body") or "").lower()
    any_no_actionable = (
        any(NO_ACTIONABLE in _body(c) for c in cr_issues)
        or any(NO_ACTIONABLE in _body(r) for r in cr_reviews)
    )
    # "Review finished"/incremental-skip acks prove CR reviewed even when the
    # original summary is buried under bump-acks.
    review_done_ack = any(any(a in _body(c) for a in REVIEW_DONE_ACKS) for c in cr_issues)

    # --- classify into ONE CR state (order = precedence) ---
    if actionable:
        state = "HAS_ACTIONABLE"
    elif has_rate(li_body):
        state = "RATE_LIMITED"          # credit-exhausted == rate-limited (ONE state)
    elif any_no_actionable:
        state = "CLEAN"                 # CR posted a "0 actionable" summary (scan ALL, not just newest)
    elif AUTO_DISABLED in li_body:
        state = "STACKED_BLOCKED"       # cloud rejects stacked base -> CR-CLI target
    elif REVIEW_TRIGGERED in li_body:
        state = "TRIGGERED_WAITING"
    elif cr_inline or cr_reviews or review_done_ack:
        state = "CLEAN"                 # reviewed, nothing actionable outstanding (ack proves review happened)
    else:
        state = "NO_REVIEW_YET"

    # --- CR-CLI harvest -> open-findings signal (the local-review channel) ---
    # The harvest comment is authored by the sweep itself, not CodeRabbit, so
    # the CR-state machine above — which only inspects `is_cr` comments —
    # cannot see it by construction. A finding babysit declines to auto-apply
    # was therefore posted to the PR and then tracked by NOTHING: never
    # HAS_ACTIONABLE, never surfaced to a human, never re-raised.
    #
    # Aged against last_push on the SAME rule as CR inline findings: a harvest
    # comment older than the newest commit is treated as addressed, so pushing
    # a fix clears it without any explicit resolve step.
    #
    # Computed BEFORE the green tier because a clean harvest is a green input,
    # not just reporting (see cli_clean_at_head below).
    newest_harvest = None
    cli_open = None
    _cli_open_cleared_by_waiver = False
    if cli_harvests:
        newest_harvest = max(cli_harvests, key=lambda c: c.get("created_at") or "")
        at = parse_iso(newest_harvest.get("created_at") or "")
        if at and (last_push is None or at > last_push):
            hbody = _body(newest_harvest)
            # A CLEAN harvest is not a finding — babysit posts a comment even
            # when it found nothing, so flagging every harvest newer than the
            # last push marks reviewed-CLEAN PRs as needing a human. Parse the
            # count; 0 means clean. An UNPARSEABLE count stays open on
            # purpose: over-surfacing costs a glance, under-surfacing is how a
            # critical goes unread.
            left = _findings_left(hbody)
            if left != 0:
                cli_open = {
                    "at": iso(at),
                    "findings": left,
                    "critical": _has_critical(hbody),
                    "excerpt": (newest_harvest.get("body") or "")[:400],
                }

    # FINDING-LEVEL WAIVERS reduce the OPEN COUNT. Deliberately NOT the same
    # treatment as known_fp (the PR-level escape hatch), which leaves
    # cli_findings_open untouched on purpose — known_fp exists to unblock a
    # whole PR while still SHOWING the raw signal everywhere else. A
    # finding-level waiver targets ONE adjudicated finding, so any OTHER,
    # genuinely new finding on the same PR must still surface.
    #
    # The harvest only gives an AGGREGATE count + a critical flag, never
    # individual finding identities — state is re-derived from GitHub each
    # sweep with no durable per-finding store to correlate against. So each
    # active waiver is presumed to account for exactly one recurring
    # re-raised finding and the count is subtracted directly; if the review
    # raises MORE findings than there are waivers, the residual still
    # surfaces (nothing is over-suppressed).
    #
    # Harvest any structured ruling comments posted on THIS PR and union them
    # into the effective waived set, keyed by finding (a key present in both
    # sources counts once). Computed whether or not the store already has the
    # entry — it is the durability path: a ruling that never reached (or has
    # since fallen out of) the local store still lands here, straight from
    # GitHub. `ruled_via_pr_comment` records which finding-keys this sweep
    # resolved via the PR comment specifically (telemetry — "ruled on" vs
    # "awaiting a human" — independent of whether the store agrees).
    _wkey = "%s#%s" % (repo, pr["number"])
    _harvested_rulings = harvest_ruling_comments(issues)
    ruled_via_pr_comment = sorted(_harvested_rulings.keys())

    if cli_open is not None and cli_open["findings"] is not None:
        _effective_waived = dict((waived_findings or {}).get(_wkey) or {})
        _effective_waived.update(_harvested_rulings)
        _waived_n = len(_effective_waived)
        if _waived_n:
            # A WAIVER NEVER ZEROES A CRITICAL UNLESS A HUMAN AUTHORIZED IT.
            # The aggregate signal cannot say whether the waived finding WAS
            # the critical one, and zeroing `cli_open` is what removes the
            # merge block. So a critical clears only when every active waiver
            # carries `authorized_by_human: true` — the flag records which
            # PATH the store write took (see hooks/babysit-progress.sh
            # `waive`), never `by`, which is a caller-supplied string an
            # agent can set to anything. The check is shape-aware — a bare
            # string can never assert authorization.
            #
            # A PR-comment ruling therefore does NOT clear a critical on its
            # own: it floors at one and the PR keeps waiting for a named
            # human. The marker proves the ruling came from the session's
            # ruling action, not that a human authorized waiving a CRITICAL.
            # Non-critical findings still clear from a PR comment alone,
            # which is the durability path the ruling harvest was built for.
            _human_ok = all(
                isinstance(w, dict) and w.get("authorized_by_human") is True
                for w in _effective_waived.values()
            )
            _floor = 0 if (_human_ok or not cli_open.get("critical")) else 1
            _eff_n = max(_floor, cli_open["findings"] - _waived_n)
            if _eff_n == 0:
                cli_open = None
                # Remember WHY cli_open is None. `cli_clean_at_head` below
                # needs to tell "the harvest reported zero" apart from "the
                # harvest reported findings and every one of them has been
                # adjudicated"; the raw comment body reads `unapplied=1` in
                # both the second case and the genuinely-open case, so it
                # cannot answer this on its own.
                _cli_open_cleared_by_waiver = True
            else:
                cli_open = dict(cli_open, findings=_eff_n)

    # "AT HEAD" IS AN IDENTITY QUESTION, NOT A RACE BETWEEN TWO CLOCKS. The
    # harvest flow is review -> apply fixes -> commit -> push -> POST THE
    # COMMENT, so on every PR babysit fixed something on, the comment is
    # newer than the push it does NOT cover, by construction. Require the
    # reviewed SHA to BE the live head. Two independent sources name it (the
    # comment's own "Reviewed head", and the head `set-cli-review` recorded);
    # ANY known source that disagrees vetoes, and if NO source knows one we
    # cannot prove coverage and do not claim it. The clock test is kept as
    # well: it can only ever narrow what greens, never widen it.
    head_oid = view.get("headRefOid") or ""
    rec = (review_recs or {}).get(_wkey) or {}
    _hm = (CLI_REVIEWED_HEAD_RE.search(_body(newest_harvest))
           if newest_harvest is not None else None)
    _cli_heads = [h for h in (_hm.group(1) if _hm else "", rec.get("head") or "") if h]
    cli_covers_head = bool(_cli_heads and all(_sha_eq(h, head_oid) for h in _cli_heads))

    # A CLEAN CR-CLI REVIEW AT HEAD IS A REAL REVIEW. Without this, `greens`
    # accepts CLOUD review only, so a PR whose sole review was a local CR-CLI
    # pass can never merge however clean it came back — on a RATE_LIMITED PR
    # the CLI is the only channel there is. The CLI greens a PR on the same
    # terms the cloud does: the harvest must POSTDATE the head commit, it
    # must report 0 open findings, and it never overrules an outstanding
    # cloud finding (HAS_ACTIONABLE always wins) or a red check.
    cli_clean_at_head = False
    if newest_harvest is not None and cli_open is None and state != "HAS_ACTIONABLE":
        at_h = parse_iso(newest_harvest.get("created_at") or "")
        if at_h and last_push and at_h >= last_push and cli_covers_head:
            # AN ADJUDICATED FINDING IS CLEAN AT HEAD. The waiver machinery
            # above takes `cli_open` to None, but the raw harvest body still
            # says `unapplied=1` however the finding was adjudicated — so a
            # ruled PR dropped out of the human queue AND could never reach a
            # green tier: invisible in both directions, which is worse than
            # either failure alone, because nothing surfaces it and it is
            # only ever re-adjudicated. The waiver flag separates the two
            # readings of `cli_open is None`; the RAW parse is kept as the
            # other accepted condition. Every other gate is untouched —
            # `cli_covers_head`, the staleness clock and the HAS_ACTIONABLE
            # veto all still apply, an UNPARSEABLE count never reaches here
            # (it leaves `cli_open` non-None on purpose), and criticals stay
            # governed upstream by the authorized_by_human floor, so this can
            # only ever green a finding a named human ruled on.
            cli_clean_at_head = (_findings_left(_body(newest_harvest)) == 0
                                 or _cli_open_cleared_by_waiver)

    # --- green tier (only CR-CLEAN PRs are green candidates) ---
    failing = failing_check_names(scr)
    red_failing = [n for n in failing if not check_is_cosmetic(n)]

    # Two ways to earn a review: a cloud CR review (state CLEAN), or a clean
    # CR-CLI pass at head. Either one opens the SAME tier ladder — the review
    # channel decides nothing past this point, but it is recorded (green_via)
    # so a merge is never silently attributed to the wrong channel.
    green_via = ("cloud" if state == "CLEAN"
                 else "cli" if cli_clean_at_head else "")

    # AN UNAPPLIED CRITICAL IS NEVER GREEN. cli_findings_open and the green
    # tier used to be computed independently, so a PR could be surfaced to a
    # human as carrying a critical AND merged by the same sweep — the cloud
    # said "0 actionable" while the local harvest reported critical=1. Only
    # criticals block: a non-critical unapplied finding still greens.
    # red_ci is assigned FIRST so the PR keeps earning a ci_triage action.
    cli_critical_open = bool((cli_open or {}).get("critical"))

    tier = ""
    if state == "CLEAN" or cli_clean_at_head:
        if red_failing:
            tier = "red_ci"             # RED check -> NEVER a green of any tier
        elif cli_critical_open:
            tier = ""                   # unapplied CRITICAL -> not green, any tier
        elif mergeable == "MERGEABLE" and mss == "CLEAN" and not failing:
            tier = "strict"
        elif mergeable == "MERGEABLE" and mss == "UNSTABLE" and failing and not red_failing:
            tier = "cosmetic_yellow"
    if not tier:
        green_via = ""

    return {
        "repo": repo, "number": pr["number"], "branch": branch,
        "cli_findings_open": cli_open,
        # Finding-keys ruled WAIVE via a structured PR comment this sweep
        # (harvest_ruling_comments), regardless of whether the local
        # waived_findings store also has them. Telemetry — "ruled on" vs
        # "awaiting a human" — aggregated into sweep()'s ruled_via_pr_comment.
        "ruled_via_pr_comment": ruled_via_pr_comment,
        "green_via": green_via,
        "state": state, "mergeable": mergeable, "mss": mss,
        "failing_checks": failing, "red_failing": red_failing,
        "tier": tier,
        "lane": lane_of(repo, labels, branch, pr.get("title", ""), base),
        "last_cr_activity": last_cr, "base": base,
        "created_at": pr.get("createdAt", ""),
        "cr_inline_count": len(cr_inline),
        "blurb": pr.get("title", ""),
    }


# ============================================================================
# quiet detection (Step 4.5a)
# ============================================================================
def is_owner_quiet(now):
    ov = os.environ.get("BABYSIT_QUIET_OVERRIDE", "").strip()
    if ov:
        return ov
    if ZoneInfo is not None:
        try:
            la = now.astimezone(ZoneInfo("America/Los_Angeles"))
            if 2 <= la.hour < 9:
                return "yes:off-hours"
        except Exception:
            pass
    root = os.environ.get("BABYSIT_REPOS_ROOT", os.path.expanduser("~/code"))
    authors = [a.strip() for a in os.environ.get(
        "BABYSIT_GIT_AUTHORS", "your-github-login").split(",") if a.strip()]
    cutoff = iso(now - timedelta(minutes=30))
    try:
        dirs = sorted(glob.glob(os.path.join(root, "*")))
    except Exception:
        dirs = []
    for d in dirs:
        if not os.path.isdir(os.path.join(d, ".git")):
            continue
        try:
            r = subprocess.run(
                ["git", "-C", d, "log", "--since", cutoff]
                + [a for login in authors for a in ("--author", login)]
                + ["-1", "--oneline"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return f"no:recent-commit-in-{os.path.basename(d)}"
        except Exception:
            continue
    return "yes:no-recent-activity"


# ============================================================================
# planning
# ============================================================================
def _sortkey_cr(e):
    dt = parse_iso(e["last_cr_activity"])
    return dt or datetime(1970, 1, 1, tzinfo=timezone.utc)


def plan_bumps(entries, now):
    """RATE_LIMITED with last CR comment >50 min old, oldest-last-CR-activity
    first, cap 3. Fill spare budget with stale NO_REVIEW_YET / TRIGGERED_WAITING.
    If any RATE_LIMITED PR is refill-ready, this MUST return bumps."""
    picks = []
    rl = [e for e in entries if e["state"] == "RATE_LIMITED"
          and older_than(e["last_cr_activity"], now, 50)]
    rl.sort(key=_sortkey_cr)                      # oldest-bumped first (rotate)
    picks.extend(rl[:BUMP_CAP])

    if len(picks) < BUMP_CAP:
        nr = [e for e in entries if e["state"] == "NO_REVIEW_YET"
              and older_than(e["created_at"], now, 30)]
        nr.sort(key=lambda e: parse_iso(e["created_at"]) or datetime(1970, 1, 1, tzinfo=timezone.utc))
        for e in nr:
            if len(picks) >= BUMP_CAP:
                break
            picks.append(e)

    if len(picks) < BUMP_CAP:
        tw = [e for e in entries if e["state"] == "TRIGGERED_WAITING"
              and older_than(e["last_cr_activity"], now, 60)]
        tw.sort(key=_sortkey_cr)
        for e in tw:
            if len(picks) >= BUMP_CAP:
                break
            picks.append(e)
    return picks


# ============================================================================
# waiver-store readers — hooks/babysit-progress.sh is the writer. classify_pr
# populates each entry's `cli_findings_open` from a CR-CLI harvest and
# subtracts these waivers from it; plan_applies() below ranks the resulting
# apply queue for the skill (not yet wired into build_actions()/sweep()).
# ============================================================================
def load_cli_reviews(path=None):
    """{"repo#pr": {head, sev{critical,major,minor,trivial}, total, rounds}}.

    The severity histogram + round count the harvest records via
    `babysit-progress.sh set-cli-review`. Missing/corrupt store -> {} so a
    planning hint can never crash the sweep.
    """
    p = path or os.environ.get(
        "BABYSIT_PROGRESS", os.path.expanduser("~/.claude/babysit-progress.json"))
    try:
        with open(p) as fh:
            data = json.load(fh)
    except Exception:
        return {}
    out = {}
    for k, v in (data.get("cli_reviewed") or {}).items():
        if not isinstance(v, dict):
            continue
        sev = v.get("sev") if isinstance(v.get("sev"), dict) else None
        try:
            rounds = int(v.get("rounds") or 0)
        except (TypeError, ValueError):
            rounds = 0  # non-numeric rounds -> same never-crash contract as a missing store
        out[k] = {
            "head": v.get("head") or "",
            "at": v.get("at") or "",
            "sev": sev,
            "total": v.get("total"),
            "rounds": rounds,
        }
    return out


def load_cli_reviewed_heads(path=None):
    """{"repo#pr": "<head sha>"} for every PR CR-CLI has already reviewed.

    Same store the skill writes via `babysit-progress.sh set-cli-head`. A
    planner uses this to avoid proposing a launch the skill's head gate would
    immediately hold. Missing/unreadable/corrupt store -> {} (propose
    everything) — a planning hint must never be able to crash the sweep.
    """
    p = path or os.environ.get(
        "BABYSIT_PROGRESS", os.path.expanduser("~/.claude/babysit-progress.json"))
    try:
        with open(p) as fh:
            data = json.load(fh)
    except Exception:
        return {}
    out = {}
    for k, v in (data.get("cli_reviewed") or {}).items():
        head = v.get("head") if isinstance(v, dict) else v
        if isinstance(head, str) and head:
            out[k] = head
    return out


def load_known_fp(path=None):
    """{"repo#pr": reason} for every PR flagged a known false positive.

    Read side of the PR-level waiver store `hooks/babysit-progress.sh`
    writes via `add-fp` (and clears via `clear-fp`). It exists here because
    an unapplied-findings launch gate (a later chunk) needs a way to re-open
    a PR whose findings were legitimately declined rather than ignored:
    gating on `cli_findings_open` alone can never clear on its own when
    nobody pushes a fix, which would freeze such a PR out of review forever.
    Same store the skill writes (`BABYSIT_PROGRESS`, defaulting to
    ~/.claude/babysit-progress.json). Missing/unreadable/corrupt store -> {}
    (nothing waived) — a planning hint must never crash the sweep.
    """
    p = path or os.environ.get(
        "BABYSIT_PROGRESS", os.path.expanduser("~/.claude/babysit-progress.json"))
    try:
        with open(p) as fh:
            data = json.load(fh)
    except Exception:
        return {}
    out = {}
    for k, v in (data.get("known_fp") or {}).items():
        if isinstance(v, dict):
            out[k] = v.get("reason") or ""
        elif isinstance(v, str):
            out[k] = v
    return out


def load_waived_findings(path=None):
    """{"repo#pr": {"<finding-key>": {reason, authorized_by_human}}} for every
    individually-waived CR-CLI finding.

    Finding-level counterpart to load_known_fp() above. known_fp waives an
    entire PR -- silencing it to kill one re-raised finding also blinds the
    PR to every future REAL finding, which is exactly the gap this closes.
    This is keyed one level deeper: repo#pr -> finding-key (caller-composed
    at waive time, typically "<file>:<rule-id>" -- deliberately excludes the
    line number and head SHA, both of which move on every push, so the
    waiver survives a force-push/rebase without being re-applied) -> reason.
    Written by `hooks/babysit-progress.sh waive`, cleared by `unwaive`.

    Same store the skill writes (`BABYSIT_PROGRESS`, defaulting to
    ~/.claude/babysit-progress.json). Missing/unreadable/corrupt store, or a
    store written before this key existed -> {} (nothing waived) -- same
    absent-key contract as load_known_fp/load_cli_reviewed_heads: a planning
    hint must never crash the sweep.
    """
    p = path or os.environ.get(
        "BABYSIT_PROGRESS", os.path.expanduser("~/.claude/babysit-progress.json"))
    try:
        with open(p) as fh:
            data = json.load(fh)
    except Exception:
        return {}
    out = {}
    for k, v in (data.get("waived_findings") or {}).items():
        if not isinstance(v, dict):
            continue
        keyed = {}
        for fk, rec in v.items():
            # `authorized_by_human` must survive normalisation: it is what
            # decides whether a waiver may clear a CRITICAL, and this loader
            # used to flatten each record to its reason string, so the flag
            # could never reach the read side at all. Absent, non-boolean, or
            # a legacy bare-string record -> False: an unprovable
            # authorisation is not an authorisation.
            if isinstance(rec, dict):
                keyed[fk] = {
                    "reason": rec.get("reason") or "",
                    "authorized_by_human": rec.get("authorized_by_human") is True,
                }
            elif isinstance(rec, str):
                keyed[fk] = {"reason": rec, "authorized_by_human": False}
        if keyed:
            out[k] = keyed
    return out


# ============================================================================
# apply-queue ranking (fed by classify_pr's `cli_findings_open` field)
# ============================================================================
# Lanes that can never be merged BY THIS AUTOMATION (a different team holds
# the button, or the base disqualifies it) — see lane_of()'s "team"/
# "secondary"/"secondary_cohort" buckets.
APPLY_DEPRIORITIZED_LANES = ("secondary", "secondary_cohort", "team")


def _apply_lane_rank(lane):
    """0 = owner lane (this automation's own merges), 1 = everything else."""
    return 1 if lane in APPLY_DEPRIORITIZED_LANES else 0


def _mergeability_distance(e):
    """How many mechanical steps stand between 'findings applied' and
    'merged', lowest first.

    A MERGEABLE + CLEAN PR needs nothing else once its findings land -- the
    apply IS the last step to `strict`, so those findings are worth the most
    per apply. Distance rises with every additional problem an apply alone
    cannot fix (a red check, a stale/conflicting/blocked branch): applying
    there still leaves the PR unmergeable, so it is worth less right now even
    though the finding is just as real."""
    if e.get("red_failing"):
        return 3
    if e.get("mergeable") != "MERGEABLE":
        return 3
    mss = e.get("mss")
    if mss in ("BEHIND", "DIRTY", "CONFLICTING", "BLOCKED"):
        return 2
    if mss == "UNSTABLE":
        return 1
    return 0


def plan_applies(entries, known_fp=None):
    """Rank PRs carrying already-harvested, unapplied CR-CLI findings.

    Sort key: (lane rank, mergeability-distance, critical-first, -findings,
    repo, number). Deterministic and side-effect-free -- same shape as
    plan_bumps: this decides ORDER only, never whether/how a finding gets
    applied or declined (that stays the applying worker's call, out of scope
    here).

    known_fp EXCLUSION. Finding-level waivers already fall out of this for
    free: they reduce cli_findings_open itself inside classify_pr, so a
    fully-waived PR's `findings` count is already 0/absent by the time it
    reaches here. known_fp is different --
    it deliberately leaves cli_findings_open untouched (the PR-wide waiver
    still SHOWS the raw signal everywhere else), so without an explicit
    check here a whole-PR-waived PR still showed up in the apply queue -- a
    finding adjudicated away must not schedule work either."""
    known_fp = known_fp or {}
    cands = [e for e in entries
             if (e.get("cli_findings_open") or {}).get("findings")
             and ("%s#%s" % (e["repo"], e["number"])) not in known_fp]
    cands.sort(key=lambda e: (
        _apply_lane_rank(e["lane"]),
        _mergeability_distance(e),
        0 if e["cli_findings_open"].get("critical") else 1,
        -(e["cli_findings_open"]["findings"] or 0),
        e["repo"], e["number"],
    ))
    return cands


def build_actions(entries, now, quiet):
    actions = []
    known_fp = load_known_fp()

    # HAS_ACTIONABLE -> fix (skill applies mechanical CR fixes verbatim)
    # known_fp EXCLUSION: plan_applies already excludes a whole-PR known_fp
    # waiver from the apply queue; this loop needs the same treatment, or a
    # PR whose CR findings were adjudicated as false positives keeps getting
    # re-planned as a `fix` target every sweep — pure report noise. Same
    # precedent as plan_applies: known_fp silences the PLANNED ACTION only,
    # never classify_pr's raw `state` (still reports HAS_ACTIONABLE in the
    # harvest). rebase/ci_triage deliberately do NOT get this check —
    # known_fp means "the review's findings were a false positive", not
    # "the branch is current" or "CI is green"; adding it there would strand
    # a legitimately-behind or red-CI PR forever.
    for e in entries:
        if e["state"] == "HAS_ACTIONABLE":
            if "%s#%s" % (e["repo"], e["number"]) in known_fp:
                continue
            actions.append({
                "type": "fix", "repo": e["repo"], "pr": e["number"],
                "why": "CR left actionable inline comment(s) newer than last push",
                "verify_open": True,
            })

    # bumps (Step 3/4) — oldest-first, cap 3
    bumps = plan_bumps(entries, now)
    for e in bumps:
        actions.append({
            "type": "bump", "repo": e["repo"], "pr": e["number"],
            "why": f"{e['state']}: consume this hour's CR refill "
                   f"(last CR activity {e['last_cr_activity'] or 'none'})",
            "verify_open": True, "comments": "@coderabbitai review",
        })

    # rebase (Step 4.7) — BEHIND/DIRTY in-lane, cap 3, update-branch first
    rebase = [e for e in entries if e["lane"] == "owner"
              and e["state"] != "FETCH_FAIL"
              and e["mss"] in ("BEHIND", "DIRTY", "CONFLICTING")]
    rebase = rebase[:REBASE_CAP]
    rebase_ids = {(e["repo"], e["number"]) for e in rebase}
    for e in rebase:
        actions.append({
            "type": "rebase", "repo": e["repo"], "pr": e["number"],
            "why": f"{e['mss']} — bring branch current with {e['base'] or 'base'} "
                   f"(mechanical, in-lane)",
            "verify_open": True, "mode": "update-branch",
        })

    # ci_triage (Step 4.6) — CR-CLEAN+mergeable red_ci, UNSTABLE/BLOCKED, cap 2
    triage = [e for e in entries if e["lane"] == "owner" and e["tier"] == "red_ci"
              and e["mss"] in ("UNSTABLE", "BLOCKED")
              and (e["repo"], e["number"]) not in rebase_ids]
    for e in triage[:CI_TRIAGE_CAP]:
        actions.append({
            "type": "ci_triage", "repo": e["repo"], "pr": e["number"],
            "why": f"CR-clean+mergeable but {e['mss']} on real check(s): "
                   f"{','.join(e['red_failing'])}",
            "verify_open": True,
        })

    # cli_launch (Step 4.5) — stacked PRs, only when the owner is quiet, cap 3
    if quiet.startswith("yes"):
        cli = [e for e in entries if e["state"] == "STACKED_BLOCKED"
               and e["cr_inline_count"] == 0
               and e["base"] not in ("main", "master", "develop")]
        for e in cli[:CLI_CAP]:
            actions.append({
                "type": "cli_launch", "repo": e["repo"], "pr": e["number"],
                "why": f"stacked PR (base {e['base']}) — cloud CR auto-disabled; "
                       f"run CR CLI locally (owner quiet)",
                "verify_open": True, "mode": "stacked",
                "branch": e["branch"], "base": e["base"],
            })

    return actions, len(bumps)


def build_greens(entries):
    greens = {"strict": [], "cosmetic_yellow": [], "red_ci": []}
    for e in entries:
        if e["tier"] in greens:
            # "number" AND "pr" carry the same value: prs[] uses "number",
            # actions[] uses "pr", and renderers have guessed both — emitting
            # both keys makes "#null" impossible whichever one they read.
            g = {
                "repo": e["repo"], "pr": e["number"], "number": e["number"],
                "branch": e["branch"],
                "base": e["base"], "lane": e["lane"], "mss": e["mss"],
                "failing_checks": e["failing_checks"], "blurb": e["blurb"],
                # Which channel earned the green ("cloud" or "cli") — a merge
                # must never be silently attributed to the wrong reviewer.
                "green_via": e.get("green_via", ""),
            }
            if e["tier"] == "red_ci":
                g["red_failing"] = e["red_failing"]
            greens[e["tier"]].append(g)
    return greens


# ============================================================================
# Step 1.5 — merged-PR -> ticket-id reconcile (3-day window)
# ============================================================================
def reconcile_tickets(gh_bin, owner, now, prefix=None):
    prefix = prefix or os.environ.get("LINEAR_BRANCH_PREFIX", "dev")
    d = iso(now - timedelta(days=3))[:10]
    merged = gh_json(gh_bin, [
        "search", "prs", "--owner", owner, "--author", "@me",
        "--merged", f"merged:>={d}", "--json", "number,repository,title",
        "--limit", "200",
    ])
    if not merged:
        return []
    tickets = set()
    for pr in merged:
        title = pr.get("title", "") or ""
        m = re.search(prefix + r"-(\d+)", title, re.I)
        if m:
            tickets.add(prefix.upper() + "-" + m.group(1))
            continue
        nwo = (pr.get("repository") or {}).get("nameWithOwner", "")
        ownr, _, rp = nwo.partition("/")
        if not rp:
            continue
        view = gh_json(gh_bin, ["-R", f"{ownr}/{rp}", "pr", "view",
                                str(pr.get("number")), "--json", "headRefName"])
        if view:
            m2 = re.search(prefix + r"-(\d+)", view.get("headRefName", "") or "", re.I)
            if m2:
                tickets.add(prefix.upper() + "-" + m2.group(1))
    return sorted(tickets, key=lambda t: int(t.split("-")[1]))


# ============================================================================
# state file
# ============================================================================
def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(path, obj):
    try:
        with open(path, "w") as f:
            json.dump(obj, f)
    except Exception as e:
        sys.stderr.write(f"babysit_classify: could not write state {path}: {e}\n")


def decide(entries, prev, bumped, fingerprint, now):
    pending = [e for e in entries if e["state"] in PENDING_STATES]
    pending_count = len(pending)
    rate_limited_pending = len([e for e in pending if e["state"] == "RATE_LIMITED"])

    prev_streak, prev_fp = 0, ""
    if prev and prev.get("last_iter_at"):
        la = parse_iso(prev["last_iter_at"])
        if la and (now - la) <= timedelta(hours=STALE_SESSION_H):
            prev_streak = int(prev.get("no_progress_streak", 0) or 0)
            prev_fp = prev.get("pending_fingerprint", "") or ""
        # else: stale session -> reset (prev_streak=0, prev_fp="")

    if pending_count == 0:
        decision, streak = "DRAINED", 0
    elif rate_limited_pending > 0 or bumped > 0:
        # credit-blocked queue (or a bump fired) can NEVER stall — the loop's
        # job is to consume the hourly refill. Force streak 0, stay armed.
        decision, streak = "PROGRESSING", 0
    elif fingerprint != prev_fp:
        decision, streak = "PROGRESSING", 0
    else:
        streak = prev_streak + 1
        decision = "STALLED" if streak >= STALL_LIMIT else "PROGRESSING"

    return decision, streak, pending_count


# ============================================================================
# discovery filter
# ============================================================================
def should_skip(pr):
    if pr.get("isDraft"):
        return True
    tl = (pr.get("title") or "").lower().strip()
    if tl.startswith("[wip") or tl.startswith("wip:") or "don't merge" in tl or "do not merge" in tl:
        return True
    labels = {(l.get("name") or "").lower() for l in (pr.get("labels") or [])}
    if labels & {"do-not-merge", "wip", "dnr", "do not merge"}:
        return True
    return False


# ============================================================================
# main sweep
# ============================================================================
def sweep(repos_filter, state_path, gh_bin, owner=OWNER_DEFAULT):
    now = now_dt()

    found = gh_json(gh_bin, [
        "search", "prs", "--owner", owner, "--author", "@me", "--state", "open",
        "--json", "repository,number,title,url,isDraft,labels,createdAt",
        "--limit", "100",
    ])
    if found is None:
        # search itself FETCH_FAILed — emit valid JSON, do NOT auto-stop.
        prev = load_state(state_path)
        streak = int((prev or {}).get("no_progress_streak", 0) or 0)
        return {
            "prs": [], "greens": {"strict": [], "cosmetic_yellow": [], "red_ci": []},
            "actions": [], "reconcile_tickets": [],
            "quiet": is_owner_quiet(now), "decision": "PROGRESSING",
            "pending": int((prev or {}).get("pending_count", 0) or 0),
            "fingerprint": (prev or {}).get("pending_fingerprint", "") or "",
            "streak": streak, "error": "search_fetch_fail",
        }

    if repos_filter:
        want = {r.strip().lower() for r in repos_filter.split(",") if r.strip()}
        found = [p for p in found if (p.get("repository") or {}).get("name", "").lower() in want]

    survivors = []
    for p in found:
        if should_skip(p):
            continue
        nwo = (p.get("repository") or {}).get("nameWithOwner", "")
        own, _, _rp = nwo.partition("/")
        p["_owner"] = own or owner
        p["_repo"] = (p.get("repository") or {}).get("name", "")
        survivors.append(p)

    try:
        conc = max(1, int(os.environ.get("BABYSIT_CONCURRENCY", "5")))
    except ValueError:
        conc = 5

    # Loaded ONCE per sweep, outside the per-PR workers: the CLI-review
    # records feed the head-identity half of cli_covers_head, and the waiver
    # store feeds the finding-level subtraction. Both loaders return {} on a
    # missing/corrupt store — a planning hint must never crash the sweep.
    review_recs = load_cli_reviews()
    waived = load_waived_findings()

    entries = []
    if survivors:
        with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as ex:
            futs = [ex.submit(classify_pr, gh_bin, p, now, review_recs, waived)
                    for p in survivors]
            for f in concurrent.futures.as_completed(futs):
                entries.append(f.result())
    entries.sort(key=lambda e: (e["repo"], e["number"]))

    quiet = is_owner_quiet(now)
    actions, bumped = build_actions(entries, now, quiet)
    greens = build_greens(entries)
    reconcile = reconcile_tickets(gh_bin, owner, now)

    lines = [f'{e["repo"]}#{e["number"]}:{e["state"]}:{e["last_cr_activity"]}' for e in entries]
    fingerprint = hashlib.sha1("\n".join(sorted(lines)).encode()).hexdigest()

    prev = load_state(state_path)
    decision, streak, pending_count = decide(entries, prev, bumped, fingerprint, now)

    write_state(state_path, {
        "pending_fingerprint": fingerprint,
        "no_progress_streak": streak,
        "pending_count": pending_count,
        "last_iter_at": iso(now),
    })

    return {
        "prs": entries,
        "greens": greens,
        "actions": actions,
        "reconcile_tickets": reconcile,
        # Findings a human RULED ON this sweep (a structured ruling comment
        # was harvested straight off the PR — see harvest_ruling_comments),
        # as distinct from what is still genuinely AWAITING a human. Present
        # regardless of whether the local waived_findings store also has the
        # entry, so human-queue depth reflects only real, unresolved debt.
        "ruled_via_pr_comment": sorted(
            [{"repo": e["repo"], "pr": e["number"], "lane": e["lane"],
              "blurb": e["blurb"], "findings": e["ruled_via_pr_comment"]}
             for e in entries if e.get("ruled_via_pr_comment")],
            key=lambda d: (d["repo"], d["pr"]),
        ),
        "quiet": quiet,
        "decision": decision,
        "pending": pending_count,
        "fingerprint": fingerprint,
        "streak": streak,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="babysit_classify.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sweep", help="classify all your open PRs + plan actions")
    sp.add_argument("--repos", default="", help="comma-separated repo NAMES to filter")
    sp.add_argument("--state", default="/tmp/babysit-prs-state.json", help="state file path")
    sp.add_argument("--gh-bin", default="gh", help="gh binary (default: gh on PATH)")
    sp.add_argument("--owner", default=OWNER_DEFAULT, help="org owner")
    # The ONE clean, scriptable way to render a ruling comment, so a sweep, a
    # review conversation, or any other session all produce byte-identical
    # output — commands/babysit-prs.md pipes this straight into
    # `gh pr comment --body-file -`. Single source of truth shared with the
    # reader (harvest_ruling_comments) so the format cannot drift out of sync
    # with its own parser.
    rp = sub.add_parser("ruling-comment",
        help="render the structured PR comment for a human ruling")
    rp.add_argument("finding_key", help='e.g. "path/to/file.py:RULE_ID"')
    rp.add_argument("reason", help="the ruling's reason, with evidence")
    rp.add_argument("by", nargs="?",
                    default=os.environ.get("BABYSIT_WAIVE_BY", "unknown"),
                    help="who ruled (default: $BABYSIT_WAIVE_BY or 'unknown')")
    # NOT an argv flag, deliberately. Authorization is read from the SAME env
    # var the authorized store write reads (BABYSIT_WAIVE_AUTHORIZED_BY, see
    # hooks/babysit-progress.sh `waive`), so the two halves of one ruling
    # action cannot disagree, and adding `--authorized` to a command line is
    # not a cheaper way to clear a critical than setting the variable the
    # hook already demands. `by` stays argv because it is a label, not a
    # claim.
    args = ap.parse_args(argv)

    if args.cmd == "sweep":
        out = sweep(args.repos, args.state, args.gh_bin, args.owner)
        sys.stdout.write(json.dumps(out))
        sys.stdout.write("\n")
        return 0
    if args.cmd == "ruling-comment":
        sys.stdout.write(render_ruling_comment(
            args.finding_key, args.reason, args.by,
            authorized_by_human=bool(
                os.environ.get("BABYSIT_WAIVE_AUTHORIZED_BY", "").strip())))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
