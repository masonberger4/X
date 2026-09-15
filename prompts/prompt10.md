You are running step 10: a whole-repo refactor pass, done by a swarm of
subagents rather than by you alone. Read PLAN.md and CLAUDE.md first, then
this file end to end, then start.

Why a swarm. The repo is ~22k lines of source across eleven step packages
(`ingest/ filter/ score/ draft/ approval_queue/ verify/ publish/ feedback/
ops/ panel/ swarm/`), 166 Python files, and a test suite that encodes most
of the boundary rules in CLAUDE.md. No single context window reads that
honestly. One strong agent asked to "refactor the repo" skims, guesses, and
produces a plausible diff nobody can verify. So this pass uses the same bet
step 9 makes about drafting, applied to reading code: many cheap agents,
each given ONE narrow job, a small slice of the tree, and no view of the
whole, layered so that later layers only ever judge, merge and verify what
earlier layers produced. The intelligence is in the topology, not in any
one call.

Models. Haiku and Sonnet ONLY. No Opus anywhere in this pass, including for
your own orchestration reasoning; if a job feels like it needs Opus, it is
a job that has not been cut small enough yet. Use:
- **Haiku** for every wide, mechanical, single-file-shaped job: reading one
  module and reporting, re-reading one finding to confirm it, checking one
  patch against one rule. Haiku is the workhorse and should account for the
  large majority of agents spawned.
- **Sonnet** for jobs that require holding several findings at once:
  clustering duplicates, adjudicating a contested finding, writing a patch
  that touches more than one file, reviewing a diff, and the final
  integration pass.
Never use Sonnet where three Haiku agents and a Sonnet judge would do the
same work. Never use Haiku to write a patch that crosses a module boundary.

No limit on the number of agents. Keep adding agents to a layer while the
layer is still producing findings that survive the next layer; stop adding
when it is not. The measurement rule is in LAYER 7.

## The four lenses

Every scouting agent is given exactly ONE of these four lenses and one
slice. A lens is not a mood; it is a checklist with a verdict format.

1. **BUGS** — wrong behaviour on some reachable input. Off-by-one, a
   `None` that reaches an attribute access, an exception type that a caller
   does not catch, a guarded migration that runs twice, a clock or timezone
   assumption, a SQL row read by position after a column was added, an
   `except Exception` that swallows a real failure, a fail-soft path that
   fails hard. A BUGS finding MUST name the input or state that triggers it
   and the observable wrong outcome. "This looks fragile" is not a finding.
2. **SHAPE** — the code disagreeing with CLAUDE.md. This repo's rules are
   unusually explicit and are the spec: config drives everything (no
   hardcoded feeds, URLs, queries or `claude-*` IDs in code); network I/O
   only in the named functions; each step reads another step's tables only
   through that step's named read-only adapter; pure modules stay pure
   (`ops/health.py`, `panel/views.py`, `swarm/fitness.py`, `swarm/prompts.py`,
   `filter/`-side helpers — `now` is a parameter, no DB, no network, no
   clock); the panel writes only the four keys in `publish/config.yaml` and
   `trusted_domains` in `verify/config.yaml`; `ops/config.yaml` never
   contains `--live`. A SHAPE finding MUST quote the CLAUDE.md rule it
   breaks and the line that breaks it. Also in this lens: dead code,
   duplicated logic across two steps that should be one helper, a function
   doing three jobs, a name that lies.
3. **PERFORMANCE** — work done more times than needed. N+1 queries against
   SQLite, a missing index on a column every read filters by, `difflib`
   comparisons that are quadratic over a growing table, a config file or
   fixture parsed per call instead of per run, a whole table pulled into
   memory to count it, a `models` call made for a case code could decide,
   matplotlib imported at module scope. A PERFORMANCE finding MUST say what
   grows (rows, sources, drafts, cells) and roughly how the cost grows with
   it. Do not micro-optimise code that runs once a day over 50 rows.
