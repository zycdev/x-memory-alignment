# Codex Target Adapter

This contract is informed by the existing local `merge-claude-memories` workflow
but does not copy user-specific paths or private memory into this repository.
Review the current Codex memory mechanism before implementation.

## Source side: Claude Code

Discover allowed user-level Claude guidance and project auto-memory Markdown.
Use Claude project memory indexes to select relevant topic files. Read project
instructions for comparison when applicable, but do not treat every instruction
as an import candidate.

Do not read transcripts, session JSONL, prompt history, plans, settings,
credentials, caches, databases, lock files, or product metadata. Filter
Claude-only mechanisms that cannot guide Codex.

## Target comparison: Codex

Compare candidates with current Codex memory, referenced durable notes, and
applicable project instructions. Generated indexes are read-only discovery
inputs. Preserve target rules that are equal or more specific and expose
material conflicts in the checklist.

## Mechanical commands

`scripts/codex_target.py` requires explicit roots and provides:

- `inventory`: outline-level Claude sources and Codex targets, without dumping
  note bodies. Pass each shared project with `--project-root`.
- `show-source`: read exactly one path from the allowed inventory; forbidden
  paths and traversal are rejected.
- `align-project`: resolve the Claude storage slug, actual target root, Git
  identity, and mapping label.
- `snapshot-source`: write a private digest manifest before apply.
- `fingerprint`: compute the adapter's shallow normalized-meaning fingerprint.
- `apply`: consume the shared `parse_checklist` result and create one ad-hoc
  extension note. Always inspect `--dry-run` first.
- `verify`: audit provenance and optionally compare the source manifest.

The adapter has no implicit live paths. For project candidates, every
`project.target_root` must equal an explicitly supplied `--project-root`.
Creating a missing notes directory requires `--create-notes-dir`.

## Target write

One approved run creates at most one uniquely named note under:

```text
<codex-home>/memories/extensions/ad_hoc/notes/
```

The note has separate global and per-project sections. Every applied candidate
has adjacent compact `camd:provenance`; project provenance retains repository
identity, mapping, and the shared project key but omits private roots. A project
whose identity is `none` includes that key in its visible section heading, so two
unrelated local projects cannot collapse into one section. Fingerprints are
qualified by their normalization version before deduplication. Existing matches
are skipped, an all-skipped run is a successful no-op, and verification failure
removes the new note and any directories created solely for it.

New notes are created mode `0600` with `O_CREAT | O_EXCL`. This keeps memory
private and preserves create-only semantics without relying on hard links; an
existing path is never replaced.

Generated `MEMORY.md`, summaries, raw memory, databases, indexes, and internal
memory-repository state are never write targets.

## Historical provenance boundary

Notes created before this project may contain earlier Claude-to-Codex manual
merges without machine-readable origin. `inventory` reports their count and sets
`historical_origin_review_required`; it does not guess their origin. Before live
reverse-direction acceptance, an agent must compare those notes semantically and
exclude historical echoes from the checklist using the procedure in
`protocol.md`.

Never apply live changes during implementation tests. Live acceptance is a
separate user-approved phase.
