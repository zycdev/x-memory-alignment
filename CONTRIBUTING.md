# Contributing

Thank you for looking at this project. This guide explains how reports reach
this repository, because the flow is unusual: most of the feedback that shapes
this project is captured offline and privately first, and only some of it is
ever published here.

## What never belongs in an issue or a pull request

Credentials and tokens, absolute paths from a home directory, memory or
checklist content, and transcript excerpts. Replace a real path with a path
class such as `<claude-home>/skills/<skill>`. This project distills memory
between agents, so its own bug reports naturally attract exactly the material
that must not be published. A report that has to be redacted after the fact is
already public.

## How maintainer reports reach this repository

Maintainers do not open an issue at the moment something goes wrong. The skill
keeps a private, offline queue on the maintainer's machine, described in
`skills/cross-agent-memory-distiller/references/improvement-loop.md`. Capture is
local: it writes nothing to this repository and contacts no network. Publication
is a separate, explicit, batched act that happens during a maintenance cycle,
after the material has been reviewed and redacted, and after the maintainer
approves it.

Two consequences are worth knowing:

- Issues from maintainers can appear in groups, days after the observation, and
  will read as summaries rather than as live debugging.
- Anything still carrying private material is never published at all. Its public
  form is a redacted description, and the evidence stays on the maintainer's
  machine.

Publication uses the maintenance-only `improve_sync.py` helper. It defaults to a
dry run and refuses to operate when the remote, repository identity, or GitHub
credential and write permission cannot be verified read-only. A publishing run
also names each locally approved report explicitly; an unfiltered queue plan is
review-only.

## The origin marker, and why you should not add one

An issue published from a maintainer's queue carries a hidden marker in its
body:

```html
<!-- camd-report-id: CAMD-R-YYYYMMDD-XXXXXX -->
```

The marker is an identity, not a label. Synchronisation matches only its own
identifiers, which is what makes republishing idempotent: a report that is
already published is updated in place rather than opened a second time.

Please do not add such a marker to an issue or pull request you open. Its
absence is precisely what marks a report as external. An unmarked issue is never
adopted, rewritten, or closed by synchronization. Marker matching cannot detect
semantic duplicates, so maintainers inspect external issues for overlap before
publishing a batch. If review misses an overlap, the two issues may coexist until
a maintainer links and reconciles them by hand.

Do not edit or remove a marker in an existing issue either. Removing one
disconnects that issue from the queue entry it came from; the sync then stops for
review instead of guessing by title or silently recreating the issue.

## Labels

`.github/labels.yml` is the source of truth for the label vocabulary, and it
mirrors the vocabulary used by the private queue, so the two records stay
reconcilable. Four families are in use:

| Family | Meaning |
| --- | --- |
| `kind/*` | what sort of report this is |
| `component/*` | which part of the project it concerns |
| `agent/*` | which agent the reporter was using, when known |
| `cycle/*` | the maintenance cycle that triaged it |

Maintainers apply labels. Please do not spend effort guessing them; the issue
forms already collect the component and the agent.

Triage dispositions, such as fixing now, deferring, or declining, are not
labels. They live in the maintainers' queue. Recording them in two places would
create two records that can disagree, and the queue is the one that drives the
work.

## Who closes an issue

Maintainers close issues. An issue you opened is closed by a maintainer, never
automatically by tooling, and never as a side effect of a synchronisation batch.
Declining a report always comes with a stated reason.

Agents working on this repository may propose a disposition and may mark work
resolved once a fix has landed with a passing test, but the closing decision on
an externally reported issue rests with the maintainers.

## Pull requests

Small, focused changes are easiest to review. Before opening one:

```bash
python -m unittest discover -s tests -v
ruff check --no-cache scripts skills tests
```

Both must pass. New behaviour needs a test; a bug fix needs a regression that
fails without the fix. If your change touches the shared protocol or either
adapter, say in the description which of the two agents you exercised it with,
since much of this project's correctness is about the asymmetry between them.

## Boundaries every change must preserve

These are not style preferences. Each one exists because violating it would
either publish private material or let the tool act without the user.

- Source memory is read only. Target memory changes only after the user has
  edited the generated checklist and explicitly approved applying it.
- Only durable guidance that the receiving agent can act on is transferred.
  Product internals, transcripts, credentials, settings, histories, caches, and
  whole memory directories are not.
- Project memory is never promoted to global scope. Projects are aligned by
  resolved root and repository identity, and the mapping is shown in the
  checklist.
- Provenance is preserved, and a candidate that originated in the receiving
  agent is refused rather than distilled back into it.
- Runtime checklist language follows the conversation. No language is hardcoded.
- Ordinary use performs no repository write and no network access.
  `improve_sync.py` is the only GitHub-facing helper, it runs only in an
  explicitly authorized maintenance session, and it reports rather than mutates
  whenever a remote or credential is missing or unverified.
- Semantic relevance and conflict decisions belong to the agent and the user.
  Scripts may validate structure, hashes, provenance, and invariants; they must
  not present a mechanical check as a semantic judgement.
- Committed tests use synthetic fixtures. No test reads or writes real agent
  memory, and no live-test material is committed.

## Development notes

- The canonical skill lives under `skills/cross-agent-memory-distiller/`.
- Install it with `python scripts/install_skill.py`, which never overwrites a
  destination it did not create.
- Tests are standard library only and must not read or write real agent memory.
  Every test builds its own temporary roots.
