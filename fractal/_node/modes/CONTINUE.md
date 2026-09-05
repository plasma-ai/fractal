## Continue Mode

This node was continued (`$CONTINUE_MODE` is `true`); the worktree was cleaned
of uncommitted changes before the first iteration, so you start from the last
committed state. Rebuild context within the current assignment's read and write
boundaries: your own memory (`$MEMORY_DIR`), prior plans in `$PLANS_DIR`, and
relevant project wiki material (`$WIKI_DIR`) only where permitted. A frozen
subject remains the input; continuation does not authorize live source reads,
excluded context, or opening sealed message channels. If you spawned child nodes
previously, decide what to do with each (see the `fractal` skill's Continue mode
section).