4. **APP** — the product, not the code. What the operator runs, sees and
   configures: a panel page that does not show the thing you would look for
   at 7am, a run button that gives no feedback, a failure that is only
   visible in a log, a queue interaction that takes four clicks, a health
   check that cannot fire, a config knob with a silently bad default, a
   HOWTO step that no longer matches the CLI. An APP finding MUST name the
   human moment it improves and stay inside the existing architecture — it
   is a refinement of this pipeline, never a new step.

## The slices

Cut the tree into slices of roughly one module each — a single `.py` file,
or a small file plus its direct test. Aim for slices a Haiku agent can read
completely; split anything over ~500 lines. Build the slice list mechanically
from `find . -name '*.py'` (excluding `.git`, egg-info) plus the settings
files (`*/config.yaml`, root `config.yaml`) and the operator docs (HOWTO.md,
README.md, OVERVIEW.md). Write the list to the scratchpad; it is the work
queue for layers 1 and 2 and every later layer refers to slices by path.

## The layers

Run them in order. Layers 1-6 write findings and patches; nothing is
committed until layer 8.

**LAYER 1 — scouts (Haiku, widest layer).** For every slice, spawn one
agent per lens: four agents per slice, each read-only, each seeing its own
slice, CLAUDE.md, and its lens definition, and NOTHING else. No agent in
this layer may read another agent's output — independence is what makes the
later voting worth anything. Each returns findings as JSON lines:
`{lens, path, line, what, trigger, fix_sketch, confidence}` with confidence
in {low, medium, high}. Zero findings is a valid and expected answer; say
so explicitly rather than inventing one. Cap each agent at 8 findings so it
spends its attention ranking rather than listing.

**LAYER 2 — second opinions (Haiku).** Re-run the BUGS and SHAPE lenses over
every slice with a DIFFERENT framing: a second agent per slice per lens that
has not seen layer 1's output and is asked the question inverted — "for each
public function here, what input makes it do the wrong thing?" for BUGS,
"which CLAUDE.md rule is this file closest to violating?" for SHAPE. This
layer exists because independent agreement is the only cheap signal of truth
available. Findings that two blind agents reach separately start at high
prior; findings only one reached start low.

**LAYER 3 — cross-cutting scouts (Haiku, a few Sonnet).** The slices above
are per file, so they cannot see boundary violations. Spawn one agent per
invariant, each given only the greps and files that invariant touches:
- every call site of `httpx`, `tweepy`, `Entrez`, `subprocess` — is it in an
  allowed module per CLAUDE.md's network rule?
- every `claude-` string literal and every hardcoded URL/feed/query in `.py`
- every cross-step table read — does it go through the named adapter?
- every guarded migration — is it idempotent, and is the column it adds read
  defensively by old rows?
- every `except` clause — which ones swallow, which ones should?
- every `--live` reachable argv path
- every `config.yaml` key — is it read anywhere, and is anything read that
  is not documented?
- the schema: every table, its indexes, and the queries that hit it
- HOWTO.md / README.md against the actual CLIs and flags
Use Sonnet only for the schema and the cross-step-adapter agents; the rest
are greps with judgement and Haiku does them.

**LAYER 4 — dedup and cluster (Sonnet).** Layers 1-3 will produce a lot of
overlapping text. Partition findings by file and by lens and give each
partition to one Sonnet agent that merges duplicates into single findings
carrying an `agreement` count (how many independent agents reached it), and
drops nothing. Output one deduplicated finding list.

**LAYER 5 — adversarial verification (Haiku, one per finding).** This is the
layer that makes the cheap ones safe. Every surviving finding gets its own
agent whose job is to KILL it: read the actual code and the actual tests,
and return CONFIRMED (with the exact failing path, and for BUGS the input
that triggers it), REFUTED (with the code or test that already handles it),
or NEEDS-JUDGEMENT. A finding whose verifier cannot state the trigger in one
sentence is refuted by default. Contested findings — high agreement but
refuted, or NEEDS-JUDGEMENT — go to one Sonnet adjudicator each. Everything
still standing after this layer is the work list; everything else is dropped
without ceremony and without being re-litigated later.

