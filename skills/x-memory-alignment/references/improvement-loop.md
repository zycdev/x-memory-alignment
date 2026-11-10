# Improvement Loop

This reference governs how defects, friction, ideas, and surprises about this
skill are recorded and acted upon. It is separate from the alignment protocol:
an improvement report describes the tool, while memory describes the user's work.
The two never mix, and no report may become a memory candidate.

## Governing rule

> Capture is a local, offline, private act. Publication is an explicit, approved
> maintenance act.

`scripts/improve_log.py` implements capture. It imports no network library,
invokes neither `git` nor `gh`, and writes only inside a private state directory.
`scripts/improve_sync.py` is the separate publication boundary and is used only
inside an explicitly authorized maintenance session.

## When an agent may record

During ordinary use, a report is written only when:

1. the user asks for one, recorded as `initiated_by: user-request`; or
2. the agent offered to record something and the user accepted, recorded as
   `initiated_by: accepted-suggestion`.

`initiated_by` is required, so an unprompted report cannot be filed by accident,
and an unusual one remains visible afterwards. An agent may offer to record
something at most once per session, in one sentence, and must not repeat the
offer after a refusal. Ordinary use never writes to the repository and never
contacts the network.

## Storage

Reports live under `$CAMD_STATE_DIR`, else `$XDG_STATE_HOME/x-memory-alignment`,
else `~/.local/state/x-memory-alignment`. Directories are created
`0700` and report files `0600` with `O_CREAT | O_EXCL`, so a report is never
written over another and never becomes group or world readable.

When upgrading an existing installation, inspect its recorded private state location before reinstalling.
An explicitly configured `CAMD_STATE_DIR` continues to select that location.
To adopt the new default, move the complete private state directory with a recoverable backup and repair any contained Git worktree registration, then reinstall and verify the skill.
Do not discard reports or edit provenance to change a display name.

Rewrites use `O_NOFOLLOW` where the platform provides it. This rejects a report
or packet whose final path component has been replaced by a symbolic link; it is
not a claim of full path-traversal protection, because a configured state
directory may itself be a symbolic link.

## Report schema `report/v1`

One Markdown file per report, headed by a single-line metadata comment, so the
visible prose may be written in whatever language the user was speaking.

| Field | Values |
| --- | --- |
| `kind` | `bug`, `friction`, `idea`, `docs`, `question`, `surprise` |
| `severity` | `blocking`, `major`, `minor`, `cosmetic` |
| `component` | `guard`, `claude-adapter`, `codex-adapter`, `protocol`, `install`, `docs`, `loop`, `unknown` |
| `status` | `new`, `triaged`, `published`, `resolved`, `dropped` |
| `agent` | `claude-code`, `codex`, `unknown` |
| `initiated_by` | `user-request`, `accepted-suggestion` |
| `fingerprint`, `normalization` | shared meaning digest and its rule version |
| `occurrences` | how often the same thing was reported |

`surprise` exists because "it worked as designed and still surprised me" is
often the most valuable signal, and it fits neither `bug` nor `idea`.

Private evidence belongs inside `<!-- camd:evidence:private -->` markers. It stays
local and never appears in any public view or packet.

## Deduplication

`add` fingerprints the normalized title and body with the shared `camd-norm/v1`
rule. A match against an open report records another occurrence instead of
creating a second file, which also produces the frequency signal triage needs. A
resolved report never absorbs a new occurrence, because a recurrence after a fix
is new information.

Two reports that describe one defect in different words are a semantic judgement,
not a mechanical one. They are linked during triage by the agents, never by the
script.

## Mechanical redaction

The scanner blocks only what can be identified without interpretation:

- absolute paths under a home directory;
- electronic mail addresses;
- host names, and `user@host:path` targets;
- alignment metadata comments, namely `camd:provenance`, `camd:candidate`,
  and `camd:checklist`;
- long hexadecimal digests;
- the private evidence block, which is excluded from every public view.

Findings name a rule, a line, a column, and a length. They never quote the
matched text, because a redaction report that quotes private material recreates
the problem it exists to prevent.

The scanner makes no claim about ordinary prose. Whether a sentence discloses
something sensitive is a semantic judgement that belongs to both agents and the
user, exactly as relevance and conflict judgements do elsewhere in this project.

## Maintenance packet

`improve_log.py draft` renders a read-only packet under the `camd:triage`
grammar, and `improve_log.py validate --packet` validates it against the local
queue: identifiers must exist, status and fingerprint must match, and private
evidence must not appear.

