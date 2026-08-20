"""Tests for the sweep mutex's lifecycle: hooks/babysit-lock.sh.

The defect these cover: a sweep could exit -- cleanly or not -- without ever
releasing /tmp/babysit-prs.lock, and nothing reaped it before the next fire.
The stale TTL cannot be the mechanism, because it is pulled in two opposite
directions at once:

  L1  a LIVE sweep must never let its own lock expire  -> TTL > sweep cap 3000s
  L2  a DEAD sweep must never eat a fire (cadence ~20m) -> would need TTL < 1200s

L1 and L2 are unsatisfiable by one number, so L1 owns the TTL (3600s) and the
deterministic `reap-since` owns L2. These tests pin both halves.

Everything runs against a throwaway lock file via the hook's own $BABYSIT_LOCK
override -- NEVER the live /tmp/babysit-prs.lock, which a real babysit sweep on
this machine may be holding right now. Same isolation rule as
tests/test_babysit_waivers.py.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_HOOK = os.path.join(REPO_ROOT, "hooks", "babysit-lock.sh")

# The `timeout <N>` every launcher wraps its sweep in (the argument the example
# plists pass to babysit-hourly-gate.sh -> headless-skill.sh). L1 is stated
# against this number, so the tests assert the relationship, not a magic value.
SWEEP_CAP_S = 3000
# The example sweep cadence (`9,29,49 * * * *`).
CRON_PERIOD_S = 1200


class LockTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="babysit-lock-test-")
        self.lock = os.path.join(self.tmp, "babysit-prs.lock")
        self.env = dict(os.environ)
        # Pin the lock away from live state. Without this the hook defaults to
        # /tmp/babysit-prs.lock, i.e. a test would read and DELETE a live
        # sweep's mutex.
        self.env["BABYSIT_LOCK"] = self.lock
        self.env["CLAUDE_CODE_SESSION_ID"] = "test-session-aaa"

    def run_lock(self, *args, session=None, launcher="test-launcher-1"):
        env = dict(self.env)
        if session is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session
        if launcher is None:
            env.pop("BABYSIT_LAUNCH_ID", None)
        else:
            env["BABYSIT_LAUNCH_ID"] = launcher
        return subprocess.run(["bash", LOCK_HOOK, *args],
                              capture_output=True, text=True, env=env)

    def write_lock(self, *, owner="someone-else", started=None, heartbeat=None,
                   launcher="test-launcher-1"):
        now = int(time.time())
        doc = {"owner": owner, "host": "testhost", "pid": 1, "launcher": launcher,
               "started": now if started is None else started,
               "heartbeat": now if heartbeat is None else heartbeat}
        with open(self.lock, "w") as fh:
            json.dump(doc, fh)
        return doc


class LockTtlInvariantTest(LockTestBase):
    """L1: a live sweep must never let its OWN lock expire."""

    def _default_ttl(self):
        """The TTL the hook actually uses when nothing overrides it."""
        m = re.search(r'TTL="\$\{BABYSIT_LOCK_TTL:-(\d+)\}"', open(LOCK_HOOK).read())
        self.assertIsNotNone(m, "TTL default not found in babysit-lock.sh")
        return int(m.group(1))

    def test_l1_a_sweep_still_inside_its_cap_keeps_its_lock(self):
        """Asserted BEHAVIOURALLY rather than by reading the constant: a sweep
        that has been running for the full sweep cap without one single
        refresh -- the worst case, since the heartbeat only fires at step
        boundaries -- must still hold its lock against the next fire. Under a
        TTL below the cap this same call would return ACQUIRED and a
        duplicate sweep would start on top of a healthy one."""
        aged = int(time.time()) - (SWEEP_CAP_S + 30)
        self.write_lock(owner="live-slow-sweep", started=aged, heartbeat=aged)
        p = self.run_lock("acquire", session="next-fire")
        self.assertEqual(p.returncode, 3,
                         "a sweep %ds into its %ds cap was reaped: %r"
                         % (SWEEP_CAP_S + 30, SWEEP_CAP_S, p.stdout))
        self.assertIn("LOCKED", p.stdout)

    def test_l1_a_lock_older_than_any_possible_sweep_is_reaped(self):
        """The other side of L1: past the TTL no live sweep can exist (every
        launcher's `timeout -k` bounds it well below), so the lock must clear."""
        ttl = self._default_ttl()
        aged = int(time.time()) - (ttl + 30)
        self.write_lock(owner="long-dead", started=aged, heartbeat=aged)
        p = self.run_lock("acquire", session="next-fire")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ACQUIRED", p.stdout)

    def test_l2_the_ttl_alone_would_still_eat_a_fire(self):
        """Why `reap-since` has to exist at all: a TTL satisfying L1 is
        necessarily longer than the launch cadence, so at one cadence period
        after a crash the lock is STILL held. Asserted behaviourally -- this
        is the exact call the next fire makes."""
        self.assertGreater(self._default_ttl(), CRON_PERIOD_S,
                           "premise: a TTL satisfying L1 always outlives the cadence")
        crashed = int(time.time()) - CRON_PERIOD_S
        self.write_lock(owner="crashed-sweep", started=crashed, heartbeat=crashed)
        p = self.run_lock("acquire", session="next-fire")
        self.assertEqual(p.returncode, 3,
                         "premise: one cadence period after a crash the TTL has not expired")
        self.assertIn("LOCKED", p.stdout)

    def test_a_live_sweeps_lock_is_not_stolen_before_the_ttl(self):
        """Another session's lock, heartbeat fresh -> LOCKED, exit 3."""
        self.write_lock(owner="other-session", heartbeat=int(time.time()) - 60)
        p = self.run_lock("acquire")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("LOCKED", p.stdout)
        self.assertTrue(os.path.exists(self.lock), "a live sweep's lock must survive")

    def test_a_corrupt_heartbeat_does_not_crash_acquire(self):
        """heartbeat="12abc" used to abort the arithmetic under set -e --
        acquire exited 1 (neither ACQUIRED=0 nor LOCKED=3, undefined for
        every caller) and, because the crash preceded the TTL check, the
        corrupt lock could never self-heal. It must degrade to stale and be
        reaped."""
        now = int(time.time())
        with open(self.lock, "w") as fh:
            json.dump({"owner": "torn-write", "host": "h", "pid": 1,
                       "launcher": "x", "started": now, "heartbeat": "12abc"}, fh)
        p = self.run_lock("acquire")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ACQUIRED", p.stdout)

    def test_an_absurd_future_heartbeat_cannot_wedge_the_lock_forever(self):
        """A huge all-digit heartbeat makes `age` negative, so the lock would
        read LOCKED forever -- un-expirable by TTL, kept by reap-since
        (foreign launcher): one torn/adversarial write to world-writable /tmp
        would wedge every future sweep until manual removal. Same 12-digit
        cap as reap-since's `started`."""
        with open(self.lock, "w") as fh:
            json.dump({"owner": "wedge", "host": "h", "pid": 1, "launcher": "x",
                       "started": 1, "heartbeat": 99999999999999}, fh)
        p = self.run_lock("acquire")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ACQUIRED", p.stdout)

    def test_a_lock_past_the_ttl_is_reaped_on_acquire(self):
        ttl = self._default_ttl()
        self.write_lock(owner="other-session", heartbeat=int(time.time()) - (ttl + 60))
        p = self.run_lock("acquire")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ACQUIRED", p.stdout)


class ReapSinceTest(LockTestBase):
    """L2: the lock a just-finished sweep left behind is gone before the next
    fire, however that sweep terminated."""

    def test_lock_taken_after_our_launch_is_reaped(self):
        """The core shape: sweep launched, acquired the lock, then died
        without releasing. Its launcher reaps it."""
        sweep_start = int(time.time()) - 300
        self.write_lock(owner="dead-sweep", started=sweep_start + 20,
                        heartbeat=sweep_start + 260)  # heartbeat 40s old: NOT TTL-stale
        p = self.run_lock("reap-since", str(sweep_start))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("REAPED", p.stdout)
        self.assertFalse(os.path.exists(self.lock),
                         "the orphaned lock must be gone, not waiting out the TTL")

    def test_the_next_fire_acquires_instead_of_skipping_LOCKED(self):
        """The acceptance criterion, end to end: kill a sweep mid-run and the
        NEXT fire proceeds instead of LOCKED-skipping. Before the reap, that
        same fire is LOCKED -- both halves asserted so the test can only pass
        because of the reap."""
        sweep_start = int(time.time()) - 300
        self.write_lock(owner="dead-sweep", started=sweep_start + 20,
                        heartbeat=sweep_start + 290)  # 10s old: maximally un-reapable by TTL

        blocked = self.run_lock("acquire", session="next-fire")
        self.assertEqual(blocked.returncode, 3, "premise: without the reap the next fire skips")
        self.assertIn("LOCKED", blocked.stdout)

        self.run_lock("reap-since", str(sweep_start))
        proceeds = self.run_lock("acquire", session="next-fire")
        self.assertEqual(proceeds.returncode, 0, proceeds.stdout + proceeds.stderr)
        self.assertIn("ACQUIRED", proceeds.stdout)

    def test_a_live_sweep_in_another_session_is_never_reaped(self):
        """A genuinely live sweep is never reaped: a lock taken BEFORE our
        sweep started belongs to someone else's still-running sweep -- and
        the pid is never the proof."""
        sweep_start = int(time.time())
        self.write_lock(owner="live-other-sweep", started=sweep_start - 120,
                        heartbeat=sweep_start)
        p = self.run_lock("reap-since", str(sweep_start))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("KEPT", p.stdout)
        self.assertTrue(os.path.exists(self.lock))

    def test_a_foreign_live_lock_taken_AFTER_our_launch_is_not_reaped(self):
        """The hole a time-bound-only rule leaves open, and the reason the
        lock carries a `launcher` at all.

        Routine sequence, not a corner case: WE launch a sweep; it hits
        `LOCKED` at Step 0 because another session's sweep is running, and
        exits in ~60s with rc=0. That other sweep acquired its lock a few
        seconds AFTER we launched (two launchers with a ~2 min stagger).
        `started >= since` is therefore TRUE for a lock that a live sweep is
        holding right now, and reaping it would put two sweeps on the same
        clones, CR-CLI quota and bump budget -- exactly what the mutex exists
        to prevent."""
        our_launch = int(time.time()) - 30
        self.write_lock(owner="OTHER-LIVE-SESSION", launcher="loop-999-1786000000",
                        started=our_launch + 10, heartbeat=int(time.time()))
        p = self.run_lock("reap-since", str(our_launch), launcher="watch-111-1786000000")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("KEPT", p.stdout)
        self.assertTrue(os.path.exists(self.lock),
                        "a DIFFERENT launcher's live lock was reaped despite being newer "
                        "than our launch -- the time bound alone is not ownership")
        # and it must still block us, i.e. the mutex still works
        self.assertEqual(self.run_lock("acquire", session="third").returncode, 3)

    def test_an_unlabeled_lock_fails_closed(self):
        """A sweep started by hand (no BABYSIT_LAUNCH_ID) leaves a lock with no
        `launcher`. Nothing can prove it is ours, so it is left to the TTL
        rather than reaped on the time bound alone."""
        sweep_start = int(time.time()) - 100
        self.write_lock(owner="hand-run", launcher="", started=sweep_start + 5)
        p = self.run_lock("reap-since", str(sweep_start))
        self.assertIn("KEPT", p.stdout)
        self.assertTrue(os.path.exists(self.lock))

    def test_a_launcher_with_no_id_of_its_own_reaps_nothing(self):
        """The mirror case: a caller that forgot to export BABYSIT_LAUNCH_ID
        must not fall back to reaping by time."""
        sweep_start = int(time.time()) - 100
        self.write_lock(owner="dead", launcher="watch-1-2", started=sweep_start + 5)
        p = self.run_lock("reap-since", str(sweep_start), launcher=None)
        self.assertIn("KEPT", p.stdout)
        self.assertTrue(os.path.exists(self.lock))

    def test_acquire_records_the_launcher_id_so_reap_since_can_match_it(self):
        """End to end through the real acquire path: the id the LAUNCHER
        exported reaches the lock file, so the launcher can later prove the
        lock is its own sweep's."""
        p = self.run_lock("acquire", launcher="loop-42-1786000000")
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(self.lock) as fh:
            self.assertEqual(json.load(fh)["launcher"], "loop-42-1786000000")
        r = self.run_lock("reap-since", "1", launcher="loop-42-1786000000")
        self.assertIn("REAPED", r.stdout)
        self.assertFalse(os.path.exists(self.lock))

    def test_ownership_is_never_the_pid(self):
        """A LIVE sweep's lock routinely names a DEAD pid ($PPID of the
        acquiring shell, which exits immediately), so a pid-based reap would
        destroy live mutexes. pid 999999 does not exist; the lock must survive
        purely because `.started` predates our sweep."""
        sweep_start = int(time.time())
        with open(self.lock, "w") as fh:
            json.dump({"owner": "live", "host": "h", "pid": 999999,
                       "launcher": "test-launcher-1",
                       "started": sweep_start - 60, "heartbeat": sweep_start}, fh)
        p = self.run_lock("reap-since", str(sweep_start))
        self.assertIn("KEPT", p.stdout)
        self.assertTrue(os.path.exists(self.lock))

    def test_a_symlinked_backup_path_is_not_followed(self):
        """$LOCK lives in world-writable /tmp, so any local process can
        pre-create <lock>.reaped as a symlink; `cp` would then write THROUGH it
        and clobber the target."""
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w") as fh:
            fh.write("PRECIOUS")
        os.symlink(victim, self.lock + ".reaped")
        sweep_start = int(time.time()) - 100
        self.write_lock(owner="dead", started=sweep_start + 5)
        self.run_lock("reap-since", str(sweep_start))
        with open(victim) as fh:
            self.assertEqual(fh.read(), "PRECIOUS", "the reap wrote through a symlink")

    def test_an_absurd_started_value_does_not_leak_a_shell_error(self):
        """A huge all-digit `started` passes the pattern filter but would make
        `[ -ge ]` print a raw 'integer expression expected' into the launchd
        log. Must degrade quietly and fail closed."""
        with open(self.lock, "w") as fh:
            json.dump({"owner": "x", "host": "h", "pid": 1, "launcher": "test-launcher-1",
                       "started": 99999999999999999999, "heartbeat": 1}, fh)
        p = self.run_lock("reap-since", str(int(time.time())))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("integer expression", p.stdout + p.stderr)
        self.assertIn("KEPT", p.stdout)

    def test_reap_backs_the_lock_up_before_removing_it(self):
        """The lock file is backed up before removal."""
        sweep_start = int(time.time()) - 100
        doc = self.write_lock(owner="dead-sweep", started=sweep_start + 5)
        self.run_lock("reap-since", str(sweep_start))
        backup = self.lock + ".reaped"
        self.assertTrue(os.path.exists(backup), "no backup written")
        with open(backup) as fh:
            self.assertEqual(json.load(fh)["started"], doc["started"])

    def test_reap_is_never_silent(self):
        """The reap is reported, never silent -- all three outcomes."""
        sweep_start = int(time.time()) - 100
        self.assertIn("UNLOCKED", self.run_lock("reap-since", str(sweep_start)).stdout)
        self.write_lock(owner="dead", started=sweep_start + 5)
        self.assertIn("REAPED", self.run_lock("reap-since", str(sweep_start)).stdout)
        self.write_lock(owner="live", started=sweep_start - 5)
        self.assertIn("KEPT", self.run_lock("reap-since", str(sweep_start)).stdout)

    def test_missing_or_bad_argument_is_a_usage_error_not_a_wipe(self):
        """A bare or non-numeric `reap-since` must never fall through to
        deleting the lock -- that would be an unconditional mutex wipe."""
        self.write_lock(owner="live", started=int(time.time()))
        for args in (["reap-since"], ["reap-since", "not-a-number"], ["reap-since", ""]):
            p = self.run_lock(*args)
            self.assertEqual(p.returncode, 2, "args=%r -> %r" % (args, p.stdout))
            self.assertTrue(os.path.exists(self.lock), "args=%r wiped the lock" % (args,))

    def test_corrupt_lock_file_does_not_crash_the_launcher(self):
        """A torn/garbage lock must not make a launcher exit non-zero mid-loop.
        `started` degrades to 0, which is < any real epoch, so it is KEPT --
        fail-closed, the safe direction for a mutex."""
        with open(self.lock, "w") as fh:
            fh.write("{not json")
        p = self.run_lock("reap-since", str(int(time.time())))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("KEPT", p.stdout)


class LauncherWiringTest(unittest.TestCase):
    """The hook above is only worth anything if the launchers call it. The
    original defect's whole shape was a mechanism that existed but was reached
    only on some exit paths, so assert the wiring, not just the primitives."""

    def _read(self, rel):
        with open(os.path.join(REPO_ROOT, rel)) as fh:
            return fh.read()

    def test_the_hourly_gate_reaps_its_sweeps_lock(self):
        self.assertIn("reap-since", self._read("launchd/babysit-hourly-gate.sh"),
                      "the gate launches sweeps but never reaps their lock")

    def test_the_gate_reaps_on_normal_exit_and_fails_closed_on_signals(self):
        """`exec` would hand the process to headless-skill.sh and skip any
        post-sweep reap; the gate must run the sweep as a child and reap from
        an EXIT trap. The SIGNAL path must NOT reap: kill+wait on the direct
        child proves nothing about the `timeout`/`claude` GRANDCHILD that
        actually holds the lock (a targeted kill -TERM of the gate leaves it
        running), so the only safe signal behaviour is to leave the lock to
        the TTL. (?m) is load-bearing on the exec check -- without it `^`
        only matches the start of the file and a restored `exec` line would
        slip through."""
        body = self._read("launchd/babysit-hourly-gate.sh")
        self.assertNotRegex(body, r"(?m)^\s*exec .*headless-skill",
                            "exec skips the reap")
        self.assertRegex(body, r"trap 'reap_once' EXIT")
        self.assertRegex(body, r"trap 'on_signal; exit 143' TERM")
        # extract on_signal()'s body: it must suppress the reap, never run it
        m = re.search(r"on_signal\(\) \{(.*?)\n\}", body, re.S)
        self.assertIsNotNone(m, "on_signal() not found")
        self.assertNotIn("reap_once", m.group(1),
                         "the signal path must not reap -- the grandchild may "
                         "still hold the lock")
        self.assertIn("reaped=1", m.group(1),
                      "the signal path must suppress the EXIT trap's reap too")

    def test_no_launcher_caps_its_sweep_above_the_lock_ttl(self):
        """L1 as a guard, not a comment: the TTL is only safe because every
        launcher bounds its sweep below it. A future edit raising a plist's
        `timeout` argument above the TTL silently re-opens the
        healthy-long-sweep-reaped-and-duplicated hole, and nothing else would
        catch it -- the numbers live in different files."""
        ttl = int(re.search(r'TTL="\$\{BABYSIT_LOCK_TTL:-(\d+)\}"',
                            self._read("hooks/babysit-lock.sh")).group(1))
        # The babysit plist only: the bulldozer example invokes
        # headless-skill.sh for a DIFFERENT skill that does not take
        # /tmp/babysit-prs.lock, so asserting its cap against this TTL would
        # claim a relationship that does not exist.
        rel = "launchd/examples/com.you.claude-babysit-hourly.plist"
        m = re.search(r"<string>(\d+)</string>", self._read(rel))
        self.assertIsNotNone(m, "%s: no timeout argument found" % rel)
        cap = int(m.group(1))
        self.assertLess(cap, ttl,
                        "%s caps its sweep at %ds, at or above the %ds lock TTL: a live "
                        "sweep could outlive its own lock" % (rel, cap, ttl))

    def test_the_sweep_cap_is_enforced_with_sigkill_not_just_sigterm(self):
        """A cap is only a BOUND if it cannot be ignored. Plain `timeout`
        sends SIGTERM; a wedged claude that blocks or ignores it runs past the
        cap and then goes stale at the TTL while still alive -- precisely the
        L1 violation this design rules out. `-k` is what closes it."""
        self.assertRegex(self._read("launchd/headless-skill.sh"),
                         r'timeout -k \d+ "\$TIMEOUT"',
                         "headless-skill.sh caps its sweep without -k, so the cap is "
                         "a request the sweep can ignore, not a bound")


if __name__ == "__main__":
    unittest.main()
