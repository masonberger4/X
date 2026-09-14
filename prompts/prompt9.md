You are building step 9 of the pipeline described in PLAN.md: the swarm.
Read PLAN.md and CLAUDE.md first, then this file end to end.

Why. Steps 2 and 7 draft every story with ONE strong model and ONE human
editor. The human's ten minutes a day are the bottleneck and the ceiling:
the account can only get as good as one person's taste, one edit at a time.
Step 9 takes the human out of the creative loop. The human keeps exactly
one job: deciding when a post goes out ("Publish now" / "Set schedule" on
the approved page, PUBLISH_ENABLED=1 and --live). Everything creative,
writing, editing, judging, and the picture, is done by many AI agents, and
in later phases the agents that produce posts X rewards stay while the
ones that do not are pruned.

The design bet, and the reason this is not "run the drafter six times and
vote": Philip Anderson's "more is different". A single cheap model is a
poor analyst. Many cheap calls, each with a narrow job, local rules and no
view of the whole, arranged in the right topology, can beat one strong
call (the 2024 "Mixture of Agents" result: layers of sub-GPT-4 open models
beat GPT-4). So step 9 is built from CELLS, not authors: no agent writes a
thread, and the arrangement, not the model, is what gets better over time.
The bet may lose. Phase one therefore always runs the existing single
strong drafter as a CONTROL on every story and records which one won, so
phase two can measure the bet on real X data and prune the swarm topology
back toward one strong call if that genuinely wins.

What never changes, in every phase:
- draft/drafter.py:check_hard_rules, verify_numbers, chart_problems and
  step 2b's claim and table checks run in code on every assembled draft.
  Cheap models hallucinate more; that is fine because these are code, not
  model judgement. The hard rules are the fixed physics the swarm evolves
  inside. No cell prompt, genome or judge can relax them.
- Nothing posts unless PUBLISH_ENABLED=1 and --live. The panel's "Publish
  now" and "Set schedule" are the human's only creative-adjacent controls.
- Config drives everything: model IDs, population sizes, fan-out, depth,
  thresholds live in swarm/config.yaml; never in code.
- Network I/O stays where it is: every swarm call goes through
  draft/drafter.py:call_anthropic (the swarm engine takes it as its `call`
  parameter, tests pass a fake). No new network module, no new dependency.

Phases (this prompt is phase one; two and three are written here so the
phase-one tables and records are shaped for them):