The memory checklist grammar is deliberately not reused. Its `source_agent`,
`origin_agent`, and scope fields have no meaning for an improvement report, and
borrowing the shape would import approval semantics into an artifact that grants
no authority. The packet records no decision and authorizes no change.

## GitHub synchronization

`improve_sync.py` provides `plan`, `push`, `pull`, and `close`. It invokes GitHub
only through argument-vector `gh` calls and is inert unless the caller supplies
both `--maintenance` and `CAMD_MAINTENANCE=1`. `push`, `pull`, and `close` print a
dry run unless `--yes` is also supplied.

Before any mutation, the helper verifies the local `origin` as a `github.com`
repository, checks an optional explicit `owner/name`, verifies the GitHub CLI
credential and its write permission read-only, and confirms the repository
identity. A missing remote, CLI, credential, permission, or repository makes the
command report the reason and change nothing.

- `push` accepts only an explicit user-approved `--report` selection of locally
  triaged or already published reports, rejects any public view with mechanical
  redaction findings, ensures the managed label vocabulary, and creates or
  updates an issue carrying one hidden `camd-report-id` marker. An unfiltered
  `plan` may summarize the queue but cannot be applied. Marker lookup makes
  repeated publication idempotent. The local link stores the last synchronized
  body digest: if a maintainer edits the remote body, the next run reports a
  divergence instead of overwriting it. Labels are reconciled exactly only
  inside the four managed families; unrelated human-added labels are preserved.
- `pull` reconciles exact markers into local metadata. A remote marker cannot
  promote a locally untriaged report, and a transient issue-query error is not
  treated as deletion. A confirmed missing issue is recorded for user review
  and is never silently recreated.
- `close` accepts only a locally `resolved` report with a linked marked issue,
  a commit that resolves locally, and a non-empty passing-test summary. It never
  infers resolution from GitHub state. The commit and test summary are maintainer
  assertions recorded for audit: the helper verifies that the commit resolves
  locally but does not rerun or interpret the stated tests.

Marker matching provides record identity, not semantic deduplication. External
unmarked issues are reviewed manually for overlap before a maintenance batch;
the tool never adopts or edits them.

## Triggering joint maintenance

`improve_log.py status` reports `maintenance_recommended` when at least five
reports are open, or any open report is `blocking`, or an open report is older
than fourteen days. The agent may surface this once per session, in one sentence,
only when the skill was already in use for another reason. Nothing in the tool
starts work, opens a session, or notifies anyone.

A maintenance cycle is opened by the user. Both agents review the packet
independently, the user decides dispositions, accepted items are implemented with
regression tests by their owner and reviewed by the other agent, and evidence is
recorded with the cycle, exactly as ordinary work is.

## Closure authority

| Actor | May do |
| --- | --- |
| User | Anything: close, reopen, drop, publish, decide `wontfix`. |
| Either agent, in a maintenance session | Propose dispositions; set `resolved` only with evidence, meaning a commit and a passing test. |
| Either agent, in ordinary use | Create reports and record occurrences. Nothing else. |

`wontfix` always requires the user's explicit decision with a recorded reason.

## Retention of private evidence

The rule for this version, stated so that a maintainer knows what to expect and
a reader knows what the tool does not do:

- A report's body is private material until it has been reviewed. A clean
  mechanical scan is a precondition for publication, not a clearance: the
  scanner blocks identifiable classes and makes no claim about ordinary prose,
  so only a scan followed by a semantic review by the agents and the user
  establishes that a body may be shown outside the queue.
- A reviewed body is kept indefinitely. Once it has passed that review it is the
  durable record of what was learned, and no rule here removes it.
- The `camd:evidence:private` block of a report that has been `resolved` or
  `dropped` for thirty days should be removed. Its purpose ends when the report
  does, and private material that no longer serves a purpose is a liability.
- Nothing deletes it automatically. No command in this version removes evidence,
  on a schedule or otherwise, so the rule above is a maintainer practice rather
  than an enforced behaviour. Treat the queue as material you are responsible
  for, not as a store that tidies itself.

Automatic purging is deferred deliberately. A tool that deletes private material
on a timer is a tool that will one day delete something a maintainer still
needed, and the queue is small enough that manual removal costs little.

## Deferred

GitHub publication remains unavailable on an installation without a verified
remote and credential, while capture, triage, and validation continue to work
offline. Per-target release refs are a different installation model and are not
part of this version.
