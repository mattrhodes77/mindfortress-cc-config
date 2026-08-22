"""Tests for hooks/babysit-fire-log.sh `verdict` — the single-turn forfeit detector.

WHAT IS BEING GUARDED. A `/babysit-prs` sweep run headlessly (`launchd/babysit-
hourly-gate.sh` -> `launchd/headless-skill.sh` -> `claude -p`) is single-turn:
if the model stops emitting tool calls — e.g. because it backgrounded the
classifier and "waited for a completion notification" that headless mode never
sends — the process simply exits `rc=0`, well under a real sweep's runtime,
having done nothing. Every other health signal (the exit code, the pipeline
status, the fire/exit log pairing) reads healthy in that shape.

The one reliable tell is the pair (short duration, no Step 3 ledger row written
since this sweep started), so `verdict` consumes exactly that.

THE FAILURE DIRECTION THAT MATTERS MOST. A detector that over-fires is worse
than the bug it catches: it would relaunch HEALTHY sweeps and double the load on
a queue that is already working. So these tests pin BOTH directions explicitly,
and every ambiguous input (unreadable row, unparseable `ts`, garbage lines) is
asserted to fail toward HEALTHY.
"""
import json
import os
import re
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import HookSandbox, REAL_HOOKS

FIRE_LOG_HOOK = os.path.join(REAL_HOOKS, "babysit-fire-log.sh")

OK, FORFEIT, USAGE = 0, 1, 2