PHASE ONE, cells + layers + control (build this now):

  swarm/
    config.yaml   # step 9's own file, NOT the root config.yaml:
                  #   enabled: true
                  #   model: claude-haiku-4-5-20251001  # cells, synthesisers, judges
                  #   assembler_model: ""     # blank = same as model
                  #   fan_out: 6              # proposals per slot per layer
                  #   layers: 2               # 1 = proposals only; 2+ = MoA synthesis
                  #   judge_votes: 3          # odd; swarm-vs-control jury size
                  #   max_similarity: 0.85    # difflib ratio; near-twins are dropped
                  #                           #   before a slot's tournament
                  #   control:
                  #     enabled: true         # also run draft_item and judge the pair
    genome.py     # pure. @dataclass Slot(name, rule); @dataclass Genome(name,
                  #   slots: list[Slot], fan_out, layers, parent_id=None,
                  #   id=None). DEFAULT_GENOME: six slots, in thread order:
                  #   hook, mechanism, thesis, catalyst, risk, closer. Each
                  #   rule is one or two sentences of local instruction (what
                  #   this post must do, what it must not do). to_json /
                  #   from_json. Phase three mutates these rows; phase one
                  #   only seeds the default.
    prompts.py    # pure prompt builders. A cell sees ONLY: the source brief
                  #   (title, abstract, url, source, preprint flag, scorer
                  #   angle), its slot's rule, the cells already chosen for
                  #   the slots before it, and the hard rules that apply to
                  #   one post. It never sees the voice guide in full or the
                  #   whole thread. Builders: propose_prompt (layer 1),
                  #   synthesise_prompt (layer 2+, given every previous
                  #   layer's outputs, MoA style), judge_prompt (A vs B, one
                  #   slot, returns JSON {"winner": "A"|"B", "reason"}),
                  #   thread_judge_prompt (A vs B, two full threads),
                  #   assemble_prompt (the chosen cells + the step 2 brief,
                  #   asks for the step 2 OUTPUT_JSON_SCHEMA object with the
                  #   cells as the thread, at most light joins, plus the
                  #   visual, why_it_matters and claims_to_verify).
    cells.py      # pure. cell_problems(text, *, source_text, url, slot,
                  #   is_preprint) -> list[str]: 280 chars (URL as 23), empty,
                  #   medical/investment advice phrases (drafter's patterns),
                  #   numbers not verbatim in the source, closer must carry
                  #   the URL verbatim, hook must say "preprint" for a
                  #   preprint. dedupe(candidates, max_similarity): drop a
                  #   candidate whose difflib ratio to an earlier survivor is
                  #   >= max_similarity. tournament(candidates, judge):
                  #   single elimination, judge(a, b) -> a or b; the A/B
                  #   order is swapped on alternate matches to blunt
                  #   position bias.
    engine.py     # run_swarm(brief, genome, cfg, *, call, rng) ->
                  #   SwarmResult(draft_result, cells: dict[slot, str],
                  #   log: list[dict], calls: int). Per slot in genome order:
                  #   fan_out propose calls; for each further layer fan_out
                  #   synthesise calls that see every earlier layer; drop
                  #   cells that fail cell_problems; dedupe; tournament by
                  #   pairwise judge calls; the winner joins the context of
                  #   the next slot. Then assemble: one system prompt from
                  #   draft.prompt.build_system_prompt (voice guide + hard
                  #   rules + schema, examples_block allowed) and the
                  #   assemble user prompt, run through
                  #   draft.drafter.generate (the public name for the
                  #   draft_item attempt loop: schema, hard rules, chart
                  #   check, retries). The assembler may join cells and fix
                  #   grammar; it is told not to rewrite them, and the log
                  #   records the difflib ratio between each cell and its
                  #   final post. compare(a, b, brief, cfg, *, call, rng) ->
                  #   Verdict(winner: "swarm"|"control", votes: list[dict]):
                  #   judge_votes thread-judge calls, A/B order randomised
                  #   per vote, majority wins, ties go to the control.
    store.py      # owns three tables, created by ensure_tables(conn) on the
                  #   pipeline DB; reads no other step's table:
                  #   swarm_genomes(id, name, genome_json, parent_id,
                  #     created_at, retired_at)
                  #   swarm_runs(id, draft_id, item_id, cluster_id,
                  #     genome_id, winner, calls, log_json, created_at)
                  #   swarm_variants(id, run_id, role 'swarm'|'control',
                  #     model, draft_json, ok, problems, created_at)
                  #   seed_default(conn) inserts DEFAULT_GENOME when the
                  #   table is empty; active_genome(conn) returns the newest
                  #   unretired row. record_run / record_variant.
    settings.py   # load_swarm_config().

  run_draft.py    # additive. New flag --no-swarm. When swarm.enabled and
                  #   not --no-swarm: per candidate, run_swarm, and when
                  #   control.enabled also draft_item as today; both are
                  #   recorded as variants; compare picks the winner, which
                  #   is stored through store.insert_draft exactly as today
                  #   (model column: the winner's model, prefixed "swarm:"
                  #   for a swarm win) so run_verify.py, the queue, publish
                  #   and feedback see an ordinary pending draft. A swarm
                  #   that fails every hard rule loses to the control; if
                  #   both fail the draft is stored failed as today. With
                  #   swarm disabled the run is byte-for-byte the old path
                  #   and every existing test passes unchanged.
  tests/          # test_swarm_cells.py, test_swarm_engine.py (fake call
                  #   that answers by prompt shape; asserts layer counts,
                  #   dedupe, tournament order, that a cell with an invented
                  #   number is dropped, that the assembled draft passes
                  #   check_hard_rules), test_swarm_store.py, and one
                  #   run_draft test with the swarm on that checks the
                  #   variants and run rows. tests/conftest.py gets an
                  #   autouse fixture that turns the swarm OFF for every
                  #   existing test; swarm tests turn it on.
  Docs: README "## Swarm drafting (step 9)", HOWTO "Changing settings"
  names swarm\config.yaml, CLAUDE.md commands/rules/layout, prompts/README
  row, PLAN.md section "9. Swarm". tests/test_docs_coverage.py enforces
  the CLI and settings-file mentions.

  Cost: a story is fan_out x layers x slots cell calls plus roughly
  fan_out x slots judge calls plus one assembly and judge_votes jury
  calls: about 110 cheap calls at the shipped values, which is in the
  region of five to ten strong-model drafts. ops/config.yaml's
  drafts_per_day_max is a row count and needs no change.

PHASE TWO, fitness from X (next prompt):
  run_evolve.py joins feedback's `posts`/`tweet_metrics` to swarm_runs by
  draft_id, credits the genome (and later the individual slot rules,
  judges and designer styles) with a RELATIVE kpi: the post's impressions
  divided by the trailing 30-day median, so a slow week prunes nobody.
  The swarm-vs-control verdicts are scored against the same data, which
  is the first real measurement of the bet. Judges get a second fitness:
  how often their pick was the post that did better. A population of
  genomes (not one) is drafted round-robin so there is variance to select
  on. Below-median genomes with >= min_posts observations are retired.

PHASE THREE, mutation and topology (after that):
  The one strong-model call in the loop: read the top genomes and their
  winning posts and write a child genome that varies ONE thing: a slot
  rule, fan_out, layers, a slot split or merge, a judge criterion. The
  child is stored with parent_id so the family tree is readable in the
  panel (/swarm). The image side joins: designer genomes own a
  draft.chart.Style preset and go through the existing grader loop with
  the same fitness accounting. Selection acts on the graph, so the
  pipeline can arrive at a structure nobody designed, including a single
  strong call if that is what X rewards.

