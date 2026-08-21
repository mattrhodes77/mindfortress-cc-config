"""Static / integrity checks over the repo.

  * shellcheck over hooks/*.sh at --severity=error (SKIPPED if shellcheck absent)
  * python3 -m compileall hooks/
  * settings.json is valid JSON (via jq if present, else json.load) — SKIPPED
    when the repo carries no settings.json (this public repo documents the hook
    wiring in the README instead)
  * every hook script referenced in settings.json exists in hooks/ and (for .sh)
    is executable — same skip rule
  * every commands/*.md and skills/*/SKILL.md has parseable YAML frontmatter with
    the keys its format requires (commands: description; skills: name+description)
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import REPO_ROOT

HOOKS_DIR = os.path.join(REPO_ROOT, "hooks")
SETTINGS = os.path.join(REPO_ROOT, "settings.json")


def _parse_frontmatter(text):
    """Zero-dependency frontmatter parse.

    Returns the set of top-level keys in the leading `---`...`---` block, or
    raises ValueError if there is no well-formed frontmatter block.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening '---'")
    body = []
    closed = False
    for ln in lines[1:]:
        if ln.strip() == "---":
            closed = True
            break
        body.append(ln)
    if not closed:
        raise ValueError("missing closing '---'")
    keys = set()
    for ln in body:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        # Top-level key: no leading whitespace, matches `key:`
        m = re.match(r"^([A-Za-z0-9_-]+):", ln)
        if m:
            keys.add(m.group(1))
    return keys


