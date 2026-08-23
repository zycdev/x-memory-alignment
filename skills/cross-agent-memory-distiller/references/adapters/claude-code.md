# Claude Code Target Adapter

This contract defines the required behavior before platform-specific code is
added. Review current official Claude Code memory documentation and the live
installation before relying on paths or formats.

## Source side: Codex

Discover Codex's supported semantic memory registry and only the topic or
extension notes it references. Project instructions may be read for comparison.
Do not ingest session history, rollout transcripts, SQLite databases, caches,
settings, generated state, or unrelated skill files.

Treat Codex-generated indexes as discovery inputs, not content to copy verbatim.
Follow references narrowly and retain scope and origin provenance.

## Target comparison: Claude Code

Compare candidates with applicable user instructions, project instructions,
and project auto-memory. These sources may already express a rule even when the
wording differs. Filter Codex-only runtime mechanics that do not help Claude.

Global guidance maps only to Claude's supported user-level instruction memory.
Project guidance maps only to the auto-memory for the confirmed actual project.
Do not invent a global auto-memory tier if the installed Claude version does not
provide one.

## Required implementation

The adapter must provide and test:

- safe source and target discovery with configurable roots for isolation;
- actual-path and repository-identity project alignment;
- comparison hooks that give the agent enough context for relevance and
  semantic deduplication without dumping whole memory trees;
- checklist generation using the shared language-neutral metadata schema;
- additive or atomic application of only approved items through current
  Claude-native memory locations;
- concise index maintenance when Claude's native project memory requires it;
- provenance storage, idempotence, rollback, and source-immutability checks;
- synthetic and temporary-HOME end-to-end tests.

Never apply live changes during implementation tests. Live acceptance is a
separate user-approved phase.

## Implemented behavior

`scripts/claude_target.py` implements this contract. Every command requires
explicit roots; the adapter has no default that could reach a live installation.

| Command | Purpose |
| --- | --- |
| `inventory --codex-home --claude-home [--project-root]` | Index-level description of both sides, with exclusion counts and the fingerprints already applied. |
| `show-source --codex-home --path` | Read one source file, refusing any path outside an allowed role. |
| `align-project --claude-home --project-root [--source-root]` | Resolve the project to its Claude memory directory and state the mapping label. |
| `snapshot-source --codex-home --out` | Record source digests for the immutability check. |
| `fingerprint --file/--stdin` | Fingerprint normalized meaning rather than raw bytes. |
| `apply --checklist --claude-home [--project-root ...] [--dry-run] [--create-project-memory] [--acknowledge-warnings]` | Apply approved items, then verify or roll back. |
| `verify --claude-home [--project-root ...] [--codex-home --source-manifest]` | Audit provenance, scope labels, and source immutability. |

`--project-root` may be repeated. Every approved project candidate must name a
target root that appears among these explicitly confirmed roots, so a checklist
cannot direct a write into a project the user did not confirm in this run.

### Source roles under the Codex home

- `registry`: `memories/MEMORY.md`. Discovery input only; never copied verbatim.
- `candidate`: `memories/extensions/ad_hoc/notes/*.md`. The only importable
  content.
- `context`: `AGENTS.md` and `memories/extensions/ad_hoc/instructions.md`, read
  for comparison.
- `excluded`: everything else, reported as counts and reasons only. Rollout
  summaries are session-derived; `raw_memories.md` and `memory_summary.md` are
  generated derivatives of the registry; skills are not memory; sessions,
  histories, databases, caches, and credentials are outside the memory surface.

`show-source` enforces these roles, so an excluded path cannot be read through
the adapter even if it is requested by name.

The inventory also reports origin evidence for candidate notes. A note written by
this workflow carries `camd:provenance`, so a note that originated at Claude is
flagged mechanically in `echo_risk_notes`. A note that predates the workflow, or
that a person wrote by hand, carries no provenance: `unprovenanced_notes` and
`historical_origin_review_required` mark that its origin can be established only
by reading it. A manual cross-agent merge performed before this project existed
is exactly such a note, and it would otherwise look like ordinary Codex guidance.

### Target locations

- Global candidates append to `<claude-home>/CLAUDE.md` inside a managed section
  delimited by `camd:distilled` markers. Existing user text is never rewritten.
- Project candidates create one memory file per candidate in
  `<claude-home>/projects/<slug>/memory/`, in the native front-matter format
  (`name`, `description`, `metadata.node_type`, `metadata.type`,
  `metadata.modified`), and add one line to that directory's `MEMORY.md` inside a
  managed index section.
- The slug is derived from the resolved project root, never from a basename. The
  target memory directory must already exist unless `--create-project-memory` is
  passed, so an unconfirmed mapping cannot silently create a new project.
- No global auto-memory tier is used, because the installed Claude version does
  not provide one.

### Optional placement hints

A candidate may carry `target_hint` in its metadata. All fields are optional and
no other adapter is required to produce them:

```json
{"target_hint": {"name": "reproduction-method", "memory_type": "feedback",
                 "title": "Archive the reproduction method",
                 "description": "One line for the memory index."}}
```

`memory_type` must be one of `user`, `feedback`, `project`, or `reference`, and
defaults to `feedback`. Without hints, the adapter derives the file name, title,
and description from the approved text.

Hints are metadata rather than approved text, so the guard does not scaffold-check
them. The adapter therefore validates `name` against
`^[a-z0-9][a-z0-9_-]{0,63}$`; underscores and hyphens match the installed
`feedback_<topic>` and `project_<topic>` conventions, while path separators,
traversal, uppercase letters, spaces, and the name reserved for the memory index
remain refused. It also rejects any rendered file, index entry, or global block
that still contains an unresolved annotation. The check covers only what the run
renders; pre-existing target content is never reclassified as new output.

### Guarantees

- Application is idempotent: a candidate whose fingerprint already appears in a
  target provenance record is skipped and reported, so repeated runs converge.
- Writes are staged and committed atomically per file, and any failure, including
  a failed post-write verification, restores every touched file and removes files
  the run created.
- Provenance records carry the candidate identity, agents, scopes, normalization
  version, fingerprint, applied timestamp, repository identity, and the shared
  project key, but never resolved private paths.
- Writing preserves an existing file's permissions and creates new files with the
  umask default, so applying memory never silently narrows access to a file the
  user already had.
- Warnings from the shared guard must be acknowledged explicitly with
  `--acknowledge-warnings`; they never pass silently.
- The adapter opens the Codex home read-only, and `verify --source-manifest`
  proves the source did not change during the run.
