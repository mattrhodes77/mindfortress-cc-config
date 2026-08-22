"""Shared test harness for the safety-hook suite.

Design goals:
  * ISOLATION: every hook that touches $HOME/.claude/... must run against a
    throwaway HOME containing a .claude/ skeleton that symlinks the REAL hook
    scripts. The real config (cleanup-needed.log, prlaunch-ok/, loop-mode, ...)
    is never read or written by a test.
  * NO INLINE jq PIPES: a fixture dict is serialized to a temp JSON file and fed
    to the hook on stdin (equivalent to `cat fixture.json | bash <hook>`), so
    shell quoting never mangles the payload.
  * PATH SHIMS: hooks that shell out to curl / gh get fake executables in a temp
    bin/ prepended to PATH, returning canned output, so fail-open behaviour can
    be exercised without touching the network.

Works under both `python3 -m pytest` and `python3 -m unittest` (tests are plain
unittest.TestCase subclasses, zero third-party imports).
"""
import json
import os
import shutil
import subprocess
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
REAL_HOOKS = os.path.join(REPO_ROOT, "hooks")
REAL_CLEANUP_LOG = os.path.join(REPO_ROOT, "cleanup-needed.log")

# Real hook scripts symlinked into every sandbox HOME. State files (loop-mode,
# cleanup-needed.log, prlaunch-ok/) are deliberately NOT symlinked — the sandbox
# creates its own so writes stay inside the temp dir.
HOOK_FILES = [
    "pr-gate.sh",
    "prlaunch-gate.sh",
    "branch-name-gate.sh",
    "linear-startwork.sh",
    "check-careful.sh",
    "careful-rm.py",
    "cleanup-sweep.py",
    "reconcile-ticket.sh",
    "check-worktree.sh",
    "check-freeze.sh",
    "check-no-edit-on-main.sh",
    "ledger-append.sh",
]

# The Linear-facing hooks are config-via-env and no-op when unconfigured, so the
# sandbox pins a full fake config (plus a non-default ticket prefix, `eng`, to
# prove nothing assumes the default).
_LINEAR_ENV = {
    "LINEAR_DEV_TEAM_ID": "00000000-0000-0000-0000-000000000000",
    "LINEAR_INPROGRESS_STATE_ID": "11111111-1111-1111-1111-111111111111",
    "LINEAR_ASSIGNEE_ID": "22222222-2222-2222-2222-222222222222",
    "LINEAR_DEPLOYED_STATE_ID": "33333333-3333-3333-3333-333333333333",
    "LINEAR_BRANCH_PREFIX": "eng",
}

# Keep git invocations (by the hooks and by test setup) isolated from the
# developer's global/system gitconfig and identity.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


class HookSandbox:
    """A temp HOME with a .claude/ skeleton symlinking the real hook scripts."""

    def __init__(self, linear_key=None):
        self.dir = tempfile.mkdtemp(prefix="hooktest-")
        self.home = os.path.join(self.dir, "home")
        self.claude = os.path.join(self.home, ".claude")
        self.hooks = os.path.join(self.claude, "hooks")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.hooks)
        os.makedirs(self.bin)
        for name in HOOK_FILES:
            src = os.path.join(REAL_HOOKS, name)
            if os.path.exists(src):
                os.symlink(src, os.path.join(self.hooks, name))
        self.cleanup_log = os.path.join(self.claude, "cleanup-needed.log")
        self.prlaunch_ok = os.path.join(self.claude, "prlaunch-ok")
        self.loop_mode = os.path.join(self.hooks, "loop-mode")
        self.key_file = None
        if linear_key is not None:
            # Exercised through the hooks' LINEAR_KEY_FILE path (a JSON file
            # holding .env.LINEAR_API_KEY); env() exports the pointer.
            self.key_file = os.path.join(self.home, "linear-key.json")
            with open(self.key_file, "w") as fh:
                json.dump({"env": {"LINEAR_API_KEY": linear_key}}, fh)

    # -- paths ------------------------------------------------------------
    def hook_path(self, name):
        return os.path.join(self.hooks, name)

    def write_marker(self, repo, branch, sha):
        """Write a LEGACY plain-sha PRlaunch marker where pr-gate.sh's back-compat
        path looks for it (no .json suffix)."""
        os.makedirs(self.prlaunch_ok, exist_ok=True)
        slug = branch.replace("/", "-")
        path = os.path.join(self.prlaunch_ok, "%s--%s" % (repo, slug))
        with open(path, "w") as fh:
            fh.write(sha)
        return path

    def ledger_path(self, repo, branch):
        """Path of the per-gate JSON ledger for repo/branch."""
        slug = branch.replace("/", "-")
        return os.path.join(self.prlaunch_ok, "%s--%s.json" % (repo, slug))

    def write_ledger(self, repo, branch, gates, scenarios=None):
        """Write a full per-gate JSON ledger.

        `gates` maps gate name -> entry dict (e.g. {"sha": HEAD, "ts": "..."} or
        {"sha": HEAD, "ts": "...", "skipped": "rate limit"}). Mirrors what
        prlaunch-gate.sh writes, so pr-gate.sh's ledger path can be exercised
        without shelling out to the record subcommand.
        """
        os.makedirs(self.prlaunch_ok, exist_ok=True)
        doc = {"repo": repo, "branch": branch, "gates": gates}
        if scenarios is not None:
            doc["scenarios"] = scenarios
        path = self.ledger_path(repo, branch)
        with open(path, "w") as fh:
            json.dump(doc, fh)
        return path

    def arm_loop_mode(self, epoch):
        """Write an expiry epoch to the loop-mode file (int seconds)."""
        with open(self.loop_mode, "w") as fh:
            fh.write(str(epoch))

    # -- shims ------------------------------------------------------------
    def add_shim(self, name, script):
        """Create a fake executable `name` in the sandbox bin/ dir."""
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write(script)
        os.chmod(path, 0o755)
        return path

    # -- env --------------------------------------------------------------
    def env(self, extra=None, shim_path=False):
        e = dict(os.environ)
        e["HOME"] = self.home
        e.update(_GIT_ENV)
        e.update(_LINEAR_ENV)
        e.pop("LINEAR_API_KEY", None)      # ambient keys must not leak into tests
        if self.key_file:
            e["LINEAR_KEY_FILE"] = self.key_file
        else:
            e.pop("LINEAR_KEY_FILE", None)
        if shim_path:
            # Prepend the shim dir but keep the real system dirs so jq/git/etc.
            # still resolve.
            e["PATH"] = self.bin + os.pathsep + e.get("PATH", "")
        if extra:
            e.update(extra)
        return e

    # -- lifecycle --------------------------------------------------------
    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def run_hook(sandbox, hook_name, payload, extra_env=None, shim_path=False):
    """Serialize `payload` to a temp fixture and pipe it into a bash hook.

    Mirrors `cat fixture.json | bash <hook>`. Returns (rc, stdout, stderr).
    """
    fixture = os.path.join(sandbox.dir, "fixture.json")
    with open(fixture, "w") as fh:
        json.dump(payload, fh)
    with open(fixture) as stdin:
        proc = subprocess.run(
            ["bash", sandbox.hook_path(hook_name)],
            stdin=stdin,
            capture_output=True,
            text=True,
            env=sandbox.env(extra_env, shim_path),
        )
    return proc.returncode, proc.stdout, proc.stderr


