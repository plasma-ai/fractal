## Sync

This mode does not expand the commission's read, write, frozen-input,
communication, or action boundaries. Skip excluded or sealed channels and retain
permitted on-disk continuation when radio is outside the assignment. The
following communication procedures apply only where authorized.

Check radio and act on anything that needs a response. An empty inbox and feed
is **not** a reason to go quiet: if your state has materially changed since your
last report -- real progress, a decision, or a blocker -- post a brief update to
your outbox before moving on. Otherwise, move on quickly. This is the per-step
pass; reading semantics, reply routing, and message discipline live in the
`radio` skill.

**Read.** `fractal radio read --channel=inbox --unread`, then
`fractal radio read --feed --unread` (subscriptions). Triage by urgency: parent
directives first.

**Act on parent directives.** Parent directives take priority -- execute them
before moving on. If a directive is unclear, send a question to the parent's
inbox.

**Respond and communicate.** Reply when your response carries content -- an
answer, a decision, a counter (`fractal radio reply`); react to acknowledge the
rest (`fractal radio react <uuid> +`). A question in your inbox always gets a
reply, never just a react. Any message from your inbox or feed is acted on
directly by its UUID. Report to your parent via outbox, steer children via their
inbox (`--node=<branch>`).

**Message the user (skip unless your parent is the user node).** The user reads
their inbox; outbox posts reach them only if they pull the feed. Send anything
worth the human's attention -- progress on what they asked for, questions,
decisions they own -- to their inbox
(`fractal radio send "<update>" --parent --subject="<subject>" --priority=<0-10>`).
Above all, answer a user message with `fractal radio reply <uuid>` -- the reply
routes to their inbox; an outbox post answering it does not.

**Steer children (skip if you have no children).** For each running child: check
status (`fractal node list`), read its outbox via feed, and assess whether it
needs redirection. Radio directives are your primary steering tool --
course-correct, ask questions, set priorities. When a child's overall direction
needs recalibrating, propose the change through the permitted channel. Revise
its NODE.md, steps, or other structural steering only with authority and before
launch or after confirming it is fully stopped; a paused node is not that
boundary. If a child is stuck, off-track, or done, redirect, kill, or merge only
within the current authority.

**Private channel.** Your private channel is your notes to your future self.
Read the new ones with `fractal radio read --channel=private --unread`
(`fractal radio messages --channel=private --all` lists the metadata). Write new
notes to carry context forward
(`fractal radio send "<note>" --node=$CURRENT_BRANCH --channel=private --subject="<subject>" --priority=<0-10>`).

**Save and preserve.** If a message's work will outlive this iteration,
`fractal radio save <uuid>` it now -- the saved set is your cross-iteration
action queue, and it is what survives when a budget boundary cuts the run. Every
sync, review it (`fractal radio messages --saved`) and unsave each item when its
work is done. If something is crucial to preserve long-term, write it to memory
(`$MEMORY_DIR`) or the project wiki (`$WIKI_DIR`) -- do this sparingly.
