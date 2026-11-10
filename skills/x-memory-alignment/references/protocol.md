# Alignment Protocol

This protocol is platform-neutral. Target adapters define storage discovery and
native writes. Semantic decisions remain with the agent; helper scripts validate
mechanical invariants only.

## 1. Authorization states

The workflow has two separate authorization states:

- **Review:** inspect allowed sources, compare, and create a checklist.
- **Apply:** write selected memory only after the user edits the checklist and
  explicitly confirms it is ready.

A request to inspect, compare, dry-run, or create a checklist grants review
authorization only. Never prepare or perform live writes speculatively.

## 2. Allowed and forbidden inputs

Allowed inputs are user-authored or agent-maintained Markdown that contains
semantic guidance, together with target instructions needed for comparison.
Start from indexes and open detailed topic files only for plausible candidates.

Forbidden inputs include transcripts, session JSONL, prompt history, credentials,
settings, caches, databases, lock files, telemetry, generated indexes treated as
write targets, and wholesale application state. Reading an index for discovery
does not authorize rewriting it.

## 3. Candidate gates

Evaluate each source idea, not each file, through all gates:

1. **Durable:** likely useful beyond the current task or run.
2. **Supported:** explicitly stated or backed by reliable evidence; preserve
   uncertainty and conflicts.
3. **Target-useful:** changes how the receiving agent can make a future decision
   or execute a supported workflow.
4. **Adaptable:** has a meaningful target concept. Translate terminology when
   safe; reject source-only mechanisms without a target analogue.
5. **Scope-correct:** global stays global; project stays with the aligned project.
6. **Non-echo:** provenance does not show origin at the receiving agent.
7. **Non-duplicate:** not already represented by equal or more specific target
   memory or instructions.

Classify survivors as:

- `novel`: useful and absent from the target;
- `conflict`: useful but inconsistent with target memory in a way requiring a
  user decision.

Do not create candidates for exact or semantic duplicates, superseded guidance,
source-only product details, transient plans, one-off commands, stale paths,
secrets, or unverified guesses. Summarize exclusion counts and reasons without
copying rejected sensitive content.

### Historical notes without provenance

Before a live run in either direction, inspect the source inventory fields
`unprovenanced_notes`, `historical_origin_review_required`, and any
`echo_risk_notes`. If origin review is required:

1. Open only plausible semantic notes through the adapter's allowed
   `show-source` surface; do not broaden discovery to histories or generated
   state.
2. Compare each note with the opposite agent's current memory and classify its
   origin as `source-native`, `known-target-origin`, or `unknown`.
3. Exclude `known-target-origin` notes as historical echoes. Do not propose an
   `unknown` note automatically; disclose it in the private checklist and ask
   the user if its origin affects an otherwise useful candidate.
4. Record the reviewed note names, classifications, and exclusion counts in the
   private checklist. Never retrofit provenance into a source note merely to
   make the run pass.

This is a semantic agent judgment. The adapters report evidence and enforce
future provenance, but do not guess the origin of legacy prose.

## 4. Comparison and conflicts

Compare meaning rather than wording. A narrower target rule may coexist with a
broader source rule if scopes differ. A newer statement does not automatically
win: show material conflicts and recommend an action with a reason.

Conflict recommendations may be `keep-target`, `use-source`, `merge`, or
`keep-both-scoped`. The visible checklist must show the current target meaning,
the proposed result, and the consequence. The user's edited proposed text is the
only text eligible to be applied.

## 5. Project identity

Resolve paths before comparing projects. Prefer, in order:

1. the same resolved repository root on the current host;
2. matching Git remote identity plus a user-confirmed path mapping;
3. explicit user mapping when repositories have no stable remote.

Never align projects by basename alone. Put source root class, target root class,
repository identity, and mapping confidence in the checklist. Do not expose
private absolute paths in committed reports.

## 6. Checklist gate

Use the structure in `checklist-schema.md`. Visible prose follows the user's
language. Stable HTML comments carry language-neutral validation metadata.

The checklist includes source inventory, target inventory, project mappings,
historical-origin review when required, exclusion summary, and every `novel` or
`conflict` candidate. Pre-check items when there is a clear recommendation;
users may uncheck, rewrite, or annotate them.

After writing it, report its exact path and stop. On resume, read the file from
disk rather than relying on the earlier conversation. Resolve every annotation,
including bracketed revision requests such as `【...】`, before applying.

## 7. Provenance and echo prevention

Every applied record carries:

- schema version and candidate ID;
- immediate source agent;
- origin agent;
- original and target scopes;
- the normalization rule version;
- a SHA-256 fingerprint of normalized source meaning;
- applied timestamp or target-native equivalent;
- project identity when scoped to a project.

When the origin agent equals the current receiving agent, exclude the candidate
as a round-trip echo by default. A user may explicitly request reconsideration,
but it must appear as a conflict and never be silently re-imported.

Provenance is for deduplication and audit. Do not copy verbose source metadata or
private paths into durable target memory when a stable abstract identifier is
sufficient.

Fingerprints are computed over normalized meaning using the shared, versioned
rule `alignment_guard.NORMALIZATION`. Normalization removes formatting noise only;
it never claims that two differently worded rules are equivalent. Because a
fingerprint written today must still match one computed later, changing the rule
requires a new version rather than an edit in place.

Repository identity is the primary project label, but it is absent for a
repository without a stable remote, so several distinct projects can share one
label inside a single target memory. When a record needs to separate them, use
the shared project key: a digest of the resolved root that disambiguates projects
within one installation without writing a private path into durable memory. It is
a local disambiguator, not a cross-host identity.

The three v1 mapping labels are `same-root`, `confirmed-remote`, and
`user-mapping`. Treat an unrecognized fourth shape as a protocol gap and stop;
adding a label requires both adapters and the schema to change together.

## 8. Apply and recovery

Before writing, validate the edited checklist, then repeat relevance,
deduplication, conflict, scope, secret, and echo checks against current target
state. Concurrent changes may make an approved candidate obsolete.

Use the target adapter's additive or atomic strategy. Keep exact recovery
material until verification succeeds. Verify the written content, scope labels,
provenance, source immutability, and absence of unresolved annotations. Delete
only the exact temporary checklist after success; retain it on failure.

If all selected candidates disappear during revalidation, report a successful
no-op and do not create an empty memory artifact.
