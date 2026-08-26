## Resume Mode

This node was paused mid-run and has been resumed (`$RESUME_MODE` is `true`);
the run continues where it left off -- same budgets, same iteration count. The
worktree was **not** cleaned: uncommitted changes are the frozen mid-step state
from before the pause (possibly from another machine). If your session carries
the interrupted context, simply continue the work in progress. If this is a
fresh session, re-orient before acting: survey the uncommitted changes
(`git status`, `git diff`), memory (`$MEMORY_DIR`), prior plans in `$PLANS_DIR`,
and the project wiki (`$WIKI_DIR`), then adopt the partially completed work and
carry it forward -- do not restart it.

Your prompts also carry a harness re-read of your unread inbox: the resumed plan
is frozen context, and directives that arrived after it froze supersede it.
Triage the re-read before executing any replayed decision -- especially spawns,
which a countermand may have barred.
