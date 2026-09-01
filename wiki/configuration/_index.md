---
name: configuration
desc: |
  The complete configuration reference: every node initialization flag and
  config key, step files and their frontmatter overrides, the node scripts,
  and what children inherit.
created: 2026-07-21T04:35:35Z
updated: 2026-09-01T05:16:58Z
---

# configuration

[[_index|..]]

[[configuration/config_json|config_json]]: Every key a node's config file
carries: type, default, the init flag that sets it, which keys are immutable,
and the surfaces for reading and retuning configuration after init.

[[configuration/inheritance|inheritance]]: What a child node inherits from its
ancestors and how: the unconditional surfaces (agent, provider, root, local,
project, agent config), the opt-in inherit surfaces, and which keys never
inherit.

[[configuration/node_init|node_init]]: Every flag of the node creation command:
identity and placement, seeding and agent selection, tree limits, time and cost
budgets, execution modes, and the cross-flag rules enforced at initialization.

[[configuration/scripts|scripts]]: The three node scripts -- setup, test, and
lint: when each runs, what the loop and the commit pipeline do with their
results, and the rules for extending them per node.

[[configuration/steps|steps]]: The step files that define a node's iteration:
discovery and ordering rules, the seed step sequence, the SYNC pseudo-step,
every supported frontmatter override, and how to customize a node's step list.

[[configuration/templates|templates]]: Node seed templates: the tracked template
folder and its layout, the config preset, seed-time slots and their values, the
provenance record, the include/exclude effective set, the diff and reseed verbs,
and the guards on what a template may deploy.

***

This branch is the complete configuration reference: every knob a user can set
when creating and running nodes, enumerated and explained. Where the sibling
`wiki/user_flow/` branch shows the journeys, this branch is the lookup surface
-- any flag or key is findable here.

Configuration enters a node in one of five ways, and the pages map to them:

- [[configuration/node_init]] -- every `fractal node init` flag, grouped:
  identity and placement, seeding and agent selection, tree limits, time and
  cost budgets, execution modes, and the cross-flag rules.
- [[configuration/config_json]] -- every key the persisted `config.json`
  carries: type, default, the flag that sets it, the immutable keys, and the
  surfaces for reading and retuning after init (`fractal node config`,
  `fractal node update`, direct edits).
- [[configuration/templates]] -- seeding a node from any tracked folder: the
  layout and the config preset, seed-time slot values, the provenance record,
  and the `node diff` and `node reseed` verbs that keep a node true to it.
- [[configuration/steps]] -- the step files that define an iteration: discovery
  and ordering, the seed sequence, the SYNC pseudo-step, the frontmatter
  override keys, and how to tailor a node's step list.
- [[configuration/scripts]] -- the three node scripts (setup, test, lint): when
  each runs, what the loop and the commit pipeline do with the results, and the
  extension rules.
- [[configuration/inheritance]] -- what children inherit and how: the
  unconditional surfaces, the opt-in `--inherit` surfaces, and the budget-class
  keys that never flow down.
