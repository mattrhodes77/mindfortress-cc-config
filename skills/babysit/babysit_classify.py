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
    fields = "state,headRefName,mergeable,mergeStateStatus,baseRefName,statusCheckRollup"
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
    }


def classify_pr(gh_bin, pr, now):
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

    # last push on the branch — only needed to age inline comments.
    last_push = None
    if cr_inline:
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

    # --- green tier (only CR-CLEAN PRs are green candidates) ---
    failing = failing_check_names(scr)
    red_failing = [n for n in failing if not check_is_cosmetic(n)]
    tier = ""
    if state == "CLEAN":
        if red_failing:
            tier = "red_ci"             # RED check -> NEVER a green of any tier
        elif mergeable == "MERGEABLE" and mss == "CLEAN" and not failing:
            tier = "strict"
        elif mergeable == "MERGEABLE" and mss == "UNSTABLE" and failing and not red_failing:
            tier = "cosmetic_yellow"

    return {
        "repo": repo, "number": pr["number"], "branch": branch,
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
# waiver-store readers — hooks/babysit-progress.sh is the writer. This is a
# FOUNDATION layer: a later chunk teaches classify_pr to populate each entry's
# `cli_findings_open` from a CR-CLI harvest and wires plan_applies() below
# into build_actions()/sweep(); until then these are tested standalone.
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
    """{"repo#pr": {"<finding-key>": reason}} for every individually-waived
    CR-CLI finding.

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
            if isinstance(rec, dict):
                keyed[fk] = rec.get("reason") or ""
            elif isinstance(rec, str):
                keyed[fk] = rec
        if keyed:
            out[k] = keyed
    return out


# ============================================================================
# apply-queue ranking (same foundation-chunk caveat as the loaders above:
# unwired until a later chunk gives entries a `cli_findings_open` field)
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
    free: they reduce cli_findings_open itself (once a later chunk wires
    that into classify_pr), so a fully-waived PR's `findings` count is
    already 0/absent by the time it reaches here. known_fp is different --
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

    # HAS_ACTIONABLE -> fix (skill applies mechanical CR fixes verbatim)
    for e in entries:
        if e["state"] == "HAS_ACTIONABLE":
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

    entries = []
    if survivors:
        with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as ex:
            futs = [ex.submit(classify_pr, gh_bin, p, now) for p in survivors]
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
    args = ap.parse_args(argv)

    if args.cmd == "sweep":
        out = sweep(args.repos, args.state, args.gh_bin, args.owner)
        sys.stdout.write(json.dumps(out))
        sys.stdout.write("\n")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