class StaticChecksTest(unittest.TestCase):
    def test_shellcheck_errors(self):
        if not shutil.which("shellcheck"):
            self.skipTest("shellcheck not installed — SKIPPED")
        scripts = sorted(glob.glob(os.path.join(HOOKS_DIR, "*.sh")))
        self.assertTrue(scripts, "no hook shell scripts found")
        proc = subprocess.run(
            ["shellcheck", "--severity=error", *scripts],
            capture_output=True, text=True,
        )
        self.assertEqual(
            proc.returncode, 0,
            "shellcheck found error-level issues:\n%s%s" % (proc.stdout, proc.stderr),
        )

    def test_ruff_clean(self):
        """`ruff check` over the scope ruff.toml declares.

        Same shape as test_shellcheck_errors above: SKIP when the tool is
        absent, FAIL when it is present and unhappy. CI ran `bash
        run-tests.sh` and run-tests.sh ran pytest — so a duplicate dict key,
        a dead assignment or an undefined name could land in the
        merge-deciding classifier and no gate would notice (three such
        findings were live in `skills/babysit/babysit_classify.py` when this
        test was written).

        SCOPE LIVES IN ruff.toml, NOT HERE — an explicit include list, not a
        repo-wide run with an ignore list bolted on (an ignore list added to
        go green just reproduces the ungated problem one level up).
        Asserting the config exists first matters: without it a deleted
        ruff.toml would leave `ruff check` running against its own defaults
        over the whole repo, and the failure would read as "someone's
        untouched file is dirty" rather than "the lint contract was removed".
        """
        ruff = shutil.which("ruff")
        if not ruff:
            # Locally the suite stays zero-install (skip); in CI a missing
            # ruff means the install step was deleted or its bin dir fell off
            # PATH, and a skip there is a silently-decorative lint gate.
            if os.environ.get("CI"):
                self.fail("ruff is not on PATH in CI — the lint gate must "
                          "never skip there (check the install step)")
            self.skipTest("ruff not installed — SKIPPED")
        config = os.path.join(REPO_ROOT, "ruff.toml")
        self.assertTrue(os.path.isfile(config),
                        "ruff.toml is missing — it is the lint contract this "
                        "test enforces, not an optional convenience")
        # FIRST: prove the gate is pointed at something. `ruff check` exits 0
        # with only a stderr warning when its include list matches NO files —
        # so an emptied include, a glob typo, or a cwd change would leave
        # this test reporting green having linted zero files. A gate that
        # cannot tell "clean" from "looked at nothing" is the exact failure
        # this whole file is about, one level up.
        listing = subprocess.run(
            [ruff, "check", "--no-cache", "--config", config, "--show-files",
             REPO_ROOT],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        # A startup error (required-version mismatch, config parse failure)
        # exits non-zero with EMPTY stdout and the real cause only on stderr
        # — without this assert it would be misreported below as a broken
        # include list, sending the reader to debug intact globs.
        self.assertEqual(
            listing.returncode, 0,
            "ruff could not run at all (not an include-list problem):\n%s"
            % (listing.stderr or listing.stdout))
        files = [ln for ln in listing.stdout.splitlines() if ln.strip()]
        # PER-ROOT, not an aggregate count: a single total lets one include
        # entry vanish behind growth in another — deleting "hooks/**" while
        # tests/ grows by two files would pass an aggregate floor with the
        # safety-spine hooks silently unlinted. Every root declared in
        # ruff.toml must contribute at least one linted file.
        roots = re.findall(r'"([^"*]+)/\*\*/\*\.py"', open(config).read())
        self.assertTrue(roots, "no include roots parsed from ruff.toml")
        for root in roots:
            self.assertTrue(
                any(("%s%s" % (root, os.sep)) in f for f in files),
                "include root %r contributes no linted files — the entry is "
                "broken or the directory was emptied" % root)
        # Name one file explicitly: the module the gate exists for.
        self.assertTrue(
            any(f.endswith("skills/babysit/babysit_classify.py") for f in files),
            "babysit_classify.py is no longer in the linted set — that is "
            "the merge-deciding module and the reason this gate exists")

        # --no-cache: the suite must not write a .ruff_cache into the
        # checkout it is gating, and a dozen-odd files are not worth caching.
        proc = subprocess.run(
            [ruff, "check", "--no-cache", "--config", config,
             "--output-format", "concise", REPO_ROOT],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        self.assertEqual(
            proc.returncode, 0,
            "ruff found lint errors:\n%s%s" % (proc.stdout, proc.stderr),
        )

    def test_run_tests_sh_covers_every_test_directory(self):
        """A test file that run-tests.sh never collects is a gate that looks
        installed and never fires. Every directory in the repo containing
        test_*.py files must be one run-tests.sh actually runs (it runs
        `tests/`), or be explicitly opted out with a
        `# CI-EXCLUDED-SUITE: <dir> — <reason>` line in run-tests.sh — so
        excluding a suite is a visible, reasoned diff instead of a silent
        hole."""
        with open(os.path.join(REPO_ROOT, "run-tests.sh")) as fh:
            runner = fh.read()
        # Parse what the runner ACTUALLY runs — a hardcoded {"tests"} here
        # would keep passing after run-tests.sh was narrowed to a single
        # file or a -k filter, which is this guard's own failure mode.
        covered = {m.rstrip("/") for m in
                   re.findall(r"pytest\s+(?:-\S+\s+)*([A-Za-z0-9_./-]+)/", runner)}
        covered |= set(re.findall(r"discover\s+-s\s+([A-Za-z0-9_./-]+)", runner))
        self.assertIn("tests", covered,
                      "run-tests.sh no longer collects the tests/ suite "
                      "(parsed coverage: %s)" % sorted(covered))
        excluded = set(re.findall(r"#\s*CI-EXCLUDED-SUITE:\s*(\S+)", runner))
        offenders = set()
        for path in glob.glob(os.path.join(REPO_ROOT, "**", "test_*.py"),
                              recursive=True):
            rel = os.path.relpath(os.path.dirname(path), REPO_ROOT)
            # Tooling/vendor trees are not this repo's suites — a local
            # virtualenv or node_modules would otherwise trip the guard on
            # third-party test files (CR CLI finding).
            ignored_dirs = {
                ".git", "__pycache__", ".venv", "venv", ".tox", ".nox",
                "node_modules", "build", "dist",
            }
            if any(part in ignored_dirs for part in rel.split(os.sep)):
                continue
            top = rel.split(os.sep)[0]
            # `rel not in excluded` only: exclusion is PER DIRECTORY, exactly
            # as the docstring says — a single top-level directive must not
            # blanket-exempt every current and future suite under that tree.
            if top not in covered and rel not in excluded:
                offenders.add(rel)
        self.assertEqual(
            offenders, set(),
            "test directories run-tests.sh never runs (add them to the "
            "runner, or opt out with a '# CI-EXCLUDED-SUITE:' line): %s"
            % sorted(offenders))

    def test_compileall_hooks(self):
        proc = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", HOOKS_DIR],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_settings_is_valid_json(self):
        if not os.path.exists(SETTINGS):
            self.skipTest("no settings.json in this repo — hook wiring lives in the README")
        if shutil.which("jq"):
            proc = subprocess.run(["jq", "empty", SETTINGS], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, "jq rejected settings.json:\n" + proc.stderr)
        with open(SETTINGS) as fh:
            json.load(fh)  # also parse in-process

    def test_referenced_hook_scripts_exist_and_executable(self):
        if not os.path.exists(SETTINGS):
            self.skipTest("no settings.json in this repo — hook wiring lives in the README")
        with open(SETTINGS) as fh:
            settings = json.load(fh)
        commands = []

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "command" and isinstance(v, str):
                        commands.append(v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(settings.get("hooks", {}))
        # Match hook scripts referenced under a .claude/hooks/ path. Validate by
        # basename against THIS repo's hooks/ dir so the check is machine-portable
        # (settings.json hardcodes an absolute /Users/... path).
        referenced = set()
        for cmd in commands:
            for m in re.finditer(r"/hooks/([A-Za-z0-9._-]+\.(?:sh|py))", cmd):
                referenced.add(m.group(1))
        self.assertIn("pr-gate.sh", referenced, "sanity: expected pr-gate.sh to be referenced")
        for name in sorted(referenced):
            path = os.path.join(HOOKS_DIR, name)
            self.assertTrue(os.path.isfile(path), "referenced hook missing: %s" % name)
            if name.endswith(".sh"):
                self.assertTrue(os.access(path, os.X_OK), "referenced hook not executable: %s" % name)

    def test_command_frontmatter(self):
        files = sorted(glob.glob(os.path.join(REPO_ROOT, "commands", "*.md")))
        self.assertTrue(files, "no command files found")
        for path in files:
            with open(path) as fh:
                keys = _parse_frontmatter(fh.read())
            self.assertIn("description", keys, "%s: frontmatter needs description" % os.path.basename(path))

    def test_skill_frontmatter(self):
        files = sorted(glob.glob(os.path.join(REPO_ROOT, "skills", "*", "SKILL.md")))
        self.assertTrue(files, "no SKILL.md files found")
        for path in files:
            with open(path) as fh:
                keys = _parse_frontmatter(fh.read())
            rel = os.path.relpath(path, REPO_ROOT)
            self.assertIn("name", keys, "%s: frontmatter needs name" % rel)
            self.assertIn("description", keys, "%s: frontmatter needs description" % rel)


if __name__ == "__main__":
    unittest.main()
