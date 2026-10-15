---
name: features/loop
desc: |
  The iteration loop: the step sequence, prompt assembly, plans, the commit
  pipeline, and run and iteration accounting.
created: 2026-07-21T04:35:35Z
updated: 2026-10-15T00:53:51Z
---

# features/loop

[[features/_index|..]]

[[features/loop/accounting|accounting]]: Run, iteration, and step accounting:
the row tables and events in the tree's central database, the shared status set,
exit codes, and how pauses credit time back to deadlines.

[[features/loop/commit_pipeline|commit_pipeline]]: The work-product commit
pipeline behind fractal commit: scope enforcement, wiki index refresh and lint,
staging excludes and warnings, subject composition, the hook retry, and the
force-commit backstop.

[[features/loop/plans|plans]]: Plan files: the per-iteration planning artifacts
under the node's plans directory, the fractal plan CLI that creates and lists
them, and how the seed steps use them across the iteration.

[[features/loop/prompt_assembly|prompt_assembly]]: How each step's prompt is
assembled: the node charter, the step body, and the active mode documents,
rendered through one merged template variable map with envsubst-pinned
substitution.

[[features/loop/steps|steps]]: The step sequence of an iteration: how step files
are discovered and ordered, what each of the five seed steps instructs, the
frontmatter overrides a step file can carry, the SYNC pass that precedes each
step, the checkpoints the loop runs between steps, the retry and model-drop
re-dispatch policies, and the steer and stall of a stuck step.

***

Covers the in-process iteration loop that drives a running node. The loop
(`fractal/core/loop.py`) runs inside the node's tmux session and drives each
iteration end to end: it discovers and orders the step files
([[features/loop/steps|steps]]), assembles each step's prompt from the charter,
step body, and active modes ([[features/loop/prompt_assembly|prompt_assembly]]),
has the PLAN step write a plan file per iteration
([[features/loop/plans|plans]]), lands the iteration's work through the commit
pipeline ([[features/loop/commit_pipeline|commit_pipeline]]), and books every
run, iteration, and step as rows and events in the central database
([[features/loop/accounting|accounting]]).
