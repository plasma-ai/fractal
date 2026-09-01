---
name: source_map
desc: |
  A mapping from source paths to the wiki pages that document them, so an
  agent editing a module can find its page in one hop.
created: 2026-07-21T04:35:35Z
updated: 2026-07-21T04:35:35Z
---

# source_map

***

The wiki is organized by concept and feature surface, not by source layout. This
table maps each source path to the branch that documents it, ordered by source
path so a module is found by scanning. Owned by the wiki root.

| Source path                                                                                        | Wiki branch                                                                                                                           |
| -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `fractal/cli/main.py`, `fractal/cli/utils.py`                                                      | [[architecture/packages]]                                                                                                             |
| `fractal/cli/cmd/channel.py`, `fractal/cli/cmd/radio.py`                                           | [[features/radio/_index\|features/radio]]                                                                                             |
| `fractal/cli/cmd/config.py`                                                                        | [[configuration/config_json]]                                                                                                         |
| `fractal/cli/cmd/cost.py`, `fractal/cli/cmd/time.py`                                               | [[features/cost/_index\|features/cost]]                                                                                               |
| `fractal/cli/cmd/db.py`, `fractal/cli/cmd/event.py`                                                | [[architecture/database]]                                                                                                             |
| `fractal/cli/cmd/fractal.py` (init, reset, destroy, top-level commands)                            | [[user_flow/_index\|user_flow]], [[features/lifecycle/_index\|features/lifecycle]]                                                    |
| `fractal/cli/cmd/node.py`                                                                          | [[features/lifecycle/_index\|features/lifecycle]], [[features/spawning/_index\|features/spawning]]                                    |
| `fractal/cli/cmd/plan.py`                                                                          | [[features/loop/_index\|features/loop]]                                                                                               |
| `fractal/constants.py`                                                                             | [[design/statuses]], [[features/lifecycle/_index\|features/lifecycle]]                                                                |
| `fractal/core/agent.py`, `fractal/impl/`                                                           | [[architecture/agent_providers]], [[features/agents/providers]], [[features/agents/models_and_effort]], [[features/agents/extending]] |
| `fractal/core/commit.py`                                                                           | [[features/loop/commit_pipeline]]                                                                                                     |
| `fractal/core/config.py`                                                                           | [[configuration/config_json]], [[configuration/inheritance]]                                                                          |
| `fractal/core/cost.py`                                                                             | [[features/cost/measurement]], [[features/cost/budgets]]                                                                              |
| `fractal/core/db.py`, `fractal/core/record.py`, `fractal/core/event.py`, `fractal/core/schema.sql` | [[architecture/database]]                                                                                                             |
| `fractal/core/files.py`                                                                            | [[features/files/contribution]], [[features/files/anchors_and_history]], [[features/files/path_validation]]                           |
| `fractal/core/loop.py`                                                                             | [[features/loop/steps]], [[features/loop/prompt_assembly]], [[features/loop/accounting]]                                              |
| `fractal/core/node.py` (lifecycle, spawn limits), `fractal/_scripts/`                              | [[architecture/node_tree]], [[features/lifecycle/_index\|features/lifecycle]], [[features/spawning/_index\|features/spawning]]        |
| `fractal/core/plan.py`                                                                             | [[features/loop/plans]]                                                                                                               |
| `fractal/core/pricing.py`                                                                          | [[features/cost/pricing]]                                                                                                             |
| `fractal/core/radio.py`                                                                            | [[features/radio/_index\|features/radio]]                                                                                             |
| `fractal/core/render.py` (prompt assembly for loop and chat)                                       | [[features/loop/prompt_assembly]], [[features/chat/addressing]]                                                                       |
| `fractal/core/session.py`                                                                          | [[features/files/transcripts]]                                                                                                        |
| `fractal/core/time.py`                                                                             | [[features/cost/time_budgets]]                                                                                                        |
| `fractal/core/worktree.py`, `fractal/_assets/`                                                     | [[architecture/worktrees]]                                                                                                            |
| `fractal/exceptions.py`, `fractal/typing.py`, `fractal/util/`                                      | [[architecture/packages]]                                                                                                             |
| `fractal/tui/` (chat pane: `fractal/tui/chat.py` → chat)                                           | [[features/tui/panes]], [[features/tui/actions]], [[features/tui/polling]], [[features/chat/_index\|features/chat]]                   |
| `fractal/_node/` (seed: NODE.md, steps, scripts, skills, modes, agents)                            | [[configuration/steps]], [[configuration/scripts]]                                                                                    |
| `fractal/skills/fractal/` (the plugin skill driving the operator workflow)                         | [[user_flow/_index\|user_flow]]                                                                                                       |
| `shim/` (the metadata-only `fractal` pointer dist at the repo root)                                | [[architecture/packages]]                                                                                                             |

The `wiki` CLI itself is not fractal source — it ships in the separate `wiki`
package; [[features/wiki_system/_index|features/wiki_system]] documents how
fractal uses it.