**LAYER 6 — patches (Haiku for single-file, Sonnet for anything else).**
Order the work list: confirmed BUGS first, then SHAPE violations of a named
CLAUDE.md rule, then PERFORMANCE with a stated growth curve, then APP. Group
findings that touch the same file into one patch job so two agents never
edit one file. Each patch agent gets its findings, the file(s), and the
rules below, and must return a working-tree edit plus the test it added or
changed. Patch rules, which are absolute:
- Every behaviour change ships with a test. A bug fix ships with a test that
  fails before the patch and passes after; the agent must state both.
- Tests never hit the network; monkeypatch the functions CLAUDE.md names.
- No new dependency without asking the human first (CLAUDE.md rule).
- No new `claude-*` ID, feed, query or URL in code; it goes in config.
- Do not relax a hard rule, delete a test, or widen an `except` to make a
  failure go away.
- Keep each patch minimal and inside the module that owns the code. A patch
  that wants to cross a step boundary stops and reports instead.
- If the operator's surface changes at all, HOWTO.md changes in the same
  patch (`tests/test_docs_coverage.py` enforces this, and so does CI).

**LAYER 7 — review, and the stopping rule (Sonnet).** Every patch gets a
reviewer agent that did not write it: does the diff do what the finding
said, does the new test actually fail on the old code, does it break a
CLAUDE.md boundary, is it minimal? Rejected patches go back to layer 6 once
with the review attached; rejected twice, the finding is dropped and
recorded as dropped.
Then measure the round. For each layer, record: agents spawned, findings
produced, findings that survived layer 5, patches that survived layer 7.
The **yield** of a layer is survivors per agent. Start the next round —
another wave of scouts, with the slices re-cut around the files that have
now changed and around the files where the last round found the most —
while the previous round's yield is still meaningfully above zero. Stop
adding agents to a layer when doubling it did not raise its survivor count,
and stop the whole pass when a full round produces no patch that survives
layer 7. Expect three to five rounds. Write the yield table to the
scratchpad each round; it is the evidence for why you stopped, and it goes
in the PR body.

**LAYER 8 — integration (you, Sonnet-level care, no subagent).** You own
this and do not delegate it. Apply the surviving patches in dependency
order, and after each group run `ruff check .`, `ruff format --check .`
and `pytest`. Green before the next group; never a batch of patches
committed against a red suite. Resolve conflicts between two patches by
re-reading both findings, not by picking the larger diff. Commit after each
working module, as CLAUDE.md requires, with a message naming the finding
class and the file. Then update HOWTO.md and CLAUDE.md for anything the
pass changed about what the operator runs, sees or configures, and about
any boundary that moved.

## Boundaries for the whole pass

- This is a refactor, not a step 11. No new package, no new pipeline stage,
  no new table, no new network module, no new dependency. APP findings are
  refinements of existing pages, flags and defaults.
- The hard rules of the product are untouchable: no medical or investment
  advice in generated content, preprints labelled, `check_hard_rules` and
  the step 2b checks stay code and stay strict, nothing posts without
  `PUBLISH_ENABLED=1` and `--live`, `ops/config.yaml` never carries `--live`.
- No subagent may run `git commit`, `git push`, or any `run_*.py` with
  `--live`, and none may touch `.env`. Scouts and verifiers are read-only;
  only layer 6 agents edit files, only you commit.
- No network in this pass beyond reading the repo: no live pipeline runs.
  `pytest` and `ruff` are the only things that execute code.
- Anything a subagent proposes that would change a step's public boundary
  (the named adapters, the network functions, the pure modules) comes to you
  and then to the human as a note in the PR, not as a patch.

## What you deliver

One branch, commits per working module, and a PR whose body contains:
1. the yield table per round, with agents spawned and survivors per layer,
   and the sentence that says why you stopped;
2. every merged finding, one line each, grouped by lens, with its agreement
   count and the commit that fixed it;
3. the findings that were confirmed but deliberately not fixed, each with
   the reason (out of scope, needs a human decision, crosses a boundary);
4. anything the pass learned about the repo that belongs in CLAUDE.md, and
   whether you already put it there.

Do not report the pass as done while `pytest` or `ruff` is red, and do not
describe a finding as fixed unless a test proves it.
