# X-Memory Alignment

X-Memory Alignment is an Agent Skills-compatible workflow for comparing and selectively migrating existing Markdown guidance between Claude Code and Codex.
Its inputs are user-maintained instructions, preferences, and project notes.
It checks which information the receiving agent can use, identifies duplicates and conflicts, and presents proposed text additions in an editable approval checklist.
Only explicitly approved additions are written to the receiving agent's native memory files.

Invoke the skill as `$x-memory-alignment`.
Its canonical directory is `skills/x-memory-alignment/`, and its default private state directory is `~/.local/state/x-memory-alignment`.
The `CAMD_*` environment variables and versioned provenance format remain supported.
The GitHub repository is [zycdev/x-memory-alignment](https://github.com/zycdev/x-memory-alignment).

The workflow reads existing text documents and produces reviewed text additions.
It does not train models or modify model parameters.
Source documents remain read-only, and project-specific information retains its scope.
Transcripts, credentials, settings, histories, caches, databases, and whole application-state directories are excluded.

## Why this project

Several existing projects provide shared stores, synchronization, or handoff mechanisms.
This repository focuses on reviewing existing guidance before it is added to another agent's native memory files.
The review checks usefulness to the receiving agent, duplicates, conflicts, project scope, and provenance.
Round-trip checks prevent information from being reimported into the agent from which it originated.

Design references include:

- [Agent Skills specification](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)
- [OpenAI: Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Anthropic: Extend Claude with skills](https://code.claude.com/docs/en/slash-commands)
- [Anthropic: Manage Claude's memory](https://code.claude.com/docs/en/memory)
- [agentmemory](https://github.com/jayzeng/agentmemory), a shared external memory CLI
- [codex-handoff](https://github.com/xu-88/codex-handoff), a cross-agent skill example
- [cross-agent-memory](https://github.com/VladimirGutuev/cross-agent-memory), a repository-local shared-memory approach
- [cross-agent-memory-kit](https://github.com/internetyev/cross-agent-memory-kit), an engine-and-adapter architecture

## Core behavior

1. Inventory only allowed semantic memory sources.
2. Compare them with the target agent's memory and project instructions.
3. Exclude transient, target-irrelevant, product-specific, duplicate, or unsafe
   content.
4. Present useful novel and conflicting candidates in an editable checklist.
5. Stop until the user explicitly approves the edited file.
6. Write through the target-native adapter, preserving scope and provenance.
7. Verify the result and suppress round-trip echoes on future runs.

Checklist prose follows the current conversation and user language. Stable
machine-readable comments make validation independent of the visible language.

## Repository layout

```text
skills/x-memory-alignment/
  SKILL.md
  references/
    protocol.md
    checklist-schema.md
    improvement-loop.md
    adapters/
  scripts/
    alignment_guard.py
    claude_target.py
    codex_target.py
    improve_log.py
    improve_sync.py
scripts/install_skill.py
tests/fixtures/
.github/
CONTRIBUTING.md
CHANGELOG.md
```

The canonical skill lives under `skills/` and is exposed through the global
installer. The repository intentionally carries no project-local discovery
links: combining them with a global installation creates duplicate same-name
entries in Codex and makes Claude Code's personal-over-project precedence hide
the working-tree copy. Use `link-working` explicitly while developing and
`link-release` or `copy` for ordinary use.

## Installation

Clone the repository, then use the installer. It resolves each agent's skill
directory, prints what it would do, and never contacts the network.

```bash
python scripts/install_skill.py status                      # what is installed now
python scripts/install_skill.py install --mode copy          # dry run by default
python scripts/install_skill.py install --mode copy --yes    # apply
python scripts/install_skill.py verify
```

Destinations are discovered rather than assumed: `$CLAUDE_CONFIG_DIR/skills`
(default `~/.claude/skills`) for Claude Code, `$CODEX_HOME/skills` (default
`~/.codex/skills`) for Codex, and `~/.agents/skills` only when that directory
already exists.

Three modes:

| Mode | What it does | Use when |
| --- | --- | --- |
| `copy` | Copies the skill into each destination | You want the installed skill frozen and independent of this clone |
| `link-release` | Maintains a detached worktree of a release ref and links the agents at it | You develop the project and want daily use to run a known ref |
| `link-working` | Links the agents at the working tree | You are dogfooding changes as you make them |

Do not add `.agents/skills/` or `.claude/skills/` links for this skill. The
installer is the single discovery entrypoint; switching its mode makes the
active source explicit instead of relying on host-specific scope precedence.

By default, the installer never modifies a destination it did not create. If one
exists, it reports a conflict and stops; an explicit `--force-replace` moves the
existing entry aside with a timestamped name and reports where it went. A
correct pre-existing link may instead be adopted without replacing it.
`uninstall` removes only entries the installer created or explicitly adopted.

In this version, every `link-release` installation shares one worktree, so both
agents run the same version by default. Reinstalling a single target at a
different ref moves that worktree for all of them, and `verify` then reports the
other target as drifted although nothing was done to it. To change the release
ref, reinstall every `link-release` target to the same ref in one operation;
reinstalling only the target named in the drift report merely moves the drift to
the other one. Never edit the receipt to silence the report. Per-target refs
would be a different installation model and are not part of this version.

## Improvement loop

The skill keeps a private, offline queue of defects, friction, ideas, and
surprises about itself, described in
[`references/improvement-loop.md`](skills/x-memory-alignment/references/improvement-loop.md).

```bash
python skills/x-memory-alignment/scripts/improve_log.py \
  add --kind friction --component install --title "..." --body "..." \
  --initiated-by user-request
python skills/x-memory-alignment/scripts/improve_log.py status
python skills/x-memory-alignment/scripts/improve_log.py draft --out packet.md
```

Capture is local, offline, and private: reports are stored outside the repository
under `$XDG_STATE_HOME`, owner-readable only, and the tool imports no network
library and runs neither `git` nor `gh`. A report is written only when the user
asks for one or accepts an offer to record one.

Publication is an explicit maintenance operation through the separate
`improve_sync.py` helper. It requires a verified GitHub remote, credential, and
write permission, both `--maintenance` and `CAMD_MAINTENANCE=1`, an explicit
`--report` selection for publication, and `--yes` for any mutation. Without
those preconditions it reports the reason and changes nothing:

```bash
CAMD_MAINTENANCE=1 python \
  skills/x-memory-alignment/scripts/improve_sync.py \
  plan --maintenance --cycle 1
```

## Development

[`CONTRIBUTING.md`](CONTRIBUTING.md) describes how to propose a change, the
boundaries every change must preserve, and how reports reach this repository.

Run the committed tests with:

```bash
python -m unittest discover -s tests -v
```

Every test builds its own temporary roots. Tests against live memory require a
separate user-approved checklist and must never be committed with real memory
content.

## Status

The initial public release is available, and the current branches contain the updated naming and guidance workflow.
See [`CHANGELOG.md`](CHANGELOG.md) for the changes.

## License

MIT