def iso(epoch):
    """Render an epoch the way ledger-append.sh does: ISO8601, UTC, `Z`."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.sbx = HookSandbox()
        os.makedirs(self.sbx.claude, exist_ok=True)
        self.ledger = os.path.join(self.sbx.claude, "automation-ledger.jsonl")
        # A fixed reference point so every fixture's timing is explicit.
        self.start = int(time.time()) - 300

    def tearDown(self):
        self.sbx.close()

    # -- helpers ----------------------------------------------------------
    def verdict(self, rc=0, duration=60, start=None, extra_env=None, lock=None):
        args = ["verdict", str(self.start if start is None else start),
                str(rc), str(duration)]
        if lock is not None:
            args += ["--lock", lock]
        return subprocess.run(
            ["bash", FIRE_LOG_HOOK, *args],
            capture_output=True, text=True,
            env=self.sbx.env(extra_env),
        )

    def write_rows(self, *rows):
        with open(self.ledger, "w") as fh:
            for row in rows:
                fh.write(row if isinstance(row, str) else json.dumps(row))
                fh.write("\n")

    def sweep_row(self, at, **over):
        row = {"skill": "babysit", "event": "sweep", "pending": 22,
               "decision": "PROGRESSING", "ts": iso(at)}
        row.update(over)
        return row

    # -- the healthy direction (the one a wrong field name breaks) ---------
    def test_healthy_sweep_with_a_ledger_row_is_not_flagged(self):
        """A sweep that wrote its Step 3 row is OK even on a short duration.

        This is the exact assertion a wrong-field-name bug would fail: reading
        the timestamp under any key other than `ts` finds no row here and
        reports a forfeit, which would relaunch a sweep that had just completed
        normally.
        """
        self.write_rows(self.sweep_row(self.start + 55))
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)
        self.assertIn("OK", p.stdout)

    def test_field_is_ts_not_timestamp(self):
        """Pin the field name itself, so a rename can't silently pass.

        `ledger-append.sh` writes `ts`. A row carrying only `timestamp` is not a
        row this detector can date, so it must not be counted as proof of work —
        but it also must not be read as a confident absence (see the
        fail-toward-healthy rule), which the `_ambiguous` tests cover.
        """
        row = self.sweep_row(self.start + 55)
        del row["ts"]
        row["timestamp"] = iso(self.start + 55)
        self.write_rows(row)
        p = self.verdict(rc=0, duration=60)
        # No datable row -> cannot confirm work; but undatable != absent.
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)

    def test_long_sweep_without_a_row_is_not_auto_relaunched(self):
        """Only the SHORT shape is the forfeit signature.

        A sweep that ran a long time and wrote no row failed some other way and
        may have done partial work (merges, pushes, CR-CLI launches). Relaunching
        that blindly is a different and riskier action than re-running a sweep
        that provably did nothing, so it stays out of scope here.
        """
        self.write_rows()  # empty ledger
        p = self.verdict(rc=0, duration=900)
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)

    def test_nonzero_rc_is_never_a_silent_forfeit(self):
        """rc!=0 is already loud; the launcher's own rc handling owns it.

        Relaunching here would be actively harmful: a rate-limit/outage exit can
        return non-zero within seconds, and retrying it immediately just burns
        the next fire too.
        """
        self.write_rows()
        for rc in (1, 124, 143):
            p = self.verdict(rc=rc, duration=60)
            self.assertEqual(p.returncode, OK, "rc=%d: %s" % (rc, p.stdout))

    # -- the forfeit direction --------------------------------------------
    def test_short_run_with_no_ledger_row_is_a_forfeit(self):
        self.write_rows()
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, FORFEIT, p.stdout + p.stderr)
        self.assertIn("FORFEIT", p.stdout)

    def test_missing_ledger_file_short_run_is_a_forfeit(self):
        """No ledger, but the writer IS available -> a real absence."""
        self.assertFalse(os.path.exists(self.ledger))
        self.assertTrue(os.access(
            os.path.join(self.sbx.hooks, "ledger-append.sh"), os.X_OK),
            "precondition: the append helper is available in this sandbox")
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, FORFEIT, p.stdout + p.stderr)

    def test_no_ledger_writer_configured_is_not_evidence_of_a_forfeit(self):
        """Step 3's append is a GUARDED NO-OP:

            [ -x ~/.claude/hooks/ledger-append.sh ] && ~/.claude/hooks/ledger-append.sh …

        So on a machine where that helper is missing or non-executable, a
        perfectly healthy sweep writes no row by design. Reading that as proof
        the sweep did nothing would relaunch healthy sweeps on every short
        cycle — the exact over-fire this detector must never commit. Absence is
        only evidence when the writer was actually available.
        """
        os.unlink(os.path.join(self.sbx.hooks, "ledger-append.sh"))
        self.assertFalse(os.path.exists(self.ledger))
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)
        self.assertIn("no ledger writer", p.stdout)

    def test_no_writer_but_a_row_exists_still_reads_as_healthy(self):
        """A stale/foreign ledger with no writer must not flip to forfeit either."""
        os.unlink(os.path.join(self.sbx.hooks, "ledger-append.sh"))
        self.write_rows(self.sweep_row(self.start - 1200))  # previous sweep only
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)

    def test_a_previous_sweeps_row_does_not_count(self):
        """The row must postdate THIS sweep's start, not merely exist.

        Without the `>= sweep_start` bound the detector reads the last healthy
        sweep's row forever and never fires again.
        """
        self.write_rows(self.sweep_row(self.start - 1200))
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, FORFEIT, p.stdout + p.stderr)

    def test_a_row_at_exactly_sweep_start_counts(self):
        self.write_rows(self.sweep_row(self.start))
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)

    def test_another_skills_row_does_not_count(self):
        """bulldozer/PRlaunch share this ledger; only a babysit sweep row proves
        a babysit sweep ran."""
        self.write_rows(
            {"skill": "bulldozer", "event": "sweep", "ts": iso(self.start + 55)},
            {"skill": "babysit", "event": "merge_deploy", "ts": iso(self.start + 55)},
        )
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, FORFEIT, p.stdout + p.stderr)

    # -- a LOCKED skip is not a forfeit ------------------------------------
    #
    # A sweep that hits the mutex at Step 0 and skips exits rc=0, in well under
    # the forfeit threshold, having written no ledger row — BYTE-IDENTICAL to
    # the forfeit signature. Observed in practice: a routine LOCKED skip (another
    # sweep already held the lock) is common at a short cadence, so firing on it
    # trains the operator to ignore FORFEIT, which defeats the detector entirely.
    #
    # The discriminator is lock OWNERSHIP, and the launcher already computes it
    # one line earlier: a real forfeit acquired the lock at Step 0 and died
    # before Step 4's release, so `reap-since` REAPS it; a skip never acquired
    # it, so `reap-since` says KEPT (someone else's).

    def test_kept_lock_means_we_never_acquired_so_never_a_forfeit(self):
        self.write_rows()  # no row — otherwise identical to a forfeit
        p = self.verdict(rc=0, duration=16, lock="kept")
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)
        self.assertIn("LOCKED skip", p.stdout)

    def test_reaped_lock_means_we_did_acquire_so_still_a_forfeit(self):
        """The forfeit direction must survive the fix — this is the case the
        lock-ownership check exists for: we held the lock and died before
        releasing it."""
        self.write_rows()
        p = self.verdict(rc=0, duration=60, lock="reaped")
        self.assertEqual(p.returncode, FORFEIT, p.stdout + p.stderr)

    def test_reap_failed_still_counts_as_ours(self):
        """`REAP FAILED` means the lock WAS ours and `rm` refused — we held it,
        so a forfeit is still live."""
        self.write_rows()
        p = self.verdict(rc=0, duration=60, lock="failed")
        self.assertEqual(p.returncode, FORFEIT, p.stdout + p.stderr)

    def test_unlocked_falls_through_to_the_ledger_check(self):
        """`UNLOCKED` is genuinely ambiguous — either a healthy sweep released
        normally, or a skip whose blocker finished first. Keep the pre-existing
        behaviour rather than inventing a verdict."""
        self.write_rows()
        self.assertEqual(self.verdict(rc=0, duration=60, lock="unlocked").returncode,
                         FORFEIT)
        self.write_rows(self.sweep_row(self.start + 30))
        self.assertEqual(self.verdict(rc=0, duration=60, lock="unlocked").returncode,
                         OK)

    def test_the_flag_is_optional_and_back_compatible(self):
        """A caller that predates this flag must keep working exactly as before."""
        self.write_rows()
        self.assertEqual(self.verdict(rc=0, duration=60).returncode, FORFEIT)
        self.write_rows(self.sweep_row(self.start + 30))
        self.assertEqual(self.verdict(rc=0, duration=60).returncode, OK)

    def test_an_unrecognised_lock_outcome_is_a_usage_error(self):
        """A typo must not silently read as 'kept' and suppress every forfeit."""
        p = self.verdict(rc=0, duration=60, lock="kpet")
        self.assertEqual(p.returncode, USAGE, p.stdout + p.stderr)

    def test_kept_lock_wins_even_over_a_zero_duration(self):
        """Ownership is decisive: if the lock was never ours there is nothing we
        could have forfeited, whatever the duration says."""
        self.write_rows()
        p = self.verdict(rc=0, duration=1, lock="kept")
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)

    # -- ambiguity must fail toward HEALTHY -------------------------------
    def test_garbage_lines_alongside_a_valid_row_stay_ok(self):
        self.write_rows(
            "not json at all",
            "{broken",
            self.sweep_row(self.start + 55),
        )
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)

    def test_unparseable_ts_on_a_matching_row_fails_toward_healthy(self):
        """A babysit sweep row IS present; only its date is unreadable.

        Declaring a forfeit here would relaunch on a formatting problem. The
        detector must not manufacture a confident absence out of an unreadable
        value.
        """
        self.write_rows(self.sweep_row(self.start + 55, ts="not-a-date"))
        p = self.verdict(rc=0, duration=60)
        self.assertEqual(p.returncode, OK, p.stdout + p.stderr)

    def test_ledger_that_cannot_be_read_fails_toward_healthy(self):
        self.write_rows(self.sweep_row(self.start + 55))
        os.chmod(self.ledger, 0o000)
        try:
            p = self.verdict(rc=0, duration=60)
            self.assertEqual(p.returncode, OK, p.stdout + p.stderr)
        finally:
            os.chmod(self.ledger, 0o644)

    # -- threshold + usage -------------------------------------------------
    def test_threshold_is_overridable(self):
        self.write_rows()
        p = self.verdict(rc=0, duration=200)
        self.assertEqual(p.returncode, OK, "200s is above the 150s default")
        p = self.verdict(rc=0, duration=200,
                         extra_env={"BABYSIT_FORFEIT_MAX_S": "300"})
        self.assertEqual(p.returncode, FORFEIT, p.stdout + p.stderr)

    def test_bad_arguments_are_a_usage_error_not_a_verdict(self):
        for args in ([], ["verdict"], ["verdict", "abc", "0", "60"],
                     ["verdict", "123", "x", "60"], ["verdict", "123", "0", "y"]):
            p = subprocess.run(
                ["bash", FIRE_LOG_HOOK, *args],
                capture_output=True, text=True, env=self.sbx.env(),
            )
            self.assertEqual(p.returncode, USAGE, "args=%r -> %s" % (args, p.stderr))

    def test_the_real_writer_and_this_reader_agree_on_the_row_shape(self):
        """End-to-end against hooks/ledger-append.sh — no hand-written fixture.

        Every other test here writes the ledger row itself, so all of them stay
        green if the WRITER and this READER drift apart: rename `ts` in
        ledger-append.sh, or change the `skill`/`event` values babysit-prs.md
        emits, and the detector silently stops finding anything. It would then
        fail toward healthy forever — no false relaunches, but no detection
        either, which is the same silence this detector exists to end.

        So this case drives the ACTUAL append hook with the ACTUAL Step 3 payload.
        It asserts the REASON, not just the exit code: `OK` alone is ambiguous
        here, because the fail-toward-healthy rule also returns OK when a row is
        present but undatable — so an exit-code-only assertion stays green under
        exactly the drift it is meant to catch.
        """
        ledger_hook = os.path.join(REAL_HOOKS, "ledger-append.sh")
        # Exactly the payload commands/babysit-prs.md Step 3 emits.
        payload = json.dumps({
            "skill": "babysit", "event": "sweep", "pending": 22,
            "bumps": 0, "fixes": 3, "red_ci": 4, "decision": "PROGRESSING",
        })

        before = self.verdict(rc=0, duration=60)
        self.assertEqual(before.returncode, FORFEIT,
                         "precondition: no row yet -> forfeit")

        appended = subprocess.run(
            ["bash", ledger_hook, payload],
            capture_output=True, text=True, env=self.sbx.env(),
        )
        self.assertEqual(appended.returncode, 0, appended.stderr)

        after = self.verdict(rc=0, duration=60)
        self.assertEqual(after.returncode, OK,
                         "a row written by the real ledger-append.sh must be "
                         "seen by verdict: %s" % (after.stdout + after.stderr))
        self.assertIn(
            "wrote its Step 3 ledger row", after.stdout,
            "verdict must DATE the real writer's row, not merely tolerate it as "
            "undatable — the undatable path is the silent-drift failure mode: "
            "got %r" % after.stdout,
        )

    def test_fire_and_exit_subcommands_still_work(self):
        """The existing interface every launcher already calls must be untouched."""
        log = os.path.join(self.sbx.claude, "logs", "headless-babysit.log")
        for args in (["fire", "/babysit-prs no-loop"], ["exit", "0"]):
            p = subprocess.run(
                ["bash", FIRE_LOG_HOOK, *args],
                capture_output=True, text=True, env=self.sbx.env(),
            )
            self.assertEqual(p.returncode, 0, p.stderr)
        with open(log) as fh:
            body = fh.read()
        self.assertIn("[babysit] fire: /babysit-prs no-loop", body)
        self.assertIn("[babysit] exit=0", body)


class ForfeitThresholdOverrideTest(VerdictTest):
    """A typo'd override must degrade to the default, never kill the hook.

    `verdict` runs under `set -e`, so an unvalidated BABYSIT_FORFEIT_MAX_S makes
    the `-ge` comparison abort the script with no output and no verdict — a
    silent failure inside the guard whose whole purpose is ending silent
    failures, and one the launcher would read as "not a forfeit".
    """

    def test_non_numeric_override_falls_back_to_the_default(self):
        self.write_rows()  # empty ledger -> a 60s rc=0 run is a forfeit at 150s
        p = self.verdict(rc=0, duration=60,
                         extra_env={"BABYSIT_FORFEIT_MAX_S": "abc"})
        self.assertEqual(p.returncode, FORFEIT,
                         "must still reach a verdict: %s" % (p.stdout + p.stderr))
        self.assertIn("not a number", p.stderr)
        self.assertIn("150", p.stdout)

    def test_empty_override_falls_back_to_the_default(self):
        self.write_rows()
        p = self.verdict(rc=0, duration=200,
                         extra_env={"BABYSIT_FORFEIT_MAX_S": ""})
        self.assertEqual(p.returncode, OK, "200s is above the 150s default")


class LauncherWiringTest(unittest.TestCase):
    """A detector nothing calls is not a detector.

    `verdict` only ends the silence if the headless launcher actually consumes
    it, and the orphan reap only holds if it survives future edits — so assert
    the wiring in launchd/babysit-hourly-gate.sh, not just the primitive.
    """

    GATE = "launchd/babysit-hourly-gate.sh"

    def _read(self, rel):
        with open(os.path.join(os.path.dirname(REAL_HOOKS), rel)) as fh:
            return fh.read()

    def test_the_gate_consumes_the_verdict(self):
        # The gate quotes the hook path ("$HOME/.claude/hooks/…" verdict), so
        # match the call itself, not one spelling of the path.
        call = re.compile(r'babysit-fire-log\.sh"?\s+verdict\b')
        self.assertRegex(self._read(self.GATE), call,
                         "%s must call the forfeit detector" % self.GATE)

    def test_the_gate_relaunches_at_most_once(self):
        """Exactly one retry. A second forfeit is a regression in the prose
        rule, and must stay visible rather than be papered over by an
        unbounded loop."""
        body = self._read(self.GATE)
        self.assertIn('attempt -ge 2', body,
                      "a second attempt must never trigger a third")
        self.assertIn("attempt=2", body)
        self.assertNotIn("attempt=3", body)

    def test_the_gate_reaps_orphaned_classifiers_before_relaunching(self):
        """Relaunching on top of a stranded classifier runs two at once.

        The PPID-1 test is the safety property — it is what keeps a LIVE
        sweep's classifier (a concurrent sweep elsewhere) from being killed —
        so pin it, not merely the presence of a `kill`.
        """
        body = self._read(self.GATE)
        self.assertIn("babysit_classify.py", body,
                      "the reap must target the classifier specifically")
        self.assertIn('"$cppid" = "1"', body,
                      "only a process reparented to init (parent sweep gone) may be killed")

    def test_the_orphan_reap_waits_for_the_process_to_actually_die(self):
        """`kill` only queues a SIGTERM and returns.

        Continuing straight to the relaunch lets a slow or unresponsive
        classifier overlap attempt 2 — the exact race the reap exists to
        prevent, so the wait is the load-bearing half, not the `kill`.
        """
        body = self._read(self.GATE)
        self.assertIn("kill -0", body, "the gate must poll until the orphan is gone")
        self.assertIn("kill -9", body, "the gate must escalate to SIGKILL after the grace period")

    def test_an_unkillable_orphan_blocks_the_relaunch(self):
        """Warning about a surviving classifier and then relaunching anyway is
        the double-run the reap exists to prevent — a failed reap must block
        attempt 2, not merely log about it."""
        body = self._read(self.GATE)
        self.assertIn("recovery=blocked_by_orphan", body)
        # The block must actually skip the relaunch, not just set a variable
        # nobody reads: it must appear before the attempt is bumped to 2.
        self.assertLess(body.index("recovery=blocked_by_orphan"),
                        body.rindex("attempt=2"),
                        "the orphan-blocked path must short-circuit before relaunching")

    def test_the_reap_precedes_the_relaunch(self):
        """Order matters: reaping after the attempt is bumped would never run
        for the sweep that actually forfeited."""
        body = self._read(self.GATE)
        block = body.split("FORFEIT on attempt", 1)[1].split("attempt=2", 1)[0]
        self.assertIn("babysit_classify.py", block,
                      "the orphan reap must sit between the FORFEIT decision and the relaunch")

    def test_the_gate_passes_the_lock_outcome_to_verdict(self):
        """A detector that is never told who owned the lock cannot tell a
        forfeit from a routine LOCKED skip. The gate must classify the reap
        outcome and pass it through."""
        body = self._read(self.GATE)
        self.assertRegex(body, r'verdict[^\n]*--lock',
                         "%s must pass --lock to verdict" % self.GATE)
        # Assert the actual case ARMS, not the bare words — the file discusses
        # these outcomes in prose too, so a token check alone would pass even
        # with the classifier deleted.
        for arm in (r'"REAP FAILED"\*\)', r'REAPED\*\)', r'KEPT\*\)', r'UNLOCKED\*\)'):
            self.assertRegex(body, arm,
                             "%s must have a real case arm for %s" % (self.GATE, arm))
        # "REAP FAILED" must be matched BEFORE any other REAP* arm, or it falls
        # through to the catch-all.
        self.assertLess(body.index('"REAP FAILED"*)'), body.index('REAPED*)'),
                        "%s: the REAP FAILED arm must precede REAPED*" % self.GATE)

    def test_the_gate_stops_after_a_second_forfeit(self):
        """A second forfeit is not retried again — it is a real regression in
        the EXECUTION MODE rule, not a blip, and must point back at it."""
        body = self._read(self.GATE)
        self.assertIn("FORFEIT AGAIN", body)
        self.assertIn("EXECUTION MODE", body,
                      "the stop message must point at the prose rule in commands/babysit-prs.md")
        self.assertIn("commands/babysit-prs.md", body)

    def test_the_gate_preserves_fail_closed_signal_handling(self):
        """A signal mid-sweep must NOT reap the lock (the sweep may still be
        alive under a different reparented tree) — this must survive the
        retry-on-forfeit rewrite unchanged."""
        body = self._read(self.GATE)
        self.assertIn("trap 'on_signal; exit 143' TERM", body)
        self.assertIn("trap 'on_signal; exit 130' INT", body)
        self.assertIn("lock NOT reaped", body)


if __name__ == "__main__":
    unittest.main()
