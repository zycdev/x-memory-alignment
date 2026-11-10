# Changelog

All notable changes to X-Memory Alignment are documented here.

## Unreleased

### Changed

- Rename the skill and project directory to `x-memory-alignment` and update
  installation destinations, documentation, and test imports.
- Rename the GitHub repository to `zycdev/x-memory-alignment` and update
  repository links and the local remote URL.
- Describe the workflow as comparison, conflict review, and selective migration
  of existing Markdown guidance throughout both maintained branches.
- Rename the shared validation module to `alignment_guard.py`, use alignment
  terminology in generated notes and managed section markers, and move the
  default private state directory to `x-memory-alignment`.
- Preserve versioned fingerprints and provenance for duplicate and round-trip
  checks, and retain explicit private state overrides for existing installations.

## 0.1.0 - 2026-08-23

Initial public release.

### Added

- One Agent Skills-compatible workflow shared by Claude Code and Codex.
- Bidirectional, target-native adapters for Claude-to-Codex and
  Codex-to-Claude memory alignment.
- Editable, language-adaptive approval checklists with strict mechanical
  validation before every live write.
- Scope preservation, target-usefulness filtering, versioned fingerprints,
  provenance, source snapshots, idempotence, and round-trip echo protection.
- A no-network installer with `copy`, `link-working`, and `link-release` modes,
  ownership receipts, non-destructive conflict handling, and verification.
  Global installation is the single discovery entrypoint: the project carries no
  same-name project-local discovery links, which would otherwise duplicate the
  skill in Codex and let Claude Code's personal-over-project precedence hide the
  working-tree copy. All `link-release` installations share one worktree, so
  verification reports a release target whose worktree has moved away from the
  commit in its receipt and asks for every release target to be brought to one
  ref in a single operation.
- A private offline improvement queue with occurrence deduplication, triage
  packets, mechanical redaction findings, and corruption isolation. Private
  files are created exclusively, are not followed through a symbolic link, and
  leave no partial file behind when a write fails.
- A maintenance-only GitHub synchronization helper for reviewed reports, with
  read-only authorization preflight, explicit report selection, dry runs,
  marker-based reconciliation, maintainer-edit divergence detection, managed
  label-family reconciliation, and evidence-gated closure.
- Public issue forms, managed labels, a pull-request template, and contribution
  guidance.

### Safety boundaries

- The skill transfers durable semantic guidance, not transcripts, credentials,
  settings, histories, caches, databases, or whole memory directories.
- Every target-memory write stops at a user-editable checklist approval gate.
- Ordinary skill use and improvement capture do not contact the network.
- Mechanical redaction detects identifiable classes only; agents and users must
  still review prose semantically before publication.
- A report body is private material until both a mechanical scan and a semantic
  review have been done. A reviewed body is kept indefinitely, the private
  evidence block of a report resolved or dropped for thirty days should be
  removed, and nothing removes either automatically.
- GitHub markers identify the same report across runs but do not detect
  semantically similar reports written independently.

### Compatibility note

Agent memory formats are product-owned surfaces and may evolve. The adapters
therefore fail closed on unknown layouts or schema values instead of guessing;
future product changes may require a tested adapter update.
