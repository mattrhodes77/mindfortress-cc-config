"""Tests for the ruling→green chain in skills/babysit/babysit_classify.py.

Three mechanisms, tested at the function level (no gh subprocess — the gh
seams `fetch_view`/`gh_json` are monkeypatched on the module):

1. HUMAN-RULING HARVEST: a structured ruling comment posted on the PR
   (render_ruling_comment / harvest_ruling_comments) reaches the effective
   waived set inside classify_pr, so a ruling survives a lost/reset local
   store. Only the module's own marker parses; authorization comes from the
   `authorized-by-human: true` line (v2), never from `by`.

2. A WAIVER NEVER ZEROES A CRITICAL UNLESS A HUMAN AUTHORIZED IT: an
   unauthorized waiver (store record or PR comment) clears a MINOR/MAJOR but
   floors a CRITICAL-bearing count at one, keeping the PR out of every green
   tier.

3. AN ADJUDICATED FINDING IS CLEAN AT HEAD: when the waiver machinery takes
   `cli_open` to None, `cli_clean_at_head` accepts that reading alongside the
   raw zero-parse — so a ruled PR can actually reach a green tier on the CLI
   channel (the only channel a RATE_LIMITED PR has), instead of being waived
   out of the human queue AND out of the merge path at once.

Plus the planner half: build_actions' HAS_ACTIONABLE→fix loop consults
known_fp the same way plan_applies does, so an adjudicated-away PR stops
being re-planned as a fix target every sweep.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "babysit")
SCRIPT = os.path.join(SKILL_DIR, "babysit_classify.py")

sys.path.insert(0, SKILL_DIR)
import babysit_classify as bc  # noqa: E402

# The author gate: only comments from these logins may carry rulings. Pinned
# for the whole module so no test depends on the machine's env or the
# team-config default.
os.environ["BABYSIT_RULING_AUTHORS"] = "agent-session"

NOW = "2026-01-02T12:00:00Z"
OLD_PUSH = "2026-01-01T00:00:00Z"      # commit predates every comment below
HEAD = "abc1234def5678900000000000000000000000ff"


def ruling_comment_v2(finding_key, *, authorized, at, reason="adjudicated with evidence"):
    body = bc.render_ruling_comment(finding_key, reason, "alice",
                                    authorized_by_human=authorized)
    return {"user": {"login": "agent-session"}, "created_at": at, "body": body}


def harvest_comment(*, at, unapplied, critical=0, major=0, minor=0, trivial=0,
                    reviewed_head=HEAD[:7]):
    total = critical + major + minor + trivial
    body = (
        "**CodeRabbit CLI review (local)**\n"
        f"severity: critical={critical} major={major} minor={minor} trivial={trivial}\n"
        f"Reviewed head `{reviewed_head}` — {total} findings, unapplied={unapplied}\n"
    )
    return {"user": {"login": "agent-session"}, "created_at": at, "body": body}


def clean_harvest_comment(*, at, reviewed_head=HEAD[:7]):
    body = (
        "**CodeRabbit CLI review (local)**\n"
        "severity: critical=0 major=0 minor=0 trivial=0\n"
        f"Reviewed head `{reviewed_head}` (= current head) — **no findings.** Clean.\n"
    )
    return {"user": {"login": "agent-session"}, "created_at": at, "body": body}


def rate_limited_comment(at):
    return {"user": {"login": "coderabbitai[bot]"}, "created_at": at,
            "body": "> Rate limit exceeded. Please wait before requesting a review."}


def classify(issues, *, waived_findings=None, review_recs=None,
             head_oid=HEAD, mss="CLEAN", scr=None, reviews=None):
    """Drive the real classify_pr with the gh seams stubbed out."""
    view = {
        "state": "OPEN", "headRefName": "feature/x", "headRefOid": head_oid,
        "mergeable": "MERGEABLE", "mergeStateStatus": mss,
        "baseRefName": "main", "statusCheckRollup": scr or [],
    }

    def fake_gh_json(gh_bin, args, attempts=None):
        joined = " ".join(args)
        if "/pulls/" in joined and joined.endswith("reviews?per_page=100"):
            return reviews or []
        if "/pulls/" in joined and "/comments" in joined:
            return []            # no inline comments
        if "/issues/" in joined and "/comments" in joined:
            return issues
        if "/commits/" in joined:
            return {"commit": {"committer": {"date": OLD_PUSH}}}
        raise AssertionError(f"unexpected gh call: {joined}")

    pr = {"_owner": "your-org", "_repo": "acme-api", "number": 700,
          "labels": [], "title": "pr 700", "createdAt": OLD_PUSH}
    with mock.patch.object(bc, "fetch_view", return_value=view), \
         mock.patch.object(bc, "gh_json", side_effect=fake_gh_json):
        return bc.classify_pr("gh", pr, bc.parse_iso(NOW),
                              review_recs=review_recs,
                              waived_findings=waived_findings)


# ============================================================================
# render / harvest round trip
# ============================================================================
class RulingCommentRenderTests(unittest.TestCase):
    def test_round_trip_preserves_key_reason_by_and_authorization(self):
        body = bc.render_ruling_comment("a.py:R1", "why", "alice",
                                        authorized_by_human=True)
        out = bc.harvest_ruling_comments(
            [{"user": {"login": "agent-session"}, "body": body,
              "created_at": "2026-01-01T00:00:00Z"}])
        self.assertEqual(out, {"a.py:R1": {
            "reason": "why", "by": "alice",
            "authorized_by_human": True, "via": "pr_comment"}})

    def test_an_arbitrary_comment_is_not_a_ruling(self):
        """Only the module's own marker parses — a human-typed comment that
        merely LOOKS like a ruling must never silence a finding."""
        out = bc.harvest_ruling_comments([{
            "user": {"login": "agent-session"},
            "body": "**Babysit ruling: WAIVE**\n- finding: `a.py:R1`\n- reason: nah\n",
            "created_at": "2026-01-01T00:00:00Z"}])
        self.assertEqual(out, {})

    def test_a_marker_comment_without_a_finding_line_is_skipped_not_fatal(self):
        out = bc.harvest_ruling_comments([{
            "user": {"login": "agent-session"},
            "body": f"{bc.RULING_MARKER}\nno finding line here\n",
            "created_at": "2026-01-01T00:00:00Z"}])
        self.assertEqual(out, {})

    def test_newest_ruling_wins_by_created_at_not_list_order(self):
        older = ruling_comment_v2("a.py:R1", authorized=False,
                                  at="2026-01-01T00:00:00Z", reason="first take")
        newer = ruling_comment_v2("a.py:R1", authorized=True,
                                  at="2026-01-02T00:00:00Z", reason="revised")
        # newest FIRST in the list — the function must still pick by timestamp
        for order in ([newer, older], [older, newer]):
            out = bc.harvest_ruling_comments(order)
            self.assertEqual(out["a.py:R1"]["reason"], "revised")
            self.assertIs(out["a.py:R1"]["authorized_by_human"], True)

    def test_v1_comments_still_parse_but_read_unauthorized(self):
        body = (f"{bc.RULING_MARKER_V1}\n**Babysit ruling: WAIVE**\n\n"
                "- finding: `a.py:R1`\n- reason: legacy\n- by: alice\n")
        out = bc.harvest_ruling_comments(
            [{"user": {"login": "agent-session"}, "body": body,
              "created_at": "2026-01-01T00:00:00Z"}])
        self.assertIs(out["a.py:R1"]["authorized_by_human"], False)

    def test_only_the_exact_lowercase_true_authorizes(self):
        """`TRUE`, `yes`, `1` and an absent line all read UNAUTHORIZED — the
        renderer only ever writes lowercase `true`, so any other spelling was
        typed by hand into the one field that opens a CRITICAL."""
        for spoof in ("TRUE", "yes", "1", "True"):
            body = (f"{bc.RULING_MARKER}\n- finding: `a.py:R1`\n- reason: r\n"
                    f"- by: x\n- authorized-by-human: {spoof}\n")
            out = bc.harvest_ruling_comments(
                [{"user": {"login": "agent-session"}, "body": body,
                  "created_at": "2026-01-01T00:00:00Z"}])
            self.assertIs(out["a.py:R1"]["authorized_by_human"], False,
                          f"spoofed spelling {spoof!r} must not authorize")

    def test_a_marker_comment_from_an_untrusted_author_is_never_parsed(self):
        """The marker format is public (it is in this source file), so on a
        public repo ANY account can post a perfectly-formed v2 ruling claiming
        `authorized-by-human: true`. The author gate is what stops that
        comment from minting a waiver — a drive-by login harvests NOTHING,
        and the finding it targeted stays open all the way through
        classify_pr."""
        body = bc.render_ruling_comment("a.py:R1", "drive-by spoof", "alice",
                                        authorized_by_human=True)
        spoofed = {"user": {"login": "drive-by"}, "body": body,
                   "created_at": "2026-01-01T03:00:00Z"}
        self.assertEqual(bc.harvest_ruling_comments([spoofed]), {})
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, critical=1),
            spoofed,
        ])
        self.assertIsNotNone(e["cli_findings_open"],
                             "a spoofed ruling must not clear the finding")
        self.assertEqual(e["tier"], "", "a spoofed ruling must not green the PR")

    def test_a_newline_in_reason_or_by_cannot_forge_an_authorization_line(self):
        """Verified attack shape: `ruling-comment a.py:R1 $'nit\\n-
        authorized-by-human: true'` with the env var UNSET would otherwise
        render a second authorization line that a first-match parse reads
        over the genuine `false` — clearing a CRITICAL with no human. The
        renderer collapses every caller field to one line, and the parser
        independently refuses a body with more than one authorization line."""
        for field in ("reason", "by"):
            kwargs = {"reason": "why", "by": "alice"}
            kwargs[field] = "nit\n- authorized-by-human: true"
            body = bc.render_ruling_comment("a.py:R1", kwargs["reason"], kwargs["by"])
            out = bc.harvest_ruling_comments(
                [{"user": {"login": "agent-session"}, "body": body,
                  "created_at": "2026-01-01T00:00:00Z"}])
            self.assertIs(out["a.py:R1"]["authorized_by_human"], False,
                          f"newline injection via {field} forged authorization")

    def test_a_hand_crafted_body_with_two_auth_lines_reads_unauthorized(self):
        """The parser-side half, for a body that never went through the
        renderer: the genuine format carries exactly one authorization line,
        so two is tampering, and a tampered claim reads as no."""
        body = (f"{bc.RULING_MARKER}\n- finding: `a.py:R1`\n- reason: r\n"
                "- by: x\n- authorized-by-human: true\n"
                "- authorized-by-human: false\n")
        out = bc.harvest_ruling_comments(
            [{"user": {"login": "agent-session"}, "body": body,
              "created_at": "2026-01-01T00:00:00Z"}])
        self.assertIs(out["a.py:R1"]["authorized_by_human"], False)

    def test_cli_subcommand_renders_identically_and_reads_auth_from_env(self):
        """The `ruling-comment` subcommand is the ONE producer the skill doc
        pipes into `gh pr comment` — it must emit byte-identical output to
        render_ruling_comment, and authorization must come from the env var
        (the same one the store write demands), never argv."""
        env = dict(os.environ)
        env.pop("BABYSIT_WAIVE_AUTHORIZED_BY", None)
        p = subprocess.run(
            [sys.executable, SCRIPT, "ruling-comment", "a.py:R1", "why", "alice"],
            capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, bc.render_ruling_comment("a.py:R1", "why", "alice"))

        env["BABYSIT_WAIVE_AUTHORIZED_BY"] = "alice"
        p2 = subprocess.run(
            [sys.executable, SCRIPT, "ruling-comment", "a.py:R1", "why", "alice"],
            capture_output=True, text=True, env=env)
        self.assertEqual(p2.stdout, bc.render_ruling_comment(
            "a.py:R1", "why", "alice", authorized_by_human=True))


# ============================================================================
# classify_pr: rulings reach the waived set; criticals stay floored
# ============================================================================
class RulingCommentHarvestTests(unittest.TestCase):
    def test_a_ruling_comment_clears_a_noncritical_finding_without_the_store(self):
        """The durability path: no local store at all, the ruling lives only
        as a PR comment, and the finding still clears."""
        issues = [
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, major=1),
            ruling_comment_v2("a.py:R1", authorized=False,
                              at="2026-01-01T03:00:00Z"),
        ]
        e = classify(issues)
        self.assertIsNone(e["cli_findings_open"])
        self.assertEqual(e["ruled_via_pr_comment"], ["a.py:R1"])

    def test_g_the_same_unauthorized_ruling_clears_a_minor_but_not_a_critical(self):
        """Two identical PRs but for severity: the unauthorized ruling zeroes
        the MINOR's count, while the CRITICAL floors at one and keeps its
        critical flag — over-blocking a day is the safe failure mode."""
        ruling = ruling_comment_v2("a.py:R1", authorized=False,
                                   at="2026-01-01T03:00:00Z")
        minor = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, minor=1),
            ruling,
        ])
        critical = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, critical=1),
            ruling,
        ])
        self.assertIsNone(minor["cli_findings_open"])
        self.assertIsNotNone(critical["cli_findings_open"])
        self.assertEqual(critical["cli_findings_open"]["findings"], 1)
        self.assertTrue(critical["cli_findings_open"]["critical"])

    def test_an_authorized_ruling_clears_a_critical(self):
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, critical=1),
            ruling_comment_v2("a.py:R1", authorized=True,
                              at="2026-01-01T03:00:00Z"),
        ])
        self.assertIsNone(e["cli_findings_open"])

    def test_an_unauthorized_store_waiver_also_floors_a_critical(self):
        """Same floor, other source: the store record's authorized_by_human
        flag is what decides, exactly as for a PR-comment ruling."""
        waived = {"acme-api#700": {"a.py:R1": {
            "reason": "agent waived", "authorized_by_human": False}}}
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, critical=1),
        ], waived_findings=waived)
        self.assertIsNotNone(e["cli_findings_open"])
        self.assertEqual(e["cli_findings_open"]["findings"], 1)


# ============================================================================
# classify_pr: the CLI green channel + the adjudicated-clean gate
# ============================================================================
class CliGreenChannelTests(unittest.TestCase):
    def test_a_clean_cli_review_at_head_is_green_via_cli(self):
        """Base case: RATE_LIMITED PR (cloud refuses), clean local harvest at
        the live head — the CLI channel greens it."""
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            clean_harvest_comment(at="2026-01-01T02:00:00Z"),
        ])
        self.assertEqual(e["state"], "RATE_LIMITED")
        self.assertEqual(e["tier"], "strict")
        self.assertEqual(e["green_via"], "cli")

    def test_a_fix_applying_harvest_is_not_clean_at_its_own_new_head(self):
        """'At head' is a SHA identity, not a clock race. The harvest flow is
        review → apply → push → comment, so the comment postdates the very
        commit it did not review — a reviewed-head that is not the live head
        must veto the green however fresh the comment is."""
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            clean_harvest_comment(at="2026-01-01T02:00:00Z",
                                  reviewed_head="99999ff"),
        ])
        self.assertEqual(e["tier"], "")
        self.assertEqual(e["green_via"], "")

    def test_a_recorded_review_head_that_disagrees_vetoes_too(self):
        """Two sources name the reviewed SHA (the comment and the store
        record); ANY known source that disagrees vetoes."""
        recs = {"acme-api#700": {"head": "1234567890", "at": NOW}}
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            clean_harvest_comment(at="2026-01-01T02:00:00Z"),
        ], review_recs=recs)
        self.assertEqual(e["tier"], "")

    def test_h_a_ruled_finding_greens_the_pr_when_the_cli_is_the_only_channel(self):
        """The adjudicated-clean gate. The raw harvest body still says
        `unapplied=1`, but every finding on it has been ruled WAIVE — the PR
        must reach a green tier instead of being invisible in both
        directions (out of the human queue AND out of the merge path)."""
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, major=1),
            ruling_comment_v2("a.py:R1", authorized=False,
                              at="2026-01-01T03:00:00Z"),
        ])
        self.assertEqual(e["state"], "RATE_LIMITED")
        self.assertIsNone(e["cli_findings_open"])
        self.assertEqual(e["tier"], "strict",
                         "an adjudicated finding must be clean at head")
        self.assertEqual(e["green_via"], "cli")

    def test_i_no_ruling_control_the_same_pr_stays_ungreen(self):
        """The control for test_h: identical PR, no ruling — the unapplied
        finding keeps it out of every green tier."""
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, major=1),
        ])
        self.assertIsNotNone(e["cli_findings_open"])
        self.assertEqual(e["tier"], "")
        self.assertEqual(e["green_via"], "")

    def test_j_an_unauthorized_ruling_on_a_critical_greens_nothing(self):
        """The floor and the gate compose: the CRITICAL floors at one, so
        cli_open stays set, cli_clean_at_head stays False, and the critical
        veto keeps the tier empty even for the cloud channel."""
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, critical=1),
            ruling_comment_v2("a.py:R1", authorized=False,
                              at="2026-01-01T03:00:00Z"),
        ])
        self.assertEqual(e["tier"], "")
        self.assertEqual(e["green_via"], "")

    def test_a_spoofed_clean_harvest_from_an_untrusted_author_greens_nothing(self):
        """The harvest channel is author-gated for the same reason the ruling
        channel is: the marker is public, so a drive-by commenter could
        otherwise post a fake "no findings" harvest at the live head and
        auto-merge an unreviewed PR."""
        fake = dict(clean_harvest_comment(at="2026-01-01T02:00:00Z"))
        fake["user"] = {"login": "drive-by"}
        e = classify([rate_limited_comment("2026-01-01T01:00:00Z"), fake])
        self.assertEqual(e["tier"], "", "a spoofed harvest must not green")
        self.assertEqual(e["green_via"], "")

    def test_a_spoofed_newer_harvest_cannot_bury_a_real_critical_one(self):
        """The other direction: a NEWER fake harvest from an untrusted login
        must not become newest_harvest and mask the real critical=1 one."""
        fake = dict(clean_harvest_comment(at="2026-01-01T03:00:00Z"))
        fake["user"] = {"login": "drive-by"}
        e = classify([
            rate_limited_comment("2026-01-01T01:00:00Z"),
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, critical=1),
            fake,
        ])
        self.assertIsNotNone(e["cli_findings_open"],
                             "the real critical harvest must stay visible")
        self.assertTrue(e["cli_findings_open"]["critical"])
        self.assertEqual(e["tier"], "")

    def test_rate_limit_text_in_a_review_body_is_rate_limited_not_clean(self):
        """Rate-limit/credit text can land as a submitted REVIEW body, not
        only an issue comment. Scanning the latest issue comment alone let
        such a PR fall through to `cr_reviews non-empty -> CLEAN` and
        auto-merge unreviewed — the adjacent no-actionable scan already
        reads both feeds for exactly this reason."""
        review = {"user": {"login": "coderabbitai[bot]"},
                  "submitted_at": "2026-01-01T01:00:00Z",
                  "body": "Rate limit exceeded. Please wait before requesting "
                          "another review."}
        e = classify([], reviews=[review])
        self.assertEqual(e["state"], "RATE_LIMITED",
                         "a rate-limited review body must never read as CLEAN")
        self.assertEqual(e["tier"], "")

    def test_an_unapplied_critical_blocks_even_a_cloud_clean_pr(self):
        """cli_findings_open and the green tier are no longer independent: a
        cloud '0 actionable' verdict must not merge a PR whose local harvest
        reports an unapplied CRITICAL."""
        e = classify([
            {"user": {"login": "coderabbitai[bot]"},
             "created_at": "2026-01-01T01:00:00Z",
             "body": "**Actionable comments posted: 0**\n\n"
                     "No actionable comments were generated."},
            harvest_comment(at="2026-01-01T02:00:00Z", unapplied=1, critical=1),
        ])
        self.assertEqual(e["state"], "CLEAN")
        self.assertEqual(e["tier"], "",
                         "an unapplied CRITICAL is never green, any channel")


# ============================================================================
# build_actions: the fix loop consults known_fp
# ============================================================================
class KnownFpFixPlanningTests(unittest.TestCase):
    def setUp(self):
        fd, self.store = tempfile.mkstemp(prefix="babysit-progress-fixplan-",
                                          suffix=".json")
        os.close(fd)
        self._env = mock.patch.dict(os.environ, {"BABYSIT_PROGRESS": self.store})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        os.remove(self.store)

    def _write_known_fp(self, keys):
        with open(self.store, "w") as fh:
            json.dump({"cli_reviewed": {}, "waived_findings": {}, "merges": [],
                       "known_fp": {k: {"reason": "adjudicated", "since": "t"}
                                    for k in keys}}, fh)

    @staticmethod
    def _entry(num):
        return {"repo": "acme-api", "number": num, "state": "HAS_ACTIONABLE",
                "lane": "owner", "mss": "CLEAN", "tier": "", "red_failing": [],
                "base": "main", "branch": f"feature/{num}",
                "cr_inline_count": 1, "last_cr_activity": NOW,
                "created_at": OLD_PUSH}

    def test_a_known_fp_pr_is_not_replanned_as_a_fix_target(self):
        """plan_applies already excludes a whole-PR known_fp waiver; the
        HAS_ACTIONABLE→fix loop needs the same treatment or an
        adjudicated-away PR is re-planned every sweep as pure noise. The raw
        `state` stays HAS_ACTIONABLE — known_fp silences the PLANNED ACTION
        only, never the harvest signal."""
        self._write_known_fp(["acme-api#801"])
        entries = [self._entry(801), self._entry(802)]
        actions, _ = bc.build_actions(entries, bc.parse_iso(NOW), "no:test")
        fix_prs = [a["pr"] for a in actions if a["type"] == "fix"]
        self.assertNotIn(801, fix_prs, "a known_fp'd PR must not be re-planned")
        self.assertIn(802, fix_prs, "an unwaived sibling still plans a fix")

    def test_without_known_fp_both_prs_plan_fixes(self):
        """Control: the exclusion must not eat ordinary fix planning."""
        self._write_known_fp([])
        entries = [self._entry(801), self._entry(802)]
        actions, _ = bc.build_actions(entries, bc.parse_iso(NOW), "no:test")
        fix_prs = sorted(a["pr"] for a in actions if a["type"] == "fix")
        self.assertEqual(fix_prs, [801, 802])


if __name__ == "__main__":
    unittest.main()
