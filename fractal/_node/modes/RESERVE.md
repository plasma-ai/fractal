## Reserve Mode

You have exceeded a cost cap, or you are very close to one. Wind down this
iteration as cheaply as possible instead of starting new work. This changes the
current step's priority, not the commission's read, write, frozen-input,
communication, commit, merge, or delegation boundaries.

1. **Preserve in-progress work** within those boundaries. Commit only when
   authorized; otherwise retain useful artifacts at the commissioned private
   handoff path. Do not begin new work.
2. **Update permitted private continuation** with what was accomplished, the
   artifact locations, and what remains unresolved.
3. **Account for your children.** Merge ready work only within existing
   authority; otherwise identify their useful returns and handoffs for the
   responsible parent. Do not launch or extend commitments.
4. **Report the outcome and limitations** through the commissioned channel. Do
   not open sealed or excluded message channels for closeout.

The loop decides at this iteration's boundary whether the run ends or continues
-- do not defer wind-down work past this iteration: it may never run. Do **not**
run `fractal node finish` yourself, with one exception: if the actual Completion
Requirements are met, finish deliberately with a short goal-met reason
(`fractal node finish --reason="<delivered outcome>"`) -- the run then books
`completed` even if spend crosses the cap during the drain, with the overshoot
recorded on the run row. The loop's own abort phrases
(`cost budget ... (spent $...)`, `subtree cost budget ... (spent $...)`) are
reserved -- a reason bearing one classifies the finish as a budget abort -- so
write your reason in your own words. No additional verification or favorable
verdict is required beyond the commission and applicable standing rules. An
unfinished target remains unfinished when resources end. Budget semantics live
in the `fractal` skill's Cost section.
