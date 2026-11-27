---
name: features/tui
desc: |
  The cockpit TUI opened by the fractal open command: a four-pane live view
  of the whole tree, radio and chat from the keyboard, and a read-only window
  onto any node's session.
created: 2026-07-21T05:08:54Z
updated: 2026-11-27T22:49:51Z
---

# features/tui

[[features/_index|..]]

[[features/tui/actions|actions]]: The node actions available from the cockpit:
explicit radio writes acting as the user node, a read-only attach onto a node's
live tmux session, and the chat door into the message pane.

[[features/tui/panes|panes]]: The cockpit's four-pane grid -- tree, radio, node,
and message -- the focus-ring navigation that moves between them and drills into
each pane's interior zones, and the keyboard mode machine behind it.

[[features/tui/polling|polling]]: How the cockpit stays live: cheap mtime change
detection over the tree, an off-thread snapshot builder with per-branch caches,
and panes that render only from the immutable snapshot.

***

`fractal open` opens the cockpit: a single-screen, four-pane terminal UI over
the whole node tree (`fractal open [name]`, with `--path`, `--light`/`--dark`).
The one argument takes either name: a tree's root branch opens that tree at its
root, a node branch opens the tree owning it focused on that node. It is an
observer's surface -- everything on screen renders from one live snapshot, and
its only writes are the explicit radio, chat, and read-receipt actions the user
takes.

- [[features/tui/panes]] -- the tree / node / radio / message grid and the
  focus-ring navigation across and within panes.
- [[features/tui/polling]] -- how the display stays live: mtime change
  detection, cached snapshot builds off-thread, zero-query steady ticks.
- [[features/tui/actions]] -- what the cockpit can do to nodes: radio writes as
  the user node, read-only session attach, the chat door, and the lifecycle
  verbs it deliberately leaves to the CLI.

The chat surface the cockpit fronts is specified in
[[features/chat/_index|chat]].