Boundaries for this session:
- Work on the branch you were given.
- You MAY create swarm/, tests/test_swarm_*.py, prompts/prompt9.md; edit
  run_draft.py (additive), draft/drafter.py (add the public `generate`
  name for `_generate`; nothing else changes), tests/conftest.py (the
  autouse fixture), tests/test_run_draft.py (one added test), README.md,
  HOWTO.md, CLAUDE.md, PLAN.md, prompts/README.md.
- Do NOT edit any other step's package, table, or settings file; no new
  dependency; never a `claude-*` ID in code.
- Commit after each working module.

PHASE FOUR, the format is heritable (written after phases one to three merged):

  Phases one to three evolved inside fixed physics: every draft a thread of
  3-6 posts, exactly one visual, attached to the first post. Those three
  constraints are opinions, not physics, and the account has no evidence
  for them. Phase four turns them into a third population, FORMAT genomes,
  drafted round-robin like writers and designers, credited with the same
  relative KPI, pruned and bred the same way, with their own, higher
  `format_min_posts` because a format is a coarse gene and five noisy posts
  must not kill a shape.

  What stays physics: the per-post character limit for the shape (280, or
  `long_max_chars` for a long post), the advice bans, verbatim numbers, the
  source URL in the last post, the preprint label in the first, chart
  numbers verbatim, table cells web-checked.

  A Format (draft/schema.py, pure, so steps 2 and 3 can read it without
  importing swarm/):
    shape      thread | single | long
               thread: `min_posts`..`max_posts` posts of <= 280 chars
               single: one post of <= 280 chars
               long:   one Premium long-form post of <= `max_chars` (the
                       `long_max_chars` in swarm/config.yaml at draft time,
                       shipped 4000; X allows 25000 for Premium, the API
                       accepts it for a Premium account and rejects it with
                       code 111 otherwise); sections joined by blank lines
    visuals    0 | 1 | 2   how many pictures the draft carries
    anchors    one 1-based post index per visual (single/long: always 1);
               `first`, `last`, `middle` in the genome, resolved against the
               thread length at assembly
  Rules: the FIRST visual may be a chart or a table (tables keep the step 2b
  verification path unchanged); any further visual must be a chart (numbers
  verbatim, no web check needed). `validate_output(data, fmt=None)` and
  `check_hard_rules(..., fmt=None)` with fmt None behave byte-for-byte as
  before (thread 3-6, exactly one visual), so every existing test passes; a
  fmt makes them read the gene. The output schema gains an optional
  `visuals` list for the second picture. The Draft gains `shape`, `anchors`,
  `max_chars` and `extra_visuals`; drafts.format_json and drafts.images_json
  are guarded migrations (`images_json`: every rendered picture with its
  index, path, alt and anchor; `image_path`/`image_alt` stay the first
  picture for older readers).

  swarm/genome.py: FormatGenome(name, shape, min_posts, max_posts, visuals,
  anchors (list of first|last|middle), parent_id, id, notes) stored in
  swarm_genomes with kind 'format'; SEED_FORMATS: thread-1-first (the phase
  one physics, as the control), thread-2-ends (two charts, first and last),
  thread-0 (no picture: tests the "every post needs a graphic" belief),
  single-1, long-1. swarm_runs.format_id / swarm_fitness.format_id (guarded).
  The engine reads the format: a single post runs ONE slot (the genome's
  hook rule, whole story, <= 280); a long post runs the genome's slots as
  sections with `long_section_chars` each and the assembler joins them; a
  thread is as today. cells.cell_problems takes `max_chars`. The assembly
  prompt states the shape, the post count and how many visuals to give.
  Breeding a format is pure code (`mutate.breed_format`): step one field to
  a neighbour (shape, visuals +-1, one anchor, min/max posts +-1) inside the
  bounds. The panel's /swarm page gets a Formats table.

  Step 3 reads `format_json` and `images_json` in fetch_approved
  (Approved.shape, max_chars, images: list of (path, alt, anchor)),
  `publish/thread.py` checks each post against the shape's limit (never
  edits), and publish_one uploads each picture just before the post it is
  anchored to (an upload failure before post 1 posts nothing; a later one
  leaves a PARTIAL thread as today). `media.attach_images` still turns all
  of it off. The queue's detail page shows every picture with its anchor;
  drop and redraw act on the first (tables) and all charts respectively.

  Bounds: thread 2..6 posts (min <= max), visuals 0..2, anchors within
  the thread, long_max_chars from config, sections <= long_section_chars.
  Cost is unchanged: the same cells, one more chart render at most.

  Boundaries for this session: swarm/, draft/schema.py, draft/prompt.py,
  draft/drafter.py (additive, fmt=None defaults), approval_queue/store.py
  (guarded migrations, images_json), approval_queue/images.py,
  approval_queue/templates/detail.html, publish/store.py, publish/thread.py,
  run_publish.py, run_draft.py, run_evolve.py, ops/store.py, panel/,
  tests, docs. No new dependency. Every existing test passes unchanged.