def run_hook_args(sandbox, hook_name, args=(), extra_env=None, cwd=None):
    """Run a bash hook with CLI arguments (no stdin payload).

    Used for prlaunch-gate.sh, which is a plain CLI (subcommands + flags) rather
    than a stdin-JSON PreToolUse hook. Returns (rc, stdout, stderr).
    """
    proc = subprocess.run(
        ["bash", sandbox.hook_path(hook_name), *args],
        capture_output=True,
        text=True,
        env=sandbox.env(extra_env),
        cwd=cwd,
    )
    return proc.returncode, proc.stdout, proc.stderr


def add_commit(path, env, msg="more"):
    """Append an empty commit to the throwaway repo at `path`; return new HEAD."""
    subprocess.run(
        ["git", "-C", path, "commit", "-q", "--allow-empty", "-m", msg],
        check=True, env=env, capture_output=True, text=True,
    )
    return subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        env=env, capture_output=True, text=True,
    ).stdout.strip()


def run_python_hook(sandbox, hook_name, args=(), stdin_text=None, extra_env=None):
    """Invoke a python hook (cleanup-sweep.py / careful-rm.py) in the sandbox."""
    proc = subprocess.run(
        ["python3", sandbox.hook_path(hook_name), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=sandbox.env(extra_env),
    )
    return proc.returncode, proc.stdout, proc.stderr


def make_git_repo(path, branch, env, commits=1):
    """Create a throwaway git repo on `branch` with `commits` empty commits.

    Returns the HEAD sha. Runs only in the given temp `path`; never the real repo.
    """
    os.makedirs(path, exist_ok=True)

    def g(*args):
        subprocess.run(
            ["git", "-C", path, *args],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )

    g("init", "-q")
    g("checkout", "-q", "-b", branch)
    for i in range(commits):
        g("commit", "-q", "--allow-empty", "-m", "c%d" % i)
    head = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return head


def decision(stdout):
    """Extract permissionDecision from hook stdout.

    Returns one of 'allow' / 'deny' / 'ask', or None when the hook stayed silent
    (empty output, or a bare `{}` allow with no explicit decision).
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    hso = obj.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision")


def load_json(stdout):
    """Parse hook stdout as JSON (or None if empty/unparseable)."""
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# A curl shim used by hooks that call the Linear GraphQL API. Behaviour is
# controlled entirely by env vars so a single shim serves every scenario:
#   FAKE_MODE=error      -> emit nothing (simulates an API failure / timeout)
#   FAKE_CANONICAL=<str> -> branchName returned for the branch-name-gate query
#   otherwise            -> issue node (for reconcile) / issueUpdate success
CURL_SHIM = r"""#!/bin/bash
args="$*"
if [ "${FAKE_MODE:-}" = "error" ]; then
  echo ''
  exit 0
fi
case "$args" in
  *issueUpdate*)
    echo '{"data":{"issueUpdate":{"success":true}}}'
    ;;
  *)
    if [ -n "${FAKE_CANONICAL:-}" ]; then
      printf '{"data":{"issues":{"nodes":[{"identifier":"ENG-%s","branchName":"%s"}]}}}' "${FAKE_NUM:-0}" "$FAKE_CANONICAL"
    elif [ -n "${FAKE_ISSUE_NODE:-}" ]; then
      printf '{"data":{"issues":{"nodes":[%s]}}}' "$FAKE_ISSUE_NODE"
    else
      echo '{"data":{"issues":{"nodes":[]}}}'
    fi
    ;;
esac
"""

# gh shim: echoes the PR state named in FAKE_PR_STATE (default MERGED).
GH_SHIM = r"""#!/bin/bash
echo "${FAKE_PR_STATE:-MERGED}"
"""
