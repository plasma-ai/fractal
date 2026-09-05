## Drain Mode

This run is a drain (`$DRAIN_MODE` is `true`): close out, never expand. The
harness refuses `fractal node init`, `fractal node start`,
`fractal node update`, and `fractal node resume` from this run's seats -- plan
no spawns, no child restarts, no cap re-arms, and no subtree wake-ups. Preserve
and hand off useful work within the current commission's read, write,
frozen-input, communication, and commit boundaries. Use `fractal node finish`
only when the actual Completion Requirements are met. Otherwise report the
unresolved deliverable and stop; a drain does not turn partial work into
completion.
