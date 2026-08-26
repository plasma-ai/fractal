---
name: features/spawning/slot_accounting
desc: |
  How spawn slots are counted: unsettled statuses occupy a slot, settled
  and retired nodes free theirs automatically, checks serialize under the
  worktrees lock, and re-entry paths re-check before re-occupying a slot.
created: 2026-07-21T04:58:40Z
updated: 2026-07-21T04:58:40Z
---

# features/spawning/slot_accounting

[[features/spawning/_index|..]]

***

## What holds a slot

The width and descendant caps count **unsettled** nodes only — status `active`,
`paused`, or `idle`. A settled node (`completed`, `stopped`, `exited`, `killed`)
or a `retired` one frees its slot automatically, with no explicit release step:
the caps bound how much of the tree is live at once, not how many children a
node may ever create. A `paused` node keeps holding its slot — parked work is
still claimed capacity.

Before counting, crashed-but-active nodes are healed to their real status (and
the healed status persisted), so a dead loop does not pin a slot forever. The
runtime liveness probe behind that healing is batched and paid only when
something actually reads active state.

## Race-free checks

The spawn gate runs under the `.worktrees` file lock, immediately before the
child is registered. Concurrent spawns therefore serialize, and the check is
TOCTOU-safe: the child just registered lands `idle` — already unsettled, already
occupying its slot — so the next serialized check sees it and counts it. A
fan-out of spawns can never overshoot a cap between check and register.

## Re-entry re-checks

Two paths return an existing node to the unsettled pool, and both re-check the
width and descendant caps under the same lock before proceeding:

- `start --continue` on a settled node re-arms it for another run.
- `unretire`, when it restores the node to `idle` (a restore to a settled status
  holds no slot and passes ungated).

Each re-entry adds exactly one unsettled node — the same pressure a spawn adds —
so it faces the same concurrency gates. Width binds at the dotted parent branch,
and the structural depth cap and spawn-time budget check are not repeated (see
[[features/spawning/tree_limits|tree_limits]]).
