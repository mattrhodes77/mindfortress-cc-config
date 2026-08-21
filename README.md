# Reeve CC Config

The public [Claude Code](https://claude.com/claude-code) config we run building [Reeve](https://meetreeve.com). It covers the full lifecycle of a unit of work:

```
brainstorm  →  build  →  ship (PRlaunch)  →  wrap up
 design          mf-frontend-design   three gates + hook      tracker/memory/report
 before code     for frontend work    before any PR exists
```

The centerpiece is **PRlaunch** — a pre-PR quality pipeline: three local gates (a deep code review, a secondary automated review, and a live outcome eval from the user's seat) run against your working tree *before* a PR is ever opened, and a hook mechanically blocks `gh pr create` until the exact bytes you're shipping have passed every gate — each gate stamped, per-HEAD, into a tamper-evident evidence ledger.

Built and battle-tested running a multi-repo production shop with Claude Code doing most of the shipping. Every rule in here exists because its absence caused a real incident.

## History

Built over ~6 months of daily production use running [Reeve](https://meetreeve.com) — a small startup (4 devs) where Claude Code does most of the shipping across a multi-repo platform. Nothing here was designed up front: every gate, hook, and classifier was added after its absence caused a real incident, then kept only if it paid rent.

The most recent overhaul is itself a story about models: with Claude Fable 5 access on the Max plan sunsetting, we pointed Fable at this config for one final pass — using the strongest model to harden the whole pipeline so a smaller one could drive it at the same quality bar. That wave produced the evidence-required review process (v6.9), the per-gate ledger a hook enforces mechanically, deterministic tested classifiers replacing prompt-time judgment, the six-section worker-brief contract, and `skillify` — repo skill libraries that hand a weaker model senior-engineer operating knowledge. The thesis of the whole repo: **put the judgment in the process, not the model.**

A follow-on wave closed what that pass left half-wired. A canonical **work taxonomy** (see `Cross-cutting: work taxonomy` below) gives `PRlaunch`, `wrapup`, `bulldozer`, and `assign` one shared PROJECT/EPIC/ticket vocabulary and a single **3-bucket follow-up-filing contract**, so every follow-up lands in the shipping feature's own epic, a standing "Bulldozer 1-offs" epic, or a fresh parked epic — never parentless, and never restated per-skill. `bulldozer` reads that standing epic as a second intake source (exempt from its normal recency filter); `assign`'s backlog-claim ranking excludes its children so a human claim can't race the heartbeat's drain. `execute` learned to size an epic to a one-session chunk (~5 tickets) and propose a re-chunk instead of grinding an oversized one in a single pass. `briefs` named the **classifier-fleet pipeline** it had already been running informally — deterministic pre-pass → read-only classifier fleet → single-writer apply → verify counts → memory-write — citing `flushdeployed` as its worked, end-to-end exemplar. And a new skill, `linear-gardener`, applies that same pipeline shape as a *standing*, config-driven board-hygiene pass: inventory the board, promote oversized epics to projects, re-chunk the rest into session epics, sweep stray tickets with the classifier fleet, and apply the fixes through one verified writer — every workspace specific (team, project, label cohort, thresholds) lives in a gitignored config file, never in the skill itself.

## What's in the box

```
skills/execute/                  # 0. ORCHESTRATE — one command, whole lifecycle: read→validate→scope→plan→build→test→ship
skills/orchestrate/              #    …or point it at a PROJECT and let it pick the next group itself
skills/epicbuilder/              #    …or, if that project's backlog is a loose pile, structure it into epics first
skills/brainstorming/            # 1. BRAINSTORM — design before code           (obra/superpowers, MIT)
skills/writing-plans/            #    …then write the implementation plan        (obra/superpowers, MIT)
skills/executing-plans/          #    …then execute it with review checkpoints   (obra/superpowers, MIT)
skills/mf-frontend-design/       # 2. BUILD — the frontend-design skill every FE surface goes through
skills/test-driven-development/  #    build-stage discipline                     (obra/superpowers, MIT)
skills/systematic-debugging/     #    when something breaks                      (obra/superpowers, MIT)
commands/PRlaunch.md             # 3. SHIP — the pipeline: 7 phases, gate order, disposition rules
commands/deep-review.md          #    Deep Review Process v6.9 — the methodology gate 1 runs
commands/wrapup.md               # 4. WRAP UP — tracker sync, GitHub sync, branch hygiene, memory, cleanup, report
commands/babysit-prs.md          # 5. AFTER — hourly self-arming sweep of open PRs until reviews drain
skills/babysit/                  #    the deterministic, tested classifier/planner that sweep executes
commands/bulldozer.md            #    hourly self-arming heartbeat that DRAINS easy backlog tickets, one fresh subagent per ticket
commands/cleanup.md              #    resolve cleanup debt — deletes the careful hook deferred during loops
skills/flushdeployed/            # 6. AUDIT — is each "Deployed" ticket REALLY live? validate against main + the deploy box
skills/assign/                   # 7. STAFF — roster-driven ticket discovery + assignment for a team (local SQLite + roster)
skills/linear-gardener/          # standing board-hygiene pass — inventory/promote/re-chunk/sweep/apply, config-driven
hooks/pr-gate.sh                 # enforcement: blocks `gh pr create` unless every gate is recorded at the current HEAD
hooks/prlaunch-gate.sh           # the per-gate evidence ledger CLI the phases stamp and pr-gate verifies
hooks/check-careful.sh           # guardrail: plain-English prompt on destructive bash; silent on routine cleanup (loop-mode aware)
hooks/careful-rm.py              # parser behind check-careful: classifies rm -r targets (quote/comment/newline aware)
hooks/cleanup-sweep.py           # helper: read/resolve the deferred-delete cleanup queue (used by cleanup/wrapup/PRlaunch/babysit)
hooks/check-freeze.sh            # guardrail: hard-block edits outside a declared directory boundary
hooks/check-worktree.sh          # guardrail: deny `git commit` in a primary clone — work in worktrees
hooks/check-no-edit-on-main.sh   # guardrail: deny editing a primary clone on its default branch — work in worktrees
hooks/loop-mode-arm.sh           # helper: time-box check-careful's loop-mode so unattended /loop runs don't wedge
hooks/reconcile-ticket.sh        # advance a ticket to Deployed only when EVERY linked PR is merged (multi-PR race fix)
hooks/ledger-append.sh           # append-one-validated-JSON-line automation ledger (fail-loud validator)
hooks/model-preamble.sh          # SessionStart: inject a strict process-first preamble for weaker-than-frontier models
skills/briefs/                   # the six-section worker-brief contract every orchestrator's subagent prompt follows
skills/skillify/                 # turn a repo's tribal knowledge into a project skill library (discover→map→author→review)
tests/ + run-tests.sh            # the safety-hook regression suite (isolated temp-HOME sandboxes; CI-wired)
ruff.toml                        # the lint contract CI enforces (explicit-include ratchet, version-pinned)
launchd/                         # headless scheduled skills — launchd fires `claude -p` on a schedule, no open session needed
skills/…                         # + the supporting superpowers set: using-superpowers (skill dispatch),
                                 #   using-git-worktrees, verification-before-completion,
                                 #   subagent-driven-development, finishing-a-development-branch,
                                 #   requesting-code-review, receiving-code-review
```

---

## 0. Drive it end to end — /execute

[`skills/execute/SKILL.md`](skills/execute/SKILL.md) is the single front door that runs the whole lifecycle below as one disciplined pass: **read → validate against prod → scope → plan → build → test → `/PRlaunch`**. Two things make it more than a macro. First, a **validate-against-prod gate** before any code is written: the ticket's premise is a claim to verify, not trust — it checks whether the capability already exists and whether the described state is real, and **halts** if production contradicts the ticket (building the wrong thing is the most expensive bug there is). Second, an **ask-only-on-a-true-fork contract**: it runs autonomously through to the PR when the path is clear, and interrupts only for a genuine decision — one with materially different outcomes that can't be derived from the ticket, the code, or a sensible default — so it neither over-asks on trivia nor silently guesses on the choices that actually matter. It builds in an isolated worktree and hands off to PRlaunch for the quality gates; it never re-runs them itself. Everything below is what `/execute` orchestrates — and each piece still stands alone.

## 0.25 Pick the group yourself — orchestrate

[`skills/orchestrate/SKILL.md`](skills/orchestrate/SKILL.md) is what runs when you don't have a work-list yet — you only know the *project*. It's `/execute`'s batch pipeline with two deltas: the orchestrator itself picks the ~3–8 unit group (collision-checked against in-flight branches/PRs so it never crosses another session's lane), and every build unconditionally runs in a fresh cheap-model subagent per unit, briefed per the `briefs` contract — never inline, regardless of how small the fix looks. It validates every unit's premise before building any of them (expect real kills — a contradicted premise here is the win, not a failure), then validates every worker's "shipped" claim before accepting it, because a green report you didn't check is a hallucination vector.

## 0.3 Structure the backlog first — epicbuilder

[`skills/epicbuilder/SKILL.md`](skills/epicbuilder/SKILL.md) (`/epicbuilder <fuzzy project(s)>`) is `/orchestrate`'s front half: a project can't be orchestrated, assigned, or bulldozed off a flat Todo/Backlog pile — those consumers all eat epics. It inventories a project's loose tickets, fans classification out to a read-only cheap-model fleet (existing-epic-first, so a concern that's already an epic gets tickets parented in rather than duplicated), proposes ~3–8-ticket session epics chained by real dependency order, and parks true self-contained 1-offs under the project's standing **Bulldozer 1-offs** epic so `/bulldozer` has a queue. Cross-board strays get flagged, never moved — that's a separate hygiene pass's job. Single writer applies the announced map, then a children-count verification pass before it's trusted.

## 0.5 Brief the workers — briefs (and build repo knowledge — skillify)

Every orchestrator here (bulldozer, babysit-prs, deep-review's finder/refuter agents, flushdeployed's validators) fans work out to fresh subagents, and worker quality is downstream of brief quality. [`skills/briefs/SKILL.md`](skills/briefs/SKILL.md) is the six-section contract every worker prompt must carry — CONTEXT / TASK / CONSTRAINTS / RETURN CONTRACT / VERIFICATION REQUIREMENT / STOP CONDITIONS — converting the judgment a strong model applies implicitly into fill-in slots a weaker worker can't silently skip, with a worked example. The load-bearing sections are the last three: a schema'd return the orchestrator can parse, evidence-before-assertion, and named bail-out conditions so a stuck worker reports instead of thrashing.

[`skills/skillify/SKILL.md`](skills/skillify/SKILL.md) (`/skillify <repo>`) points the same discipline at onboarding: it mines ONE repo's ground truth (code, tests, CI, deploy scripts, memory, tracker history, prod) into a `<repo>/.claude/skills/` library — discover→map→author→review with an adversarial refute pass, per-skill required sections from [`taxonomy.md`](skills/skillify/taxonomy.md), and an acceptance test where a fresh zero-context subagent must operate the repo from the library alone. Its hard rule: a README rewritten as skills is a failure — skills carry exact invocations with expected observations, traps, decision gates, and settled failures.

## 1. Brainstorm — design before code, then plan, then execute

[`skills/brainstorming/SKILL.md`](skills/brainstorming/SKILL.md) governs how work *enters* a session. It runs before any creative work: explore context, clarify intent one question at a time, propose 2–3 approaches with trade-offs, present a design, and **get approval before a single line of code** — with a hard gate against "this is too simple to need a design" (simple projects are exactly where unexamined assumptions burn the most work). Designs that go through this gate arrive at PRlaunch with their scope already agreed, which is most of why the review gates come back clean.

Brainstorming doesn't stand alone — it hands off to [`writing-plans`](skills/writing-plans/SKILL.md) (turn the approved design into a step-by-step implementation plan) and [`executing-plans`](skills/executing-plans/SKILL.md) (work the plan with review checkpoints). Every repo we work in has a `docs/superpowers/` directory of dated specs and plans this loop produced. The supporting cast is vendored too: [`using-superpowers`](skills/using-superpowers/SKILL.md) (the skill-dispatch discipline — injected at session start so skills actually fire), [`using-git-worktrees`](skills/using-git-worktrees/SKILL.md) (why our worktree-per-ticket pattern exists — mechanically enforced by [`hooks/check-worktree.sh`](hooks/check-worktree.sh)), [`subagent-driven-development`](skills/subagent-driven-development/SKILL.md), [`finishing-a-development-branch`](skills/finishing-a-development-branch/SKILL.md), and the [`requesting-`](skills/requesting-code-review/SKILL.md)/[`receiving-code-review`](skills/receiving-code-review/SKILL.md) pair.

All of it is vendored verbatim from Jesse Vincent's [superpowers](https://github.com/obra/superpowers) plugin (MIT, license included in each skill directory). **We run the full plugin in production, daily** — this is the actively-used subset, snapshotted so this repo is complete on its own. For the latest versions and the rest of the suite, install the live plugin; upstream keeps evolving and these copies don't auto-update.

## 2. Build — mf-frontend-design

[`skills/mf-frontend-design/SKILL.md`](skills/mf-frontend-design/SKILL.md) is how frontend work actually gets built here — every UI surface goes through it, not as an optional flourish but as the build-stage standard. Tunable VARIANCE/MOTION/DENSITY dials, metric-based typography, color-calibration bans, RSC architecture rules, performance guardrails, and **screenshot-driven verification** baked into the build loop itself. Zero AI slop is the bar.

It's also why PRlaunch's gate 3 works: the skill builds UI to a standard *and verifies it with screenshots as it goes*, so by the time the outcome eval grades what the user receives, it's confirming a discipline that ran during the build — not discovering taste problems for the first time. Merged from Anthropic's `frontend-design` skill and Leonxlnx's `taste-skill` (MIT), with every rule from both preserved.

Build-stage discipline that isn't frontend-specific is superpowers territory: [`test-driven-development`](skills/test-driven-development/SKILL.md) for any feature or bugfix, [`systematic-debugging`](skills/systematic-debugging/SKILL.md) when something breaks (root-cause before fixes, with its reference docs on tracing and defense-in-depth), and [`verification-before-completion`](skills/verification-before-completion/SKILL.md) — evidence before any "it works" claim, which is the same ethos PRlaunch's gates enforce at ship time.

## 3. Ship — PRlaunch

### Why three gates

Each gate catches a bug class the others can't see:

| Gate | Grades | Catches |
|------|--------|---------|
| **1. Deep review** ([deep-review.md](commands/deep-review.md), v6.9) | the **diff** | correctness, security, architecture — "will this overwrite the DB before the user accepts?" |
| **2. Secondary reviewer** (CodeRabbit CLI or similar) | the **repo** | style, nits, patterns |
| **3. Outcome eval** | the **running product, from the user's seat** | the "basic stuff that always gets missed": raw `**markdown**` shown to users, an LLM that asks for context it already has, a spinner that never resolves, a layout broken at the real viewport |

Gate 3 is the one most teams don't have. The failure it targets isn't *not testing* — it's **testing and grading the wrong signal**. It's dangerously easy to watch `POST … 200`, see "a bubble appeared", and write ✅ while the actual words on screen are wrong and the pixels show literal asterisks. The eval forces you to write PASS criteria *before* running anything, phrased as *what the user receives*, then quote the real output for every scenario. Transport (status codes, payloads, "element exists") is necessary, never sufficient.

### The re-gate rule

**Any code change in a later gate invalidates the earlier gates.** A gate's green is only valid for the exact code it ran against. Fixed something during the eval? That fix hasn't been deep-reviewed, linted, or re-tested. The pipeline loops until a full pass over the final committed tree produces zero new changes — and the hook enforces it: each of the four gates (deep review, secondary review, outcome eval, tests) is stamped into a per-branch JSON ledger with the HEAD sha it ran against ([`hooks/prlaunch-gate.sh`](hooks/prlaunch-gate.sh)), and [`hooks/pr-gate.sh`](hooks/pr-gate.sh) blocks `gh pr create` unless all four are recorded at the *current* HEAD. Any commit after a gate ran stales that gate's entry automatically. The outcome eval won't even record without a pre-registered scenarios file — writing PASS criteria *before* running anything is enforced, not aspirational.

### Deep Review v6.9 highlights

A 10-step review process with empirical validation at its core — *evidence over opinion: a finding without proof is not a finding*. Notable machinery:

- **Mandatory branch alignment** before any analysis (a review of stale code is a review of something that won't exist after merge)
- **Parallel security + quality agents** with calibrated detection prompts: duplicate logic, silent failure paths, unregistered integration points, and (new in v6.8) **structural regressions** — spaghetti-conditional growth, thin wrappers, type-boundary muddying, layer leaks, non-atomic updates
- **Finding origin classification** (IN-SCOPE / ADJACENT / OUT-OF-SCOPE) so PRs are never blocked for pre-existing debt — and pre-existing debt is never silently dropped either
- **Severity recalibration** as a standing function — review agents reliably inflate code-quality nits to HIGH; structural findings default to MEDIUM, never CRITICAL, and a missed simplification opportunity is a suggestion, never a blocker
- **The code-judo question** (v6.8, adapted from Cursor's [thermo-nuclear-code-quality-review](https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md) skill): one explicit pass per review asking whether a reframing would *delete* complexity rather than rearrange it
- **Empirical validation**: run the app, curl the endpoints, drive the UI, prove security fixes with before/after against a running instance
- **Evidence-required findings** (v6.9): every finding carries `Evidence: <command run> → <output excerpt>` from an *executed* check — no evidence caps the finding at MEDIUM, and severity is assigned from a fixed decision table, not agent labels
- **Adversarial refute pass** (v6.9): every CRITICAL/HIGH candidate gets independent refuter subagents briefed to *disprove* it before it can publish; refuted findings downgrade to INFO with the refutation recorded
- **Evidence-based re-review** (v6.9): a fix is `RESOLVED` only when the original finding's evidence command re-runs clean on the new code — a diff-only check is `PARTIALLY RESOLVED (unconfirmed)`

## 4. Wrap up

[`commands/wrapup.md`](commands/wrapup.md) is the session-end discipline PRlaunch's phase 6 runs, usable standalone as `/wrapup`: sync every touched ticket to reality, put every PR in the right state, leave zero dirty/unpushed branches, persist what future sessions need to know, commit your config repo, and report it all in one scannable message. The core rule is the same as PRlaunch's: **every finding and follow-up gets a disposition** — filed, fixed, or waived with a reason — never silently dropped.

## 5. After the PR — babysit-prs

Opening a PR isn't the end: automated reviewers post findings on their own schedule, rate limits stall queues, and stacked PRs get skipped by cloud bots entirely. [`commands/babysit-prs.md`](commands/babysit-prs.md) is a **self-arming hourly sweep** of every open PR you've authored across the org. As of v3, everything decidable is decided by a deterministic, regression-tested script — [`skills/babysit/babysit_classify.py`](skills/babysit/babysit_classify.py) classifies every PR, computes the merge-ready "greens" tiers, plans the bump/fix/rebase/CI-triage/CLI actions under hard caps, and owns the stall/drain decision — while the skill executes only the judgment work: applying mechanical reviewer fixes (with a hard rule that behavioral changes must pass the *actual test suite*, not just a syntax check — learned from a one-line fix that parsed clean and shipped a red suite), resolving rebase conflicts, and writing NEEDS-HUMAN prose. Every hard rule in the script patches a real past model mistake, and each is pinned by a named regression test. It re-triggers stalled reviews within rate-limit budgets and runs the reviewer's CLI in the background for stacked PRs the cloud bot refuses — but only when you're not at the keyboard, so it never burns your quota.

The interesting machinery is the **convergence rule** (also script-owned): every sweep fingerprints the queue (PR × state × latest-bot-activity, hashed) and the loop stays armed *as long as the queue is moving* — then auto-stops in exactly two cases: drained (every PR is CLEAN or NEEDS-HUMAN) or stalled (12 frozen sweeps ≈ the bot is down). Reviewer credit exhaustion is never a stop condition — consuming the hourly refill is the job. No runaway polling, no babysitting the babysitter. It never merges; the report ends with a clean-list and a "likely merge" call-out driven by a per-repo policy table you define for your team (ours is redacted — write your own).

## 6. Audit shipped work — flushdeployed

[`skills/flushdeployed/SKILL.md`](skills/flushdeployed/SKILL.md) (`/flushdeployed <project>`) treats a tracker's "Deployed" column as **a claim to verify, not trust** — the same stance `/execute` opens with, applied to the other end of the pipeline. Trackers auto-advance a ticket to Deployed on PR merge, but merged ≠ live: a service that ships manually can lag its merge by hours, tickets get marked Deployed when only *part* of their scope shipped, and some get reverted. It fans out one read-only validator per ticket — confirm the merge is in `main`, confirm the actual change is present (not just a merge commit, which is what catches reverts and scope-gaps), and for manually-deployed services confirm the merge is an ancestor of the live box's HEAD — then moves the truly-live to Done with an evidence note, splits partials into a Done plus a fresh Todo for the unshipped remainder, and bounces the never-shipped back to Todo. An audit you can re-run, that leaves the tracker matching reality.

---

## 7. Staff the backlog — assign

[`skills/assign/SKILL.md`](skills/assign/SKILL.md) (`/assign <names…>`) turns "find more tickets for Alice and Bob" into verified, assigned tracker tickets. It's roster-driven: each engineer's repos, active branches, projects, and themes live in a `roster.json`, and their live ticket queue is cached locally (SQLite) so you always staff from their *current* workstream, not a guess. It mines each person's repos for well-constrained candidates, dedups against what they already own, ranks the roster by repo + project + label + theme overlap so each candidate lands in the right lane, and files the assigned tickets — up to ~15 swappable people. Ships with a sanitized [`roster.example.json`](skills/assign/roster.example.json): copy it to `roster.json` and edit. The real roster (teammate emails + IDs) and the cache are gitignored, so team data never gets committed.

---

## Cross-cutting: safety hooks

Four deterministic guardrails. The first two are adapted from [garrytan/gstack](https://github.com/garrytan/gstack) (with a JSON-extraction bugfix — the originals' grep-based parsing missed commands containing escaped quotes, e.g. `psql -c "DROP TABLE …"` — and output modernized to the current `hookSpecificOutput` hook schema); the last two are ours:

- **`check-careful.sh`** (+ **`careful-rm.py`**) — gates rare-but-catastrophic bash, and works hard to ask in plain English *only* when it matters. For `rm -r`, every delete target is classified by `careful-rm.py` — a real quote/comment/newline/redirection-aware parser, because a bash word-loop mis-reads all four (it parsed `rm` inside a quoted argument and `#` comments as real deletes, and leaked tokens across newlines — we hit exactly this). If **every** target is routine/regenerable — virtualenvs (incl. suffixed `.venv-*`), build/cache dirs (`node_modules`, `.next`, `dist`, `__pycache__`, `.pytest_cache`, …), local test DBs (`*test*.db`/`-shm`/`-wal`), `/tmp` paths, logs — the command is **allowed silently**. Otherwise the prompt lists each item with ✓ (routine) or ⚠ (please check) and a plain label, so a human can answer at a glance instead of decoding a regex verdict like *"recursive delete of a non-temp, non-build path."* Other gated commands stay narrow with plain-English warnings: true `git push --force` (`--force-with-lease` passes), SQL `DROP`/`TRUNCATE` *via a database client*, `kubectl delete`, `docker rm -f`/`system prune`. The principle: a guardrail that prompts on routine commands — or asks a question you can't answer — trains you to click through, which is worse than no guardrail. Gate only what's rare *and* catastrophic, and explain it.

  **A delete never blocks a goal/loop.** A confirmation prompt that no one is there to answer wedges the whole run — and because a `/goal` is an undetectable Stop hook (no env/stdin/file signal a hook can read), the hook *can't* tell whether a loop is driving it. So the rule is unconditional: it **never prompts on a delete in any context**. Recognized-safe deletes (virtualenvs, caches, test DBs, `/tmp`, logs) run silently; an unrecognized ⚠ delete is **deferred** — *not run*, appended as JSON to `~/.claude/cleanup-needed.log`, and the run continues. Deletes are uniquely safe to defer (you can always delete later, never un-delete), so this accrues reviewable cleanup debt instead of blocking *or* silently destroying. The debt is resolved when a human is back: [`hooks/cleanup-sweep.py`](hooks/cleanup-sweep.py) reads the queue and [`commands/cleanup.md`](commands/cleanup.md) (`/cleanup`) re-runs each deferred delete with you approving per ⚠ item; `wrapup` and `PRlaunch` run the same sweep at their end, and `babysit-prs` (unattended) surfaces the pending count.

  **Loop-mode** (`~/.claude/hooks/loop-mode`) now only governs the *rare non-delete* gated commands (true force-push, SQL `DROP`, `kubectl delete`, `docker prune`) — the only ones that still prompt interactively. When armed they auto-proceed instead of asking. Arm with [`hooks/loop-mode-arm.sh [minutes]`](hooks/loop-mode-arm.sh) (self-expiring epoch, default 90 min; re-armed each iteration, disarms after the window so a leftover never poisons a later interactive session); an empty file (`touch`) arms indefinitely until you `rm` it. `babysit-prs` arms it in Step 0c (skipped on `no-loop`).
- **`check-freeze.sh`** — dormant until you write a directory path to `~/.claude/hooks/freeze-dir.txt`, then **hard-blocks** any Edit/Write outside that boundary. It turns "stay in this repo" from an instruction the agent must remember into a rule the harness enforces. `rm ~/.claude/hooks/freeze-dir.txt` to unfreeze.
- **`check-worktree.sh`** — **denies `git commit` in a primary clone** (`.git` is a directory) and allows it in linked worktrees (`.git` is a gitfile). This is the mechanical enforcement of the worktree-per-ticket pattern that [`using-git-worktrees`](skills/using-git-worktrees/SKILL.md) teaches, and it exists for the same reason every rule here does: humans and agents work the same repos in parallel, and an agent commit in a shared primary clone can be silently clobbered or buried in reflog by a human rebase/amend/branch-rename. We lost commits this way before making it a rule — and the rule still depended on the model remembering it, so now it's a hook. It resolves the repo the commit actually targets (`git -C <path>` first, then the last `cd` in the command, then the session cwd), so compound commands and subagent cwd-drift don't slip past it. Scratch clones under `/tmp` are exempt, and you can exempt a repo deliberately: `echo /path/to/repo >> ~/.claude/hooks/worktree-exempt.txt`. Reading and exploring in a primary clone stays unrestricted — only mutation needs isolation.
- **`check-no-edit-on-main.sh`** — the **edit-side** complement to `check-worktree.sh`: **denies any Edit/Write to a primary clone while it's sitting on its default branch** (`main`/`master`). `check-worktree.sh` only fires at `git commit`, so plain edits and untracked files can quietly pile up on a base-on-`main` without ever tripping it — and that dirt has a second, sneakier cost beyond clobber-on-rebase: it **wedges fast-forward-only `main` updates**. If you keep local `main` current with a periodic `git pull --ff-only` (or an autopull agent that does), ff-only *correctly* refuses to advance over a dirty tree — so local `main` silently stops moving for as long as the base stays dirty. We hit exactly this: a base checkout parked dirty-on-`main` drifted many days and hundreds of commits stale before anyone noticed. The fix is the discipline this hook enforces — the base stays clean on its default branch, all work happens in worktrees on feature branches. Same exemptions as `check-worktree.sh` (linked worktrees, `/tmp`, `worktree-exempt.txt`); editing a primary clone that's on a *feature* branch is allowed (only the default branch is guarded), and reads stay unrestricted.

## Cross-cutting: tracker hygiene (Linear)

Two paired hooks keep the issue tracker honest about what's actually being worked on — so status reflects reality without anyone remembering to click. Both are **opt-in via env** and **fail open**: set `LINEAR_API_KEY` (or `LINEAR_KEY_FILE`, a JSON file holding `.env.LINEAR_API_KEY`), `LINEAR_DEV_TEAM_ID`, and — for the status/assignee flip — `LINEAR_INPROGRESS_STATE_ID` + `LINEAR_ASSIGNEE_ID`; the ticket-token prefix defaults to `dev` (`LINEAR_BRANCH_PREFIX` to change it). Find the UUIDs with `get_team` / `list_issue_statuses` / `get_user` in the Linear MCP. Unconfigured — or on any missing dep, API error, or timeout — both exit 0 and never block git.

- **`branch-name-gate.sh`** (PreToolUse) — on branch **creation**, requires the branch to carry the ticket's exact canonical `gitBranchName`, so the PR links and Linear's own status automation fires. No ticket token → deny (the PR would never link); token present but off-slug → deny and hand back the exact name to re-run. Put `LINEAR_SKIP=1` in the command to bypass for genuinely ticket-less branches (infra/config repos).
- **`reconcile-ticket.sh`** (CLI, called by `wrapup` and `babysit-prs`) — advances a ticket to **Deployed** only when *every* linked PR is merged, fixing the multi-PR race where the tracker's per-PR automation leaves a cross-repo ticket stuck In Progress after the first sibling merges. Advance-only, never sets Done, fail-open; needs `LINEAR_DEPLOYED_STATE_ID` on top of the shared config.
- **`linear-startwork.sh`** (PostToolUse) — the other half: once that branch lands, take the ticket — flip a not-started state to In Progress and assign you if it's unassigned (never reassigns someone else's ticket, never regresses In Review/Deployed/Done). It detects creation via `checkout -b/-B`, `switch -c/-C`, bare `git branch`, **and `git worktree add … -b`** — the last is what the worktree-per-ticket pattern actually uses, so without it the ticket silently never moves (we hit exactly this).

## Cross-cutting: work taxonomy (Linear conventions)

The vocabulary `PRlaunch`, `wrapup`, `bulldozer`, and `assign` all assume for filing and routing follow-up work. Canonical — those skills reference this section instead of restating it.

- **PROJECT** = a whole board / program-scale grouping (e.g. one product line).
- **EPIC** = one orchestratable chunk of work — roughly a session's worth, ~5 related tickets/PRs shipping a feature or feature-chunk. An epic stays open until it's genuinely done, **including its ops tail** (deploy verification, flag flips, doc updates, any follow-ups it spawned) — shipping the PRs is not the same as the epic being finished. Re-chunk an oversized epic into smaller session-sized epics chained by blocked-by rather than grinding through all of it in one pass.
- **Ticket** ≈ one PR.

**Follow-up filing buckets** — every follow-up ticket filed in any session picks exactly one bucket; nothing gets filed parentless:

1. **CR-deferred 1-off** — a small, self-contained, LLM-doable finding from a repo-wide review with no cross-ticket ordering → parent it under that project's standing **"Bulldozer 1-offs"** epic (one per board/project, title exactly `Bulldozer 1-offs`, state Epics, never closes — create it if the board doesn't have one yet). File it **unassigned**: this is `/bulldozer`'s queue, not a human's.
2. **Nit/ops follow-up on the feature being shipped** (a flag flip, a data migration, "turn this on once X is live") → the **same epic** as the feature it belongs to; that epic stays open until the follow-up is done.
3. **A bigger idea surfaced mid-session** → its **own new parked epic**, even if it starts with a single ticket.

`PRlaunch`, `wrapup`, `bulldozer`, and `assign` all route follow-ups through these three buckets — see each skill for where in its flow the routing happens.

## Cross-cutting: how we do memory

Not shippable in this repo (it's wired to our internal platform), but worth describing because it changes what an agent can do across sessions. We replaced Claude Code's native file-based memory (which truncates: first ~200 lines / 25 KB of `MEMORY.md`) with **retrieval-backed memory served over MCP**:

- An **MCP server** exposes two tools — `memory_search` and `memory_write` — backed by a memory service with namespaces for prescriptive rules vs. facts.
- A **UserPromptSubmit hook** runs a relevance query against the store on every prompt and injects the top-ranked memories into context — so the right gotchas, decisions, and runbooks surface *for the task at hand* instead of whatever fit in the first 25 KB.
- A **SessionStart hook** injects standing rules (the prescriptive namespace) at boot.
- **`/wrapup` step 4** is the write path: end of session, dedupe against existing entries, update rather than duplicate, skip anything derivable from the repo.

The effect compounds: deploy gotchas, reviewer false-positive lists, infra quirks, and per-repo policies recorded once get injected exactly when relevant, sessions later. If you build your own, the architecture above is the whole trick — ranked retrieval per prompt beats a static file the moment your memory outgrows the truncation window.

---

## Install

1. Copy the commands into your Claude Code config:

   ```bash
   cp commands/*.md ~/.claude/commands/
   ```

   And the skills:

   ```bash
   mkdir -p ~/.claude/skills
   cp -R skills/* ~/.claude/skills/
   ```

   (Skip the superpowers-derived skills if you already run the [superpowers](https://github.com/obra/superpowers) plugin — they ship there, and the live plugin auto-updates while these snapshots don't. `mf-frontend-design` is ours and only lives here.)

2. (Recommended) Install the hooks:

   ```bash
   mkdir -p ~/.claude/hooks
   cp hooks/*.sh hooks/*.py ~/.claude/hooks/
   chmod +x ~/.claude/hooks/*.sh ~/.claude/hooks/*.py
   ```

   Then add to `~/.claude/settings.json` (merge with any existing hooks):

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             { "type": "command", "command": "~/.claude/hooks/pr-gate.sh", "timeout": 10 },
             { "type": "command", "command": "~/.claude/hooks/check-careful.sh", "timeout": 10 },
             { "type": "command", "command": "~/.claude/hooks/check-worktree.sh", "timeout": 10 },
             { "type": "command", "command": "~/.claude/hooks/branch-name-gate.sh", "timeout": 10 }
           ]
         },
         {
           "matcher": "Edit|Write",
           "hooks": [
             { "type": "command", "command": "~/.claude/hooks/check-freeze.sh", "timeout": 10 },
             { "type": "command", "command": "~/.claude/hooks/check-no-edit-on-main.sh", "timeout": 10 }
           ]
         }
       ],
       "PostToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             { "type": "command", "command": "~/.claude/hooks/linear-startwork.sh", "timeout": 10 }
           ]
         }
       ]
     }
   }
   ```

   (`branch-name-gate.sh` and `linear-startwork.sh` are the optional Linear pair below — they no-op unless you set the `LINEAR_*` env vars, so they're harmless to wire in unconfigured.)

3. (Optional) Run the hook test suite — `bash run-tests.sh` — before and after adapting anything. Every test runs the real hook scripts in an isolated temp-HOME sandbox (fake `curl`/`gh` shims, throwaway git repos), so a local edit that breaks a guardrail fails loudly. `.github/workflows/tests.yml` wires the same suite into CI.

4. Adapt the stack-specific bits: the secondary reviewer command in PRlaunch phase 2 (we use the CodeRabbit CLI), the tracker references (we use ticket IDs like `XXX-123`), the infra checklists in deep-review Step 5d (written for FastAPI + Alembic + Celery + Docker — keep the categories, swap the specifics), and your team's merge policy in phase 5.

## Use

A full unit of work, end to end:

1. **`Skill(brainstorming)`** fires before any creative work — design agreed, approaches weighed, scope locked.
2. **Build.** Frontend surfaces go through `mf-frontend-design` (the skill self-verifies with screenshots as it builds).
3. **`/PRlaunch`** — the agent identifies the session's shippable units, confirms them with you, then runs each through: deep review → secondary review → outcome eval → re-gate checkpoint → push + PR (ready, not draft, with a Testing section reporting everything that actually ran).
4. **`/wrapup`** closes the loop (PRlaunch runs it as its final phase automatically): tracker synced, branches clean, memory written, one report.

`/deep-review <pr-number>` also works standalone against any existing PR, including paired backend+frontend reviews.

## Design notes

- **Every finding gets a disposition** — fixed, ticketed, or waived-with-reason. "Out of scope" means *track it elsewhere*, never *discard it*. The wrapup phase audits the disposition list.
- **The hook is the enforcement, not the process.** The gate marker is written only after the re-gate checkpoint passes on the final tree. Writing it early to silence the hook defeats the entire mechanism.
- **Gate order matters.** Deep review first (nit fixes would mutate code it just judged), nits second, outcome eval last (it grades the experience, so the code should already be correct and clean — and its findings loop you back through the earlier gates).
- **Structure informs findings; it never gates merges.** v6.8's structural lens deliberately rejects its source material's blocking stance: working code with a missed elegance opportunity ships, with the better shape recorded as a suggestion.

## Influences — and where we differ

Two projects shaped this config enough to deserve more than a credit line. Both are worth studying directly; both take approaches we deliberately didn't.

### Cursor's thermo-nuclear-code-quality-review

[The skill](https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md) is a pure structural-maintainability lens: hunt for "code judo" moves (reframings that delete whole branches/layers while preserving behavior), flag files crossing 1,000 lines, treat ad-hoc conditionals bolted onto unrelated flows as design problems, and be suspicious of thin wrappers, casts, and optionality that obscure the real contract.

**What we took:** the entire detection lens. It became Deep Review v6.8's structural regression prompt, the file-size threshold check, and the code-judo question — it covered a genuine blind spot (our reviewers detected duplicate logic, silent failures, and unregistered integration points, but nothing structural).

**Where we differ:** thermo-nuclear treats structural issues as *presumptive blockers* — its approval bar refuses working code that missed a visible simplification, and it pushes the reviewer to "be ambitious about restructuring." We inverted that: structural findings default to MEDIUM, never CRITICAL, and a missed simplification is a recorded suggestion, never a blocker. In a shop optimizing for merge velocity with multiple agents shipping in parallel, a reviewer that restructures ambitiously — or holds working code hostage to elegance — creates more risk than the mess it prevents. Structure informs findings; it never gates merges.

### garrytan/gstack

[gstack](https://github.com/garrytan/gstack) is a complete parallel AI-software-factory: ~80 skills spanning plan→build→review→QA→ship→retro, its own Playwright browser binary, a semantic memory layer (gbrain), event-sourced decision stores, per-question preference hooks, telemetry, and multi-host adapters (Claude Code, Codex, Cursor, …). It's the maximalist take — a whole operating layer installed on top of the agent.

**What we took:** the two most portable guardrails — `check-careful.sh` and `check-freeze.sh` (improved: a JSON-extraction bugfix and the current hook output schema) — and its core enforcement philosophy, which pr-gate.sh applies to shipping: **safety rules belong in deterministic hooks the harness executes, not in instructions the model is asked to remember.** A prompt can be rationalized around; a PreToolUse hook can't.

**Where we differ:** gstack builds its own infrastructure for nearly everything — custom browser, custom memory, custom state directories, custom analytics. We stay harness-native: plain markdown commands and skills in `~/.claude`, standard hooks in `settings.json`, MCP for memory, the stock browser tooling. That keeps every piece independently adoptable (you can take one file from this repo and use it today) and means there's no parallel ecosystem to maintain or upgrade. If you want the integrated-factory experience, gstack is the best version of it; if you want composable pieces on the stock harness, that's this repo.

(A third influence, Jesse Vincent's [superpowers](https://github.com/obra/superpowers), is different in kind: it's not just an influence — we run the full plugin in production alongside this config. Brainstorm → write plan → execute plan is the superpowers loop; this repo picks up where that loop ends, at shipping. The actively-used subset is vendored in `skills/` so this repo stands alone, but the live plugin is the better install — upstream keeps evolving and snapshots don't.)

## Credits

- Matt Rhodes, founder of Reeve (https://meetreeve.com)
- Deep Review v6.8's structural-quality lens is adapted from Cursor's [thermo-nuclear-code-quality-review](https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md) skill (with its approval-blocking stance deliberately softened) and Jesus Roncal's code review. 
- The careful/freeze safety hooks are adapted from [garrytan/gstack](https://github.com/garrytan/gstack), which also inspired the pr-gate enforcement style.
- The brainstorming, planning, TDD, debugging, worktree, verification, and code-review skills are vendored verbatim from Jesse Vincent's [superpowers](https://github.com/obra/superpowers) (MIT, license included in each directory).
- The frontend-design skill is merged from Anthropic's `frontend-design` skill and Leonxlnx's `taste-skill` (MIT).
- Written with Claude Code, which also runs it.

## License

MIT
