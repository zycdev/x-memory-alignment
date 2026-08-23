---
name: cross-agent-memory-distiller
description: Distill durable, target-useful memory between Claude Code and Codex by comparing source and target memory, producing an editable approval checklist, and applying only approved novel or conflicting guidance through target-native adapters. Use for Claude-to-Codex or Codex-to-Claude memory consolidation, and to record a defect, friction, or improvement idea about this skill when the user asks; do not use for transcripts, credentials, settings, caches, databases, or wholesale state synchronization.
---

# Cross-Agent Memory Distiller

Transfer semantic guidance, not product state. The receiving agent should gain
only durable memory that it can use and that is absent from, or meaningfully
conflicts with, its current memory.

## Select the direction

Identify the source and receiving agent from the request. Read:

1. [the shared protocol](references/protocol.md);
2. [the checklist schema](references/checklist-schema.md); and
3. exactly one target adapter:
   - [Claude Code target](references/adapters/claude-code.md)
   - [Codex target](references/adapters/codex.md)

If the direction, target memory scope, or actual project mapping cannot be
established safely, stop and ask the user. Never infer project identity from a
basename alone.

## Review phase

1. Inventory only allowed Markdown memory sources and leave them unchanged.
2. Inspect target memory and applicable project instructions for comparison.
3. Reject content that is transient, unsafe, unverified, target-irrelevant, or
   specific to a source capability with no target equivalent.
4. Deduplicate semantically. Retain only useful `novel` and `conflict`
   candidates.
5. Preserve source scope. Resolve project identity using actual paths and
   repository identity.
6. Filter candidates whose provenance shows that they originated from the
   receiving agent.
7. If inventory reports historical unprovenanced notes, follow the origin-review
   procedure in `protocol.md` and record the classifications in the checklist.
8. Write one editable checklist in a temporary, private location. Use the
   current conversation and user language for visible prose; keep hidden schema
   metadata language-independent. Recommendations may be pre-checked.
9. Report the exact path and stop. Checklist creation is not permission to
   change memory.

For the Codex-to-Claude direction, `scripts/claude_target.py inventory`,
`show-source`, `align-project`, and `snapshot-source` perform the mechanical part
of steps 1, 2, and 5 with explicit roots. Selecting, translating, and rejecting
candidates remains the agent's judgment.

For the Claude-to-Codex direction, use the matching commands in
`scripts/codex_target.py`. Its target inventory reports unprovenanced legacy
notes that require semantic origin review before a reverse-direction live test.

## Apply phase

Continue only after the user explicitly says the checklist is ready.

1. Re-read the exact file. Treat checkbox changes, rewritten text, and user
   annotations as authoritative.
2. Validate structure with `scripts/distill_guard.py validate-checklist`.
3. Recheck selected items for target usefulness, duplicates, conflicts,
   provenance loops, sensitive material, transient content, and unresolved
   annotations.
4. Apply only surviving checked items through the target adapter. If none
   remain, make no memory write. For a Claude target, use
   `scripts/claude_target.py apply --checklist <path> --claude-home <root>`,
   inspecting the plan with `--dry-run` first. For a Codex target, use
   `scripts/codex_target.py apply --checklist <path> --codex-home <root>`, also
   with `--dry-run` first and one explicit `--project-root` for every approved
   project scope.
5. Re-read and audit the result with `scripts/claude_target.py verify`. Confirm
   correct scope and source immutability. Use `scripts/codex_target.py verify`
   for a Codex target.
6. Delete the exact checklist only after the full write and verification
   succeed. Retain it on any failure and report recovery information.

## Recording an improvement

This skill keeps a private, offline queue of defects, friction, ideas, and
surprises about itself. See [the improvement loop](references/improvement-loop.md).

- Record only when the user asks, or after the user accepts an offer to record.
  `scripts/improve_log.py add` requires `--initiated-by user-request` or
  `--initiated-by accepted-suggestion`, so unprompted capture is not possible.
- Offer at most once per session, in one sentence, and never again after a
  refusal.
- Recording writes only to the private state directory. Ordinary use never
  modifies this repository and never contacts the network.
- `scripts/improve_log.py status` may report that maintenance is recommended.
  Surface it in one sentence; never start maintenance work unasked.
- Publication is a separate maintenance operation through `scripts/improve_sync.py`,
  the only helper allowed to invoke GitHub. Read `improvement-loop.md` before
  using it. Every command requires both an explicit maintenance flag and the
  maintenance environment marker, mutations require `--yes`, and missing or
  unverified remote/authentication/write-permission state causes a read-only
  refusal. Publishing also requires an explicit user-approved `--report`
  selection; an unfiltered plan cannot be applied.

## Boundaries

- Never read or migrate transcripts, session records, histories, credentials,
  settings, caches, databases, lock files, or generated product state.
- Never upload memory content as part of this workflow.
- Never silently resolve conflicting guidance.
- Never promote project evidence to global scope.
- Never copy source-agent-only metadata or behavior that cannot help the target.
- A dry run, inspection, or checklist request authorizes no target mutation.
- Improvement reports describe this tool, never the user's work. They are not
  memory, are never imported as memory candidates, and live outside both agents'
  memory surfaces.
