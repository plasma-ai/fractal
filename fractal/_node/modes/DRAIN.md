## Drain Mode

This run is a drain (`$DRAIN_MODE` is `true`): close out, never expand. The
harness refuses `fractal node init`, `fractal node start`,
`fractal node update`, and `fractal node resume` from this run's seats -- plan
no spawns, no child restarts, no cap re-arms, and no subtree wake-ups. Land the
work in flight, retire or hand off what cannot land, file your records and
sign-offs, and drive toward `fractal node finish`.
