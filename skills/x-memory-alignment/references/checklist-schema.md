# Checklist Schema

The checklist is human-first Markdown with language-neutral HTML comments for
mechanical validation. Visible headings and labels may use any language.

Begin every checklist with one document marker:

```markdown
<!-- camd:checklist {"schema":"camd/v1","source_agent":"claude-code","target_agent":"codex"} -->
```

The source and target identify the direction. A checklist with no candidate
blocks is a valid no-op review result.

## Candidate block

Each candidate uses this shape:

```markdown
<!-- camd:candidate {"schema":"camd/v1","id":"CAMD-001","status":"novel","source_agent":"claude-code","target_agent":"codex","origin_agent":"claude-code","source_scope":"global","target_scope":"global","normalization":"camd-norm/v1","fingerprint":"sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"} -->
- [x] Include this candidate

Why it helps the receiving agent: ...

Existing target memory: ...

Recommended action: ...

<!-- camd:proposed:start -->
The exact durable text proposed for the target.
<!-- camd:proposed:end -->
<!-- /camd:candidate -->
```

For project scope, metadata also includes:

```json
{
  "source_scope": "project",
  "target_scope": "project",
  "project": {
    "source_root": "/resolved/source/root",
    "target_root": "/resolved/target/root",
    "git_identity": "host/owner/repository",
    "mapping": "same-root",
    "key": "0123456789abcdef"
  }
}
```

Checklist files are private temporary artifacts, so resolved paths may appear
there. Public reports must replace them with non-identifying path classes.

## Rules

- Candidate IDs are unique and stable within one run.
- `status` is `novel` or `conflict`.
- Source and target agents differ.
- `origin_agent` records the earliest known agent, not merely the immediate
  source. If it equals the target, the validator rejects the echo.
- A block contains exactly one approval checkbox and exactly one proposed-text
  region, and the checkbox precedes that region. The checkbox is counted only
  outside the proposed text, so proposed guidance may itself contain Markdown
  task syntax.
- Unchecked candidates are never applied.
- `normalization` names the fingerprint rule and must equal the current
  `alignment_guard.NORMALIZATION` value.
- Project candidates carry the shared `project.key` computed from the resolved
  target root. The validator checks it, so the user can distinguish projects
  whose Git identity is `none` before approving them.
- Checked candidates require non-empty proposed text and no unresolved revision
  request `【...】` and no marker-shaped scaffold: a line that opens with `TODO`
  or `TBD`, the forms `TODO:`, `TBD:`, and `TODO(...)`, or the uppercase
  standalone token `PLACEHOLDER`.
- An incidental mention of a scaffold word inside otherwise finished guidance is
  a warning rather than an error, because classifying prose is the agent's
  judgment. The applying agent must reconcile every warning explicitly, and
  `validate-checklist --strict` escalates warnings to failures.
- Fingerprints are unique within one checklist. Two selected candidates that
  share a fingerprint are an error; a collision involving an unselected candidate
  is a warning.
- Metadata supports validation but does not override the user's visible edits.
  The applying agent must reconcile any mismatch or annotation explicitly.
- Adapters may add optional platform-specific keys to candidate metadata, such as
  a target placement hint. Validators ignore unknown keys, and no adapter may
  require a key that the opposite adapter cannot produce.

## Output provenance

Target-native output should retain an adjacent record such as:

```markdown
<!-- camd:provenance {"schema":"camd/v1","records":[{"id":"CAMD-001","source_agent":"claude-code","target_agent":"codex","origin_agent":"claude-code","source_scope":"global","target_scope":"global","normalization":"camd-norm/v1","fingerprint":"sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}]} -->
```

Adapters may translate this into an equivalent native metadata representation,
but must keep enough information to detect duplicates and round-trip echoes.
Records may carry additional native fields, such as an applied timestamp.

`project.mapping` must be one of `same-root`, `confirmed-remote`, or
`user-mapping`. Adding a value requires a schema change that both adapters
implement, so that a mapping-confidence claim stays auditable.

A project-scoped provenance record needs only `project.git_identity` and
`project.mapping`, and new adapters add `project.key`, the shared digest of the
resolved target root, to separate projects that share a repository identity of
`none`. Provenance written before normalization was explicit may omit
`normalization`; it is interpreted as `camd-norm/v1`, while every newly rendered
record must name its version. The resolved roots are omitted on purpose: `protocol.md`
forbids writing private absolute paths into durable target memory when a stable
abstract identifier is sufficient. Checklists are private temporary artifacts and
must still carry the full mapping that the user reviews.
