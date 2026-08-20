"""Tests for the babysit waiver-store foundation.

Covers the finding-level waiver store hooks/babysit-progress.sh maintains
(the `waive`/`unwaive` subcommands, `set-cli-review`'s severity-count +
round-idempotency + waived-budget short-circuit, and rounds surviving a bare
`set-cli-head`) and its readers in skills/babysit/babysit_classify.py
(`load_known_fp`, `load_waived_findings`, `load_cli_reviews`,
`load_cli_reviewed_heads`) plus `plan_applies`'s known_fp exclusion.

known_fp waives an entire PR ("this whole PR is a false positive"). That is
too blunt for "one specific finding was adjudicated away on an otherwise-live
PR" -- silencing the whole PR to kill one re-raised finding also blinds it to
every future REAL finding. waived_findings is the finding-level escape hatch:
keyed repo#pr -> a caller-composed finding-key (e.g. "path/to/file.py:RULE_ID"
-- deliberately never a line number or head SHA, both of which move on every
push, so a waiver survives a force-push/rebase without being re-applied).

The bash tests run the REAL hook against a throwaway store via its own
$BABYSIT_PROGRESS override (never the live store). The python tests exercise
the readers and plan_applies directly against synthetic entries -- neither
touches a real classify_pr/gh pipeline.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
HOOK = os.path.join(REPO_ROOT, "hooks", "babysit-progress.sh")
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "babysit")

sys.path.insert(0, SKILL_DIR)
from babysit_classify import (  # noqa: E402
    load_known_fp,
    load_waived_findings,
    load_cli_reviews,
    load_cli_reviewed_heads,
    plan_applies,
)


# ============================================================================
# hooks/babysit-progress.sh — waive / unwaive / set-cli-review
# ============================================================================
class BabysitProgressWaiveTest(unittest.TestCase):
    def setUp(self):
        fd, self.store = tempfile.mkstemp(prefix="babysit-progress-test-", suffix=".json")
        os.close(fd)
        os.remove(self.store)  # let the hook's `ensure` create it fresh
        self.env = dict(os.environ)
        self.env["BABYSIT_PROGRESS"] = self.store
        # These cases exercise the waive MECHANISM (finding-scoping, unwaive,
        # survival across a push), not the autonomy bar -- and the bar fails
        # closed on a PR with no recorded severity, which would stand in for
        # the thing under test. Take the authorized path explicitly;
        # BabysitProgressWaivePolicyTest owns the bar itself.
        self.env["BABYSIT_WAIVE_AUTHORIZED_BY"] = "reviewer"

    def tearDown(self):
        if os.path.exists(self.store):
            os.remove(self.store)

    def _run(self, *args):
        return subprocess.run(
            ["bash", HOOK, *args],
            capture_output=True, text=True, env=self.env,
        )

    def _store(self):
        with open(self.store) as fh:
            return json.load(fh)

    def _rounds(self, key):
        return self._store()["cli_reviewed"][key]["rounds"]

    def test_waive_records_reason_and_who(self):
        key = "acme-frontend#878"
        fk = "app/config.py:HARDCODED_SECRET"
        p = self._run("waive", key, fk, "ineffective, reviewed and declined", "reviewer")
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = self._store()["waived_findings"][key][fk]
        self.assertEqual(rec["reason"], "ineffective, reviewed and declined")
        self.assertEqual(rec["by"], "reviewer")
        self.assertIn("since", rec)

    def test_waive_is_finding_scoped_not_pr_scoped(self):
        """Waiving ONE finding on a PR must leave a SECOND, different finding
        on the SAME PR untouched -- the whole point of finding-level waivers
        over known_fp is that suppressing one adjudicated finding must not
        blind the PR to every future real finding."""
        key = "acme-api#3183"
        self._run("waive", key, "provision.sh:RULE_A", "declined, needs live box", "reviewer")
        store = self._store()
        self.assertIn("provision.sh:RULE_A", store["waived_findings"][key])
        self.assertNotIn("provision.sh:RULE_B", store["waived_findings"][key])

    def test_unwaive_removes_only_that_finding_key(self):
        key = "acme-api#3183"
        self._run("waive", key, "provision.sh:RULE_A", "reason A", "reviewer")
        self._run("waive", key, "provision.sh:RULE_B", "reason B", "reviewer")
        p = self._run("unwaive", key, "provision.sh:RULE_A")
        self.assertEqual(p.returncode, 0, p.stderr)
        store = self._store()
        self.assertNotIn("provision.sh:RULE_A", store["waived_findings"][key])
        self.assertIn("provision.sh:RULE_B", store["waived_findings"][key],
                      "unwaiving one finding must not touch its sibling")

    def test_waiver_survives_a_simulated_push(self):
        """The key is repo#pr + finding-key, NEVER a head SHA -- so recording
        a new reviewed head (a push/rebase) via set-cli-head/set-cli-review
        must leave a previously-recorded waiver completely untouched."""
        key = "acme-frontend#878"
        fk = "app/config.py:HARDCODED_SECRET"
        self._run("waive", key, fk, "ineffective, reviewed and declined", "reviewer")
        before = self._store()["waived_findings"][key][fk]

        # simulate a force-push/rebase: the head SHA changes completely.
        p = self._run("set-cli-head", key, "brandnewsha-after-push")
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self._run("set-cli-review", key, "brandnewsha-after-push", "0", "1", "0", "0")
        self.assertEqual(p.returncode, 0, p.stderr)

        after = self._store()["waived_findings"][key][fk]
        self.assertEqual(after, before,
                         "a waiver keyed on repo#pr+finding-key must survive a "
                         "head change untouched -- it is not head-scoped")

    def test_waive_and_known_fp_coexist(self):
        """PR-level known_fp and finding-level waived_findings are two
        independent stores on the same PR -- neither write must clobber the
        other (backward compat for existing known_fp usage)."""
        key = "acme-api#3183"
        self._run("add-fp", "acme-api#9999", "unrelated PR-level FP")
        self._run("waive", key, "provision.sh:RULE_A", "declined", "reviewer")
        store = self._store()
        self.assertIn("acme-api#9999", store["known_fp"])
        self.assertIn(key, store["waived_findings"])
        self.assertIn("provision.sh:RULE_A", store["waived_findings"][key])

    def test_set_cli_head_preserves_rounds(self):
        """A bare set-cli-head (no severity data) used to REPLACE the whole
        cli_reviewed record, resetting the round counter to 0 and defeating
        the round cap. It must now preserve `rounds`."""
        key = "acme-api#100"
        self._run("set-cli-review", key, "sha-1", "0", "1", "0", "0")
        self.assertEqual(self._rounds(key), 1)
        p = self._run("set-cli-head", key, "sha-2")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self._rounds(key), 1,
                         "a bare set-cli-head must not reset the round counter")

    # -- round counter exclusion (composes with set-cli-review idempotency) --
    def test_round_counter_does_not_advance_when_everything_reported_is_waived(self):
        """A re-review whose ENTIRE severity histogram is covered by active
        waivers for that PR is CodeRabbit re-raising adjudicated findings,
        not new work -- it must not burn a round toward the round cap."""
        key = "acme-api#3183"
        self._run("waive", key, "provision.sh:RULE_A", "declined, needs live box", "reviewer")
        # one waiver on record; this review reports exactly one major finding
        # (the same re-raised one) -- fully covered by the waiver budget.
        p = self._run("set-cli-review", key, "sha-1", "0", "1", "0", "0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self._rounds(key), 0,
                         "a review that only re-raises a waived finding must "
                         "not spend a round")

    def test_round_counter_still_advances_when_a_new_finding_exceeds_the_waived_budget(self):
        """The exclusion must not become a blanket free pass for the whole
        PR -- a genuinely NEW, unwaived finding on the same PR must still
        count normally."""
        key = "acme-api#3183"
        self._run("waive", key, "provision.sh:RULE_A", "declined", "reviewer")
        # 2 findings reported but only 1 waiver on record -> real new work.
        p = self._run("set-cli-review", key, "sha-1", "0", "1", "1", "0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self._rounds(key), 1,
                         "a finding beyond the waived budget must still spend a round")

    def test_round_counter_unaffected_when_pr_has_no_waivers(self):
        """Backward compat: a PR with no waived_findings entry at all must
        behave exactly as before -- every review counts normally."""
        key = "acme-api#4242"
        p = self._run("set-cli-review", key, "sha-1", "0", "0", "1", "0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self._rounds(key), 1)

    def test_double_call_same_harvest_increments_rounds_once(self):
        """One harvest, two set-cli-review calls -> rounds advances by
        exactly 1 (idempotency, independent of the waiver budget check)."""
        key = "acme-api#3210"
        sha = "abc123"
        p1 = self._run("set-cli-review", key, sha, "0", "0", "1", "2")
        self.assertEqual(p1.returncode, 0, p1.stderr)
        self.assertEqual(self._rounds(key), 1)
        p2 = self._run("set-cli-review", key, sha, "0", "0", "1", "2")
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertEqual(self._rounds(key), 1, "duplicate call must not spend a round")

    def test_waive_and_round_counter_work_against_a_pre_existing_legacy_store(self):
        """A store predating `waived_findings` entirely (keys are just
        cli_reviewed/known_fp/merges) must not crash `waive` or
        `set-cli-review` -- both must auto-vivify the missing key rather than
        assuming `ensure()` put it there (ensure() only seeds a store when
        the FILE is absent; an already-existing legacy file is left as-is)."""
        legacy = {
            "cli_reviewed": {"acme-api#1": {"head": "a", "rounds": 2}},
            "known_fp": {}, "merges": [],
        }
        with open(self.store, "w") as fh:
            json.dump(legacy, fh)

        p = self._run("waive", "acme-api#3183", "provision.sh:RULE_A",
                       "declined, needs live box", "reviewer")
        self.assertEqual(p.returncode, 0, p.stderr)
        store = self._store()
        self.assertIn("provision.sh:RULE_A", store["waived_findings"]["acme-api#3183"])
        self.assertEqual(store["cli_reviewed"]["acme-api#1"]["rounds"], 2,
                         "pre-existing unrelated data must survive untouched")

        p = self._run("set-cli-review", "acme-api#9999", "shaX", "0", "1", "0", "0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self._rounds("acme-api#9999"), 1,
                         "a PR with no waiver entry on a legacy store still counts normally")


# ============================================================================
# skills/babysit/babysit_classify.py — waiver-store readers + plan_applies
# ============================================================================
class WaiverReadersTest(unittest.TestCase):
    def setUp(self):
        fd, self.store = tempfile.mkstemp(prefix="babysit-progress-readers-", suffix=".json")
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.store):
            os.remove(self.store)

    def _write(self, obj):
        with open(self.store, "w") as fh:
            json.dump(obj, fh)

    def test_missing_store_returns_empty_for_every_reader(self):
        missing = self.store + ".does-not-exist"
        self.assertEqual(load_known_fp(missing), {})
        self.assertEqual(load_waived_findings(missing), {})
        self.assertEqual(load_cli_reviews(missing), {})
        self.assertEqual(load_cli_reviewed_heads(missing), {})

    def test_load_known_fp_reads_reason(self):
        self._write({"known_fp": {"acme-api#1": {"reason": "ack-reply FP", "since": "t"}},
                     "waived_findings": {}, "cli_reviewed": {}, "merges": []})
        self.assertEqual(load_known_fp(self.store), {"acme-api#1": "ack-reply FP"})

    def test_load_waived_findings_reads_nested_reason(self):
        """The reader keeps each record a DICT so `authorized_by_human` can
        reach the classifier's critical floor -- flattening to the reason
        string is how the flag once never reached the read side at all. An
        absent/non-True flag and a legacy bare-string record both normalise
        to False: an unprovable authorisation is not an authorisation."""
        self._write({"known_fp": {},
                     "waived_findings": {
                         "acme-api#7001": {
                             "file.py:RULE_A": {"reason": "adjudicated FP",
                                                "since": "t", "by": "reviewer"},
                             "file.py:RULE_B": {"reason": "human ruled",
                                                "since": "t", "by": "alice",
                                                "authorized_by_human": True},
                             "file.py:RULE_C": "legacy bare-string reason"}},
                     "cli_reviewed": {}, "merges": []})
        out = load_waived_findings(self.store)
        self.assertEqual(out, {"acme-api#7001": {
            "file.py:RULE_A": {"reason": "adjudicated FP",
                               "authorized_by_human": False},
            "file.py:RULE_B": {"reason": "human ruled",
                               "authorized_by_human": True},
            "file.py:RULE_C": {"reason": "legacy bare-string reason",
                               "authorized_by_human": False}}})

    def test_load_waived_findings_is_backward_compatible_with_a_legacy_store(self):
        """A store written before waived_findings existed (no such key at
        all) must not crash the reader -- same absent-key contract as
        load_known_fp/load_cli_reviewed_heads."""
        self._write({"cli_reviewed": {}, "known_fp": {
            "acme-api#1": {"reason": "x", "since": "t"}}, "merges": []})
        self.assertEqual(load_waived_findings(self.store), {})
        self.assertEqual(load_known_fp(self.store), {"acme-api#1": "x"})

    def test_load_cli_reviews_and_reviewed_heads(self):
        self._write({"known_fp": {}, "waived_findings": {},
                     "cli_reviewed": {
                         "acme-api#5": {"head": "sha123", "at": "t",
                                        "sev": {"critical": 0, "major": 1, "minor": 0, "trivial": 0},
                                        "total": 1, "rounds": 2}},
                     "merges": []})
        reviews = load_cli_reviews(self.store)
        self.assertEqual(reviews["acme-api#5"]["rounds"], 2)
        self.assertEqual(reviews["acme-api#5"]["sev"]["major"], 1)
        heads = load_cli_reviewed_heads(self.store)
        self.assertEqual(heads, {"acme-api#5": "sha123"})


class PlanAppliesTest(unittest.TestCase):
    @staticmethod
    def _entry(repo, number, lane="owner", mergeable="MERGEABLE", mss="CLEAN",
               findings=1, critical=False, red_failing=None):
        return {
            "repo": repo, "number": number, "lane": lane,
            "mergeable": mergeable, "mss": mss,
            "red_failing": red_failing or [],
            "cli_findings_open": {"findings": findings, "critical": critical},
        }

    def test_ignores_entries_without_open_findings(self):
        e = self._entry("acme-api", 1)
        del e["cli_findings_open"]
        self.assertEqual(plan_applies([e]), [])

    def test_ranks_by_mergeability_distance_within_the_same_lane(self):
        """A PR one apply away from merging (MERGEABLE + CLEAN) outranks one
        that still needs a rebase (BEHIND) -- applying there converts
        straight into a merge."""
        far = self._entry("acme-api", 201, mss="BEHIND")
        near = self._entry("acme-api", 202, mss="CLEAN")
        applies = plan_applies([far, near])
        self.assertEqual([e["number"] for e in applies], [202, 201])

    def test_deprioritizes_non_owner_lanes(self):
        """Non-owner lanes (team/secondary/secondary_cohort) can never be
        merged by this automation -- so even a non-owner PR mechanically
        CLOSER to merge must rank behind an owner-lane PR that is further
        away. Lane dominates distance."""
        team = self._entry("acme-platform-api", 301, lane="team", mss="CLEAN")
        secondary = self._entry("acme-writer-app", 302, lane="secondary", mss="CLEAN")
        owner_far = self._entry("acme-api", 303, lane="owner", mss="BEHIND")
        applies = plan_applies([team, secondary, owner_far])
        self.assertEqual(applies[0]["number"], 303,
                         "owner lane must rank first even though it is further "
                         "from merging")
        self.assertIn(301, [e["number"] for e in applies[1:]])
        self.assertIn(302, [e["number"] for e in applies[1:]])

    def test_excludes_a_known_fp_pr(self):
        """A PR-wide known_fp waiver must exclude it from the apply queue --
        a finding adjudicated away must not schedule work either, whole-PR
        or single-finding."""
        waived = self._entry("acme-api", 401)
        live = self._entry("acme-api", 402)
        applies = plan_applies([waived, live], known_fp={"acme-api#401": "declined"})
        numbers = [e["number"] for e in applies]
        self.assertNotIn(401, numbers)
        self.assertIn(402, numbers)


class WaiverRoundTripTest(unittest.TestCase):
    """The end-to-end acceptance: `waive` (the shell hook) writes the store,
    `load_waived_findings` (the python reader) reads it back, and a
    `known_fp` waiver keeps a PR out of `plan_applies`'s apply queue."""

    def setUp(self):
        fd, self.store = tempfile.mkstemp(prefix="babysit-progress-roundtrip-", suffix=".json")
        os.close(fd)
        os.remove(self.store)
        self.env = dict(os.environ)
        self.env["BABYSIT_PROGRESS"] = self.store
        # `env` starts as a copy of os.environ, so an exported override would
        # silently flip this test onto the authorized path.
        self.env.pop("BABYSIT_WAIVE_AUTHORIZED_BY", None)
        self.env.pop("BABYSIT_WAIVE_BY", None)

    def tearDown(self):
        if os.path.exists(self.store):
            os.remove(self.store)

    def _run(self, *args):
        p = subprocess.run(["bash", HOOK, *args], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def test_waive_write_read_and_plan_applies_round_trip(self):
        # A nit-grade review on record (zero critical, zero major, <=5 total)
        # lets the waive through WITHOUT the authorized override -- this
        # round-trip exercises the autonomous nit lane end-to-end.
        self._run("set-cli-review", "acme-api#501", "sha-1", "0", "0", "1", "0")
        self._run("waive", "acme-api#501", "app.py:RULE_X", "adjudicated FP", "reviewer")
        self._run("add-fp", "acme-api#502", "ack-reply false positive")

        waived = load_waived_findings(self.store)
        self.assertEqual(waived, {"acme-api#501": {
            "app.py:RULE_X": {"reason": "adjudicated FP",
                              "authorized_by_human": False}}})

        known_fp = load_known_fp(self.store)
        self.assertEqual(known_fp, {"acme-api#502": "ack-reply false positive"})

        entries = [
            {"repo": "acme-api", "number": 502, "lane": "owner",
             "mergeable": "MERGEABLE", "mss": "CLEAN", "red_failing": [],
             "cli_findings_open": {"findings": 1, "critical": False}},
            {"repo": "acme-api", "number": 503, "lane": "owner",
             "mergeable": "MERGEABLE", "mss": "CLEAN", "red_failing": [],
             "cli_findings_open": {"findings": 1, "critical": False}},
        ]
        applies = plan_applies(entries, known_fp=known_fp)
        numbers = [e["number"] for e in applies]
        self.assertNotIn(502, numbers, "known_fp-waived PR must not schedule an apply")
        self.assertIn(503, numbers, "an unwaived sibling still schedules an apply")


# ============================================================================
# hooks/babysit-progress.sh — the autonomous-waiving bar (write-time gate)
# ============================================================================
class BabysitProgressWaivePolicyTest(unittest.TestCase):
    """`waive` must enforce a severity policy at WRITE time, because waiving
    the last finding on a PR is strictly more powerful than any other
    suppression -- the count goes to zero, `cli_open` nulls, the critical
    flag goes with it, and the PR flips "" -> strict -> owner lane ->
    auto-merge and deploy.

    The bar: autonomous waiving is allowed only for <=5 findings, zero
    critical, zero major (the same bar as the nit exemption). Anything
    heavier requires a NAMED human via BABYSIT_WAIVE_AUTHORIZED_BY, and that
    authorization is recorded durably in the store record."""

    KEY = "acme-api#600"
    FK = "services/ingest.py:PRE_SPEND_DEDUPE"

    def setUp(self):
        fd, self.store = tempfile.mkstemp(prefix="babysit-progress-policy-", suffix=".json")
        os.close(fd)
        os.remove(self.store)
        self.env = dict(os.environ)
        self.env["BABYSIT_PROGRESS"] = self.store
        # The bar under test is env-driven, and `env` starts as a copy of
        # os.environ -- an exported BABYSIT_WAIVE_AUTHORIZED_BY would take
        # the override path in every refusal case below and all of them
        # would pass while asserting nothing.
        self.env.pop("BABYSIT_WAIVE_AUTHORIZED_BY", None)
        self.env.pop("BABYSIT_WAIVE_BY", None)

    def tearDown(self):
        if os.path.exists(self.store):
            os.remove(self.store)

    def _run(self, *args, **kw):
        env = dict(self.env)
        env.update(kw.pop("env", {}))
        return subprocess.run(["bash", HOOK, *args],
                              capture_output=True, text=True, env=env)

    def _store_json(self):
        with open(self.store) as fh:
            return json.load(fh)

    def _waived(self, key):
        return (self._store_json().get("waived_findings") or {}).get(key) or {}

    def _record(self, key, crit, major, minor, trivial):
        p = self._run("set-cli-review", key, "abc1234",
                      str(crit), str(major), str(minor), str(trivial))
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_critical_on_record_refuses_the_waive_and_writes_nothing(self):
        """One finding, critical -- exactly the case where a waive would take
        the count to zero and ship an unread critical."""
        self._record(self.KEY, 1, 0, 0, 0)
        p = self._run("waive", self.KEY, self.FK, "stop the apply churn")
        self.assertNotEqual(p.returncode, 0, "a critical waive must FAIL")
        self.assertRegex((p.stderr + p.stdout).lower(), r"critical",
                         "the refusal must name what blocked it")
        self.assertRegex((p.stderr + p.stdout), r"BABYSIT_WAIVE_AUTHORIZED_BY",
                         "the refusal must name the authorized way through")
        self.assertEqual(self._waived(self.KEY), {},
                         "a refused waive must write NOTHING")

    def test_b_major_on_record_refuses_too(self):
        """The bar is zero critical AND zero major."""
        self._record("acme-api#601", 0, 1, 2, 0)
        p = self._run("waive", "acme-api#601", "x.py:RULE", "reason")
        self.assertNotEqual(p.returncode, 0, "a major waive must FAIL")
        self.assertRegex((p.stderr + p.stdout).lower(), r"major")
        self.assertEqual(self._waived("acme-api#601"), {})

    def test_c_more_than_five_findings_refuses(self):
        """<=5 is the other half of the bar; 6 nits is not a nit sweep."""
        self._record("acme-api#602", 0, 0, 4, 2)      # 6 total
        p = self._run("waive", "acme-api#602", "x.py:RULE", "reason")
        self.assertNotEqual(p.returncode, 0, "6 findings exceeds the <=5 bar")
        self.assertEqual(self._waived("acme-api#602"), {})

    def test_d_nits_only_within_the_bar_waive_autonomously(self):
        """The bar must still ALLOW the case it was written for, or the tool
        is just broken. <=5 findings, zero critical, zero major -> no human."""
        self._record("acme-api#603", 0, 0, 3, 1)      # 4 nits
        p = self._run("waive", "acme-api#603", "x.py:RULE", "nit, declined")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("x.py:RULE", self._waived("acme-api#603"))

    def test_e_an_unrecorded_histogram_cannot_clear_the_bar(self):
        """Fail CLOSED on an unknown severity. The bar is a claim about the
        findings; with no recorded review there is nothing to check it
        against, and "we could not look" is not "we looked and it was
        clean"."""
        p = self._run("waive", "acme-api#604", "x.py:RULE", "reason")
        self.assertNotEqual(p.returncode, 0,
                            "no recorded severity -> the bar is unproven -> refuse")
        self.assertEqual(self._waived("acme-api#604"), {})

    def test_f_the_override_permits_it_and_records_the_authorizing_human(self):
        """Possible, deliberate, and never the default path. The override is
        not a boolean: it NAMES the human, that name lands in `by`, and the
        load-bearing field the classifier reads -- `authorized_by_human` --
        is written true."""
        self._record(self.KEY, 1, 0, 0, 0)
        p = self._run("waive", self.KEY, self.FK, "human ruling: intended",
                      env={"BABYSIT_WAIVE_AUTHORIZED_BY": "alice"})
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = self._waived(self.KEY)[self.FK]
        self.assertEqual(rec["by"], "alice",
                         "the authorizing human must be recorded, not 'unknown'")
        self.assertIs(rec["authorized_by_human"], True)
        self.assertIn("since", rec)

    def test_g_the_override_beats_an_explicit_by_argument(self):
        """`by` is caller-supplied and an agent can pass any string; the
        override is the one that had to be set deliberately. If they
        disagree, the deliberate one wins."""
        self._record(self.KEY, 1, 0, 0, 0)
        p = self._run("waive", self.KEY, self.FK, "reason", "some-agent",
                      env={"BABYSIT_WAIVE_AUTHORIZED_BY": "alice"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self._waived(self.KEY)[self.FK]["by"], "alice")

    def test_i_a_whitespace_only_override_is_unset_not_authorization(self):
        """A blank-after-strip BABYSIT_WAIVE_AUTHORIZED_BY must take the
        DEFAULT path (bar enforced, nothing authorized): the paired comment
        renderer strips the same variable, and the two records of one ruling
        action must never disagree."""
        self._record(self.KEY, 1, 0, 0, 0)
        p = self._run("waive", self.KEY, self.FK, "reason",
                      env={"BABYSIT_WAIVE_AUTHORIZED_BY": "   "})
        self.assertNotEqual(p.returncode, 0,
                            "a whitespace-only override must not clear a critical")
        self.assertEqual(self._waived(self.KEY), {})

    def test_h_unauthorized_write_records_the_flag_false(self):
        """The autonomous nit lane still writes a record -- with the flag
        FALSE, so the read side can never mistake it for a human ruling."""
        self._record("acme-api#605", 0, 0, 1, 0)
        p = self._run("waive", "acme-api#605", "x.py:RULE", "nit, declined")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIs(self._waived("acme-api#605")["x.py:RULE"]["authorized_by_human"],
                      False)


if __name__ == "__main__":
    unittest.main()
