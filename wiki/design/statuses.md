---
name: design/statuses
desc: |
  Why one status vocabulary serves both the node status file and every
  database row table, which statuses apply at which level and why, why exit
  codes are binary and derived from outcome, and why events are
  point-in-time instants with durations always derived.
created: 2026-07-21T04:48:14Z
updated: 2026-07-21T04:48:14Z
---

# design/statuses

[[_index|..]]

***

Fractal answers "what state is this thing in?" at four levels — node, run,
iteration, step — plus a stream of events, and it answers all of them from one
status vocabulary. The design goal is that any two readers of the system agree
without translation: a parent listing children, the TUI, a merge guard, and a
SQL query over history all speak the same words.

## One status set, two homes

The status vocabulary is defined once and shared by the node's status file and
every row table in the central database. The two homes exist because they answer
different questions at different costs. The status file answers "what is this
node *now*" — a single local read that works without touching the database,
keeping lifecycle state out of the config file so config stays purely user
settings; a node with no status file yet simply reads idle. The database rows
answer "what happened, and what is still open" — runs, iterations, and steps
each carry their own status, so history is queryable at every granularity.

Sharing one vocabulary across both is what keeps them reconcilable. When a loop
dies without closing its rows, the healer stamps the same terminal status on the
status file and the still-open rows, so the two stores can never permanently
disagree about whether a node is running. Display strings decorate but never
fork the vocabulary: a pending signal shows as a suffix on the bare status, and
the stored value stays the bare word that filters match on.

## Which statuses apply at which level, and why

Not every status is meaningful at every level, and the restrictions carry design
intent:

- **failed** is for entity rows only — a step or iteration can fail, but a *run*
  that ends early records exited, never failed. A run is a container of
  attempts, not an attempt itself: its honest terminal says "it stopped before
  the goal," and the failure that stopped it is recorded on the row that
  actually failed.
- **idle** and **retired** are node-only. Both describe a node *between* runs —
  never started, or deliberately shelved — and rows only exist once a run
  exists.
- A user (root) node is marked by configuration, not by a status. The root never
  iterates, so a lifecycle status would be a fiction; its identity is a config
  fact, and its "status" is just whatever its tree is doing.
- **paused** is active-like everywhere but execution: rows stay open, slots stay
  held, drains stay blocked. The rationale — pause is a promise to resume, not
  an exit — is on [[design/durability]].
- **completed** versus **exited** is the vocabulary's load-bearing distinction:
  completed means the run did what was asked — the node declared its goal met (a
  drained finish), or it ran its full configured iteration count with a clean
  final iteration; exited means the run ended for any structural reason —
  budget, timeout, max iterations with a failed final iteration, crash. A parent
  deciding whether to merge, continue, or absorb a child's work reads this one
  word first, which is why the loop is strict about never letting a failed or
  budget-cut stop launder into completed. The two completed landings stay
  distinguishable too: a full iteration count records its cap on the run row and
  surfaces as `run exhausted: ...` in the status detail, while a drained finish
  reads bare — an exhausted lane usually wants a re-continue, a finished one is
  done.

## Binary exit codes, derived from outcome

Rows carry an exit code alongside the status, and it is deliberately binary:
zero for every designed landing, one for every abnormal one. The exit code is
never an agent's raw process code and never a third channel of meaning — it is
*derived* from the outcome the status already names, collapsing "how it ended"
into "was this ending part of the design." Completed runs, requested stops, and
budget landings all exit zero; timeouts, setup crash-loops, failed final
iterations, and unexplained deaths exit one.

The redundancy is the point: status carries the nuance, the exit code carries
the alarm. A monitoring query can find every abnormal ending without enumerating
status words, and the one deliberate composite — exited with code zero — cleanly
discriminates a budget landing (designed, but not goal-met) from both a crash
(exited, one) and a success (completed, zero).

## Point-in-time events, derived durations

Everything durable in the record is an instant. Runs, iterations, and steps
store a start and an end timestamp; events — init, spawn, commit, merge, pause,
resume, and kin — store a single creation instant. Durations exist only as
derived readings computed from instants at query time, never as stored values.

Storing instants rather than durations keeps the record append-only truthful. A
stored duration is a claim about two moments that can silently disagree with the
moments themselves; a derived duration cannot drift, and an open row's "duration
so far" is always current for free. It also makes the record correctable by
*addition*: the pause and resume events are point-in-time instants whose spans
are credited back to run and iteration deadlines when read (see
[[design/durability]]) — a design that only works because nothing pre-computed
the elapsed time and baked the parked days in. Events double as the audit trail
for actions that outlive their actor: a merge is logged on the target so the
record survives the merged child's deletion, and a commit event anchors each
work commit's hash into run lineage.

The full schema, the status file mechanics, and the crash-reconciliation walk
are structural material — see [[architecture/_index|architecture/]] and
[[features/lifecycle/status_machine]], which calls the terminal statuses
*settled* and the rest *unsettled*; how budget landings choose their terminal
status is on [[design/budgets]].
