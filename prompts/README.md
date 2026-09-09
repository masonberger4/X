# Kickoff prompts

Each file is the prompt that was pasted into a fresh Claude Code session to
build one step of the pipeline described in [PLAN.md](../PLAN.md). Prompts 3
onward were written so that their step could be built in parallel with the
previous one on a separate branch, with strict file-ownership boundaries.

| Prompt | Step | Result |
|---|---|---|
| [prompt1.md](prompt1.md) | Ingest, dedup, prefilter, score, digest | merged (PR #3) |
| [prompt2.md](prompt2.md) | Draft + approval queue | merged (PR #4) |
| [prompt3.md](prompt3.md) | Publish layer | merged (PR #7) |
| [prompt4.md](prompt4.md) | Feedback loop | merged (PR #10) |
| [prompt5.md](prompt5.md) | Operations layer | merged (PR #12) |
| [prompt6.md](prompt6.md) | Conference abstracts, KOL X list, HTTP retry | merged (PR #14) |
| [prompt7.md](prompt7.md) | Voice learning loop | merged (PR #15) |
| [prompt8.md](prompt8.md) | PROJECT BUILD COMPLETE: audit of PLAN.md against steps 1-7 | merged (PR #13) |

The prompts are kept as a record of how the code was specified, not as
instructions to re-run. Any new requirement should be written as its own
prompt against the current `main`, following the boundary conventions in
prompt5.md through prompt7.md.
