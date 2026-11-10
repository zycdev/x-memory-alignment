#!/usr/bin/env python3
"""Explicit, maintenance-only synchronization of reviewed reports with GitHub.

This is the only x-memory-alignment helper allowed to invoke ``gh``.
It is deliberately separate from ``improve_log.py`` so ordinary report capture
remains offline by construction. Every command requires both ``--maintenance``
and ``CAMD_MAINTENANCE=1``. Mutating commands are dry runs unless ``--yes`` is
also present.

The tool fails closed before changing either GitHub or the local queue when the
repository remote, GitHub CLI, authentication, or repository identity cannot be
verified read-only. Reports are matched by a hidden ``camd-report-id`` marker,
never by title or fuzzy search.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import improve_log  # noqa: E402  (local module beside this script)

MAINTENANCE_ENV = "CAMD_MAINTENANCE"
MAINTENANCE_VALUE = "1"
REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
MARKER_RE = re.compile(
    r"<!--\s*camd-report-id:\s*(CAMD-R-\d{8}-[0-9A-HJ-NP-Z]{6})\s*-->"
)
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
MUTATING_PERMISSIONS = frozenset({"ADMIN", "MAINTAIN", "WRITE"})
OWNED_LABEL_PREFIXES = ("kind/", "component/", "agent/", "cycle/")


class SyncError(RuntimeError):
    """A condition that must stop before the requested mutation."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    """Argument-vector subprocess runner, replaceable by deterministic tests."""

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=30,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class Preflight:
    available: bool
    repo: str | None
    account: str | None
    visibility: str | None
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "repo": self.repo,
            "account": self.account,
            "visibility": self.visibility,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RemoteIssue:
    number: int
    state: str
    title: str
    body: str
    url: str
    labels: frozenset[str]

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "RemoteIssue":
        labels = data.get("labels", [])
        names = {
            str(item.get("name"))
            for item in labels
            if isinstance(item, dict) and item.get("name")
        }
        return cls(
            number=int(data["number"]),
            state=str(data.get("state", "")).upper(),
            title=str(data.get("title", "")),
            body=str(data.get("body", "")),
            url=str(data.get("url", "")),
            labels=frozenset(names),
        )

    @property
    def marker_ids(self) -> tuple[str, ...]:
        return tuple(MARKER_RE.findall(self.body))


LABEL_SPECS: dict[str, tuple[str, str]] = {
    "kind/bug": ("d73a4a", "Something behaves incorrectly"),
    "kind/friction": ("e99695", "It works, but using it is harder than it should be"),
    "kind/idea": ("a2eeef", "A proposal that is not yet a concrete change"),
    "kind/docs": ("0075ca", "Documentation is missing, wrong, or misleading"),
    "kind/question": ("d876e3", "A question about intended behaviour"),
    "kind/surprise": ("fbca04", "Worked as designed and still surprised the reporter"),
    "component/guard": ("c5def5", "Shared validation and mechanical guards"),
    "component/claude-adapter": ("c5def5", "Codex-to-Claude adapter"),
    "component/codex-adapter": ("c5def5", "Claude-to-Codex adapter"),
    "component/protocol": ("c5def5", "Shared protocol, schema, and references"),
    "component/install": ("c5def5", "Installer and skill discovery"),
    "component/docs": ("c5def5", "Repository documentation"),
    "component/loop": ("c5def5", "Improvement loop, queue, and maintenance cycles"),
    "component/unknown": ("c5def5", "Component not yet determined"),
    "agent/claude-code": ("bfd4f2", "Observed while using Claude Code"),
    "agent/codex": ("bfd4f2", "Observed while using Codex"),
    "agent/unknown": ("bfd4f2", "Agent not recorded or not relevant"),
}


def require_maintenance(
    maintenance: bool, environment: Mapping[str, str] | None = None
) -> None:
    env = environment or os.environ
    if not maintenance or env.get(MAINTENANCE_ENV) != MAINTENANCE_VALUE:
        raise SyncError(
            "publication commands require --maintenance and "
            f"{MAINTENANCE_ENV}={MAINTENANCE_VALUE}; neither is implied"
        )


def parse_github_remote(remote: str) -> str | None:
    match = REMOTE_RE.fullmatch(remote.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def _json_result(result: CommandResult, context: str) -> Any:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise SyncError(f"{context}: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SyncError(f"{context}: invalid JSON output") from error


def preflight(
    repo_root: Path,
    expected_repo: str | None,
    runner: CommandRunner,
) -> Preflight:
    if expected_repo is not None and not GITHUB_REPO_RE.fullmatch(expected_repo):
        return Preflight(False, None, None, None, "--repo must be owner/name")
    remote = runner.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"]
    )
    if remote.returncode != 0 or not remote.stdout.strip():
        return Preflight(False, None, None, None, "origin remote is missing")
    repo = parse_github_remote(remote.stdout)
    if repo is None:
        return Preflight(
            False, None, None, None, "origin is not an accepted github.com remote"
        )
    if expected_repo is not None and expected_repo.lower() != repo.lower():
        return Preflight(
            False,
            repo,
            None,
            None,
            f"origin resolves to {repo}, not the explicitly expected repository",
        )
    if runner.which("gh") is None:
        return Preflight(False, repo, None, None, "GitHub CLI is not available")
    auth = runner.run(["gh", "auth", "status", "-h", "github.com"])
    if auth.returncode != 0:
        return Preflight(False, repo, None, None, "GitHub CLI authentication failed")
    account_result = runner.run(["gh", "api", "user", "--jq", ".login"])
    if account_result.returncode != 0 or not account_result.stdout.strip():
        return Preflight(False, repo, None, None, "GitHub credential could not be verified")
    account = account_result.stdout.strip()
    view = runner.run(
        [
            "gh",
            "repo",
            "view",
            repo,
            "--json",
            "nameWithOwner,visibility,viewerPermission",
        ]
    )
    if view.returncode != 0:
        return Preflight(False, repo, account, None, "GitHub repository is unavailable")
    try:
        payload = json.loads(view.stdout)
    except json.JSONDecodeError:
        return Preflight(False, repo, account, None, "repository preflight returned invalid JSON")
    actual = str(payload.get("nameWithOwner", ""))
    if actual.lower() != repo.lower():
        return Preflight(False, repo, account, None, "repository identity verification failed")
    permission = str(payload.get("viewerPermission", "")).upper()
    if permission not in MUTATING_PERMISSIONS:
        return Preflight(
            False,
            repo,
            account,
            str(payload.get("visibility", "")),
            "GitHub credential lacks verified write permission",
        )
    return Preflight(True, repo, account, str(payload.get("visibility", "")), None)


def issue_marker(identifier: str) -> str:
    if not improve_log.ID_RE.fullmatch(identifier):
        raise SyncError(f"invalid report id: {identifier}")
    return f"<!-- camd-report-id: {identifier} -->"


def issue_body_digest(body: str) -> str:
    canonical = body.rstrip().encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def labels_for(report: improve_log.Report, cycle: int) -> tuple[str, ...]:
    if cycle < 1:
        raise SyncError("cycle must be a positive integer")
    return (
        f"kind/{report.metadata.get('kind')}",
        f"component/{report.metadata.get('component')}",
        f"agent/{report.metadata.get('agent')}",
        f"cycle/{cycle}",
    )


def label_style(name: str) -> tuple[str, str]:
    if name in LABEL_SPECS:
        return LABEL_SPECS[name]
    if re.fullmatch(r"cycle/\d+", name):
        return "ededed", f"Triaged in maintenance cycle {name.split('/', 1)[1]}"
    raise SyncError(f"unknown label outside the managed vocabulary: {name}")


def render_issue_body(report: improve_log.Report) -> str:
    lines = [
        "This issue was published from a locally reviewed CAMD improvement report.",
        "Private evidence is intentionally omitted.",
        "",
        f"- Kind: `{report.metadata.get('kind')}`",
        f"- Severity: `{report.metadata.get('severity')}`",
        f"- Component: `{report.metadata.get('component')}`",
        f"- Observed by: `{report.metadata.get('agent')}`",
        f"- Occurrences: `{report.metadata.get('occurrences')}`",
    ]
    public_body = report.public_body()
    if public_body:
        lines.extend(["", "## Report", "", public_body])
    lines.extend(["", issue_marker(report.id)])
    return "\n".join(lines).rstrip() + "\n"


def report_revision(report: improve_log.Report) -> dict[str, Any]:
    """Fields that make a reviewed plan stale when local capture changes."""

    return {
        "fingerprint": report.metadata.get("fingerprint"),
        "updated": report.metadata.get("updated"),
        "occurrences": report.metadata.get("occurrences"),
        "status": report.status,
    }


def _current_reports_for_plan(plan: Mapping[str, Any]) -> dict[str, improve_log.Report]:
    reports = {report.id: report for report in improve_log.load_reports()}
    for action in plan.get("actions", []):
        identifier = action.get("id")
        planned = action.get("local_revision")
        if not isinstance(identifier, str) or not isinstance(planned, dict):
            continue
        report = reports.get(identifier)
        if report is None:
            raise SyncError(
                f"local report {identifier} disappeared after planning; rerun the plan"
            )
        if report_revision(report) != planned:
            raise SyncError(
                f"local report {identifier} changed after planning; rerun the plan "
                "before any publication mutation"
            )
    return reports


def _select_reports(identifiers: Sequence[str] | None) -> tuple[list[improve_log.Report], list[str]]:
    reports, warnings = improve_log.scan_reports()
    by_id = {report.id: report for report in reports}
    if identifiers:
        if len(set(identifiers)) != len(identifiers):
            raise SyncError("the same report id was selected more than once")
        missing = [identifier for identifier in identifiers if identifier not in by_id]
        if missing:
            raise SyncError(f"unknown report id(s): {', '.join(missing)}")
        return [by_id[identifier] for identifier in identifiers], warnings
    return [
        report
        for report in reports
        if report.status in ("triaged", "published")
    ], warnings


def _remote_snapshot(
    repo: str, runner: CommandRunner
) -> tuple[list[RemoteIssue], set[str]]:
    issues_result = runner.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            "number,state,title,body,url,labels",
        ]
    )
    raw_issues = _json_result(issues_result, "cannot list GitHub issues")
    if not isinstance(raw_issues, list):
        raise SyncError("cannot list GitHub issues: expected a JSON array")
    issues = [RemoteIssue.from_json(item) for item in raw_issues]
    labels_result = runner.run(
        ["gh", "label", "list", "--repo", repo, "--limit", "1000", "--json", "name"]
    )
    raw_labels = _json_result(labels_result, "cannot list GitHub labels")
    if not isinstance(raw_labels, list):
        raise SyncError("cannot list GitHub labels: expected a JSON array")
    labels = {
        str(item.get("name"))
        for item in raw_labels
        if isinstance(item, dict) and item.get("name")
    }
    return issues, labels


def _issues_by_marker(issues: Sequence[RemoteIssue]) -> dict[str, RemoteIssue]:
    mapping: dict[str, RemoteIssue] = {}
    duplicates: set[str] = set()
    for issue in issues:
        for identifier in issue.marker_ids:
            if identifier in mapping and mapping[identifier].number != issue.number:
                duplicates.add(identifier)
            else:
                mapping[identifier] = issue
    if duplicates:
        raise SyncError(
            "multiple GitHub issues carry the same report marker: "
            + ", ".join(sorted(duplicates))
        )
    return mapping


def build_push_plan(
    *,
    repo_root: Path,
    expected_repo: str | None,
    identifiers: Sequence[str] | None,
    cycle: int,
    runner: CommandRunner,
) -> dict[str, Any]:
    checked = preflight(repo_root, expected_repo, runner)
    payload: dict[str, Any] = {
        "command": "push",
        "dry_run": True,
        "explicit_selection": bool(identifiers),
        "preflight": checked.as_dict(),
        "actions": [],
        "missing_labels": [],
        "warnings": [],
    }
    if not checked.available or checked.repo is None:
        return payload
    reports, warnings = _select_reports(identifiers)
    payload["warnings"] = warnings
    issues, existing_labels = _remote_snapshot(checked.repo, runner)
    by_marker = _issues_by_marker(issues)
    required_labels: set[str] = set()
    for report in reports:
        labels = labels_for(report, cycle)
        required_labels.update(labels)
        action: dict[str, Any] = {
            "id": report.id,
            "labels": list(labels),
            "local_revision": report_revision(report),
        }
        metadata_errors = improve_log.validate_metadata(report.metadata, report.id)
        if metadata_errors:
            action.update(action="blocked", reasons=metadata_errors)
            payload["actions"].append(action)
            continue
        if report.status not in ("triaged", "published"):
            action.update(action="blocked", reasons=["report has not survived triage"])
            payload["actions"].append(action)
            continue
        redaction = improve_log.scan_redaction(report.public_view())
        if redaction:
            action.update(
                action="blocked",
                reasons=["public view has unresolved mechanical redaction flags"],
                redaction_flags=redaction,
            )
            payload["actions"].append(action)
            continue
        wanted_body = render_issue_body(report)
        remote = by_marker.get(report.id)
        if remote is None:
            action.update(action="create", title=report.title)
        else:
            wanted_labels = set(labels)
            remove_labels = sorted(
                label
                for label in remote.labels
                if label.startswith(OWNED_LABEL_PREFIXES) and label not in wanted_labels
            )
            wanted_digest = issue_body_digest(wanted_body)
            remote_digest = issue_body_digest(remote.body)
            github = report.metadata.get("github")
            stored_digest = (
                github.get("body_digest")
                if isinstance(github, dict)
                and str(github.get("repo", "")).lower() == checked.repo.lower()
                and github.get("issue") == remote.number
                else None
            )
            if (
                stored_digest is not None
                and remote_digest not in (stored_digest, wanted_digest)
            ) or (stored_digest is None and remote_digest != wanted_digest):
                action.update(
                    action="blocked",
                    issue=remote.number,
                    url=remote.url,
                    reasons=[
                        "published issue body diverged from the last synchronized body; "
                        "review and reconcile the maintainer edit explicitly"
                    ],
                )
                payload["actions"].append(action)
                continue
            changed = (
                remote.title != report.title
                or remote_digest != wanted_digest
                or not wanted_labels.issubset(remote.labels)
                or bool(remove_labels)
            )
            linked = (
                isinstance(github, dict)
                and github.get("repo", "").lower() == checked.repo.lower()
                and github.get("issue") == remote.number
                and report.status == "published"
                and stored_digest == wanted_digest
            )
            action.update(
                action="update" if changed else ("noop" if linked else "link"),
                issue=remote.number,
                url=remote.url,
                title=report.title,
                remove_labels=remove_labels,
            )
        payload["actions"].append(action)
    payload["missing_labels"] = sorted(required_labels - existing_labels)
    return payload


def _ensure_labels(
    repo: str, names: Sequence[str], runner: CommandRunner
) -> list[str]:
    created: list[str] = []
    for name in names:
        color, description = label_style(name)
        result = runner.run(
            [
                "gh",
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                description,
            ]
        )
        if result.returncode != 0:
            raise SyncError(
                f"cannot create label {name}: "
                + (result.stderr.strip() or result.stdout.strip() or "command failed")
            )
        created.append(name)
    return created


def _issue_number_from_output(output: str) -> tuple[int, str]:
    url = output.strip().splitlines()[-1] if output.strip() else ""
    match = re.search(r"/issues/(\d+)(?:\?.*)?$", url)
    if not match:
        raise SyncError("GitHub issue creation did not return an issue URL")
    return int(match.group(1)), url


def _rewrite_report_link(
    report: improve_log.Report,
    *,
    repo: str,
    issue: int,
    url: str,
    state: str,
    missing: bool = False,
    body_digest: str | None = None,
) -> None:
    expected = improve_log.render_report(report.metadata, report.body)
    try:
        current = report.path.read_text(encoding="utf-8")
    except OSError as error:
        raise SyncError(f"cannot re-read local report {report.id}: {error}") from error
    if current != expected:
        raise SyncError(
            f"local report {report.id} changed during synchronization; "
            "the remote marker is sufficient for a later pull to reconcile it"
        )
    metadata = dict(report.metadata)
    github = dict(metadata.get("github") or {})
    github.update(
        {
            "repo": repo,
            "issue": issue,
            "url": url,
            "state": state.lower(),
            "missing": missing,
            "marker": "camd-report-id/v1",
            "published_occurrences": metadata.get("occurrences"),
        }
    )
    if body_digest is not None:
        github["body_digest"] = body_digest
    metadata["github"] = github
    if metadata.get("status") in ("new", "triaged"):
        metadata["status"] = "published"
    metadata["updated"] = improve_log._now()
    improve_log._rewrite_private_file(
        report.path, improve_log.render_report(metadata, report.body)
    )


def apply_push_plan(
    plan: dict[str, Any], runner: CommandRunner
) -> dict[str, Any]:
    checked = plan["preflight"]
    if not checked.get("available") or not checked.get("repo"):
        raise SyncError(checked.get("reason") or "publication preflight failed")
    if not plan.get("explicit_selection"):
        raise SyncError(
            "push mutation requires at least one explicit --report; "
            "an unfiltered plan is review-only"
        )
    blocked = [item for item in plan["actions"] if item["action"] == "blocked"]
    if blocked:
        raise SyncError("push plan contains blocked report(s); nothing was changed")
    repo = str(checked["repo"])
    reports = _current_reports_for_plan(plan)
    created_labels = _ensure_labels(repo, plan["missing_labels"], runner)
    results: list[dict[str, Any]] = []
    for action in plan["actions"]:
        report = reports[action["id"]]
        kind = action["action"]
        if kind == "noop":
            results.append({"id": report.id, "action": "noop", "issue": action["issue"]})
            continue
        body = render_issue_body(report)
        labels = ",".join(action["labels"])
        if kind == "create":
            completed = runner.run(
                [
                    "gh",
                    "issue",
                    "create",
                    "--repo",
                    repo,
                    "--title",
                    report.title,
                    "--body-file",
                    "-",
                    "--label",
                    labels,
                ],
                input_text=body,
            )
            if completed.returncode != 0:
                raise SyncError(
                    f"cannot create issue for {report.id}: "
                    + (completed.stderr.strip() or completed.stdout.strip())
                )
            number, url = _issue_number_from_output(completed.stdout)
        else:
            number = int(action["issue"])
            url = str(action.get("url", ""))
            if kind == "update":
                edit_args = [
                    "gh",
                    "issue",
                    "edit",
                    str(number),
                    "--repo",
                    repo,
                    "--title",
                    report.title,
                    "--body-file",
                    "-",
                    "--add-label",
                    labels,
                ]
                remove_labels = ",".join(action.get("remove_labels", []))
                if remove_labels:
                    edit_args.extend(["--remove-label", remove_labels])
                completed = runner.run(edit_args, input_text=body)
                if completed.returncode != 0:
                    raise SyncError(
                        f"cannot update issue #{number}: "
                        + (completed.stderr.strip() or completed.stdout.strip())
                    )
        _rewrite_report_link(
            report,
            repo=repo,
            issue=number,
            url=url,
            state="open",
            body_digest=issue_body_digest(body),
        )
        results.append({"id": report.id, "action": kind, "issue": number, "url": url})
    return {
        "command": "push",
        "dry_run": False,
        "repo": repo,
        "created_labels": created_labels,
        "results": results,
    }


def _build_pull_link_action(
    report: improve_log.Report, issue: RemoteIssue, repo: str
) -> dict[str, Any]:
    github = report.metadata.get("github")
    same_link = (
        isinstance(github, dict)
        and str(github.get("repo", "")).lower() == repo.lower()
        and github.get("issue") == issue.number
    )
    stored_digest = github.get("body_digest") if same_link else None
    remote_digest = issue_body_digest(issue.body)
    wanted_digest = issue_body_digest(render_issue_body(report))
    diverged = (
        stored_digest is not None
        and remote_digest not in (stored_digest, wanted_digest)
    ) or (stored_digest is None and remote_digest != wanted_digest)
    base = {
        "id": report.id,
        "issue": issue.number,
        "url": issue.url,
        "state": issue.state.lower(),
        "local_revision": report_revision(report),
    }
    if diverged:
        return {
            **base,
            "action": "blocked",
            "reason": (
                "published issue body diverged from the last synchronized body; "
                "review and reconcile the maintainer edit explicitly"
            ),
        }
    linked = (
        same_link
        and github.get("state") == issue.state.lower()
        and github.get("missing") is False
        and github.get("url") == issue.url
        and stored_digest == remote_digest
    )
    return {
        **base,
        "action": "noop" if linked else "link",
        "body_digest": remote_digest,
    }


def build_pull_plan(
    *, repo_root: Path, expected_repo: str | None, runner: CommandRunner
) -> dict[str, Any]:
    checked = preflight(repo_root, expected_repo, runner)
    payload: dict[str, Any] = {
        "command": "pull",
        "dry_run": True,
        "preflight": checked.as_dict(),
        "actions": [],
        "warnings": [],
    }
    if not checked.available or checked.repo is None:
        return payload
    reports, warnings = improve_log.scan_reports()
    payload["warnings"] = warnings
    by_id = {report.id: report for report in reports}
    issues, _ = _remote_snapshot(checked.repo, runner)
    by_marker = _issues_by_marker(issues)
    seen: set[str] = set()
    for identifier, issue in sorted(by_marker.items()):
        report = by_id.get(identifier)
        if report is None:
            payload["actions"].append(
                {"id": identifier, "action": "unknown-local-report", "issue": issue.number}
            )
            continue
        seen.add(identifier)
        if report.status == "new":
            payload["actions"].append(
                {
                    "id": identifier,
                    "action": "blocked",
                    "issue": issue.number,
                    "reason": "remote marker cannot bypass local triage",
                    "local_revision": report_revision(report),
                }
            )
            continue
        payload["actions"].append(
            _build_pull_link_action(report, issue, checked.repo)
        )
    for report in reports:
        github = report.metadata.get("github")
        if (
            report.id in seen
            or not isinstance(github, dict)
            or str(github.get("repo", "")).lower() != checked.repo.lower()
            or not isinstance(github.get("issue"), int)
        ):
            continue
        number = int(github["issue"])
        view = runner.run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                checked.repo,
                "--json",
                "number,state,title,body,url,labels",
            ]
        )
        if view.returncode == 0:
            issue = RemoteIssue.from_json(_json_result(view, f"cannot view issue #{number}"))
            if report.id not in issue.marker_ids:
                payload["actions"].append(
                    {
                        "id": report.id,
                        "action": "blocked",
                        "issue": number,
                        "reason": "recorded issue no longer carries this report marker",
                        "local_revision": report_revision(report),
                    }
                )
                continue
            payload["actions"].append(
                _build_pull_link_action(report, issue, checked.repo)
            )
            continue
        failure = (view.stderr or view.stdout).lower()
        if "not found" not in failure and "http 404" not in failure:
            raise SyncError(
                f"cannot determine whether issue #{number} is missing: "
                + (view.stderr.strip() or view.stdout.strip() or "command failed")
            )
        payload["actions"].append(
            {
                "id": report.id,
                "action": "missing",
                "issue": number,
                "url": str(github.get("url", "")),
                "state": str(github.get("state", "unknown")),
                "body_digest": github.get("body_digest"),
                "ask_user": True,
                "local_revision": report_revision(report),
            }
        )
    return payload


def apply_pull_plan(plan: dict[str, Any]) -> dict[str, Any]:
    checked = plan["preflight"]
    if not checked.get("available") or not checked.get("repo"):
        raise SyncError(checked.get("reason") or "publication preflight failed")
    repo = str(checked["repo"])
    reports = _current_reports_for_plan(plan)
    results: list[dict[str, Any]] = []
    for action in plan["actions"]:
        if action["action"] in ("noop", "unknown-local-report", "blocked"):
            results.append(action)
            continue
        report = reports[action["id"]]
        _rewrite_report_link(
            report,
            repo=repo,
            issue=int(action["issue"]),
            url=str(action.get("url", "")),
            state=str(action.get("state", "unknown")),
            missing=action["action"] == "missing",
            body_digest=action.get("body_digest"),
        )
        results.append(action)
    return {"command": "pull", "dry_run": False, "repo": repo, "results": results}


def build_close_plan(
    *,
    repo_root: Path,
    expected_repo: str | None,
    identifiers: Sequence[str],
    commit: str,
    test_evidence: str,
    runner: CommandRunner,
) -> dict[str, Any]:
    checked = preflight(repo_root, expected_repo, runner)
    payload: dict[str, Any] = {
        "command": "close",
        "dry_run": True,
        "preflight": checked.as_dict(),
        "actions": [],
    }
    if not checked.available or checked.repo is None:
        return payload
    if not identifiers:
        raise SyncError("close requires at least one --report")
    if not COMMIT_RE.fullmatch(commit):
        raise SyncError("--commit must be a 7 to 64 character hexadecimal commit id")
    resolved = runner.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{commit}^{{commit}}"]
    )
    if resolved.returncode != 0:
        raise SyncError("--commit does not resolve in the local repository")
    if not test_evidence.strip():
        raise SyncError("--test must summarize the passing validation")
    if improve_log.scan_redaction(test_evidence):
        raise SyncError("closure comment has unresolved mechanical redaction flags")
    reports, _ = _select_reports(identifiers)
    by_id = {report.id: report for report in reports}
    issues, _ = _remote_snapshot(checked.repo, runner)
    by_number = {issue.number: issue for issue in issues}
    for identifier in identifiers:
        report = by_id[identifier]
        github = report.metadata.get("github")
        if report.status != "resolved":
            payload["actions"].append(
                {"id": identifier, "action": "blocked", "reason": "local report is not resolved"}
            )
            continue
        if not isinstance(github, dict) or not isinstance(github.get("issue"), int):
            payload["actions"].append(
                {"id": identifier, "action": "blocked", "reason": "report has no linked issue"}
            )
            continue
        number = int(github["issue"])
        issue = by_number.get(number)
        if issue is None or identifier not in issue.marker_ids:
            payload["actions"].append(
                {"id": identifier, "action": "blocked", "issue": number, "reason": "linked issue or marker is missing"}
            )
            continue
        payload["actions"].append(
            {
                "id": identifier,
                "action": "noop" if issue.state == "CLOSED" else "close",
                "issue": number,
                "url": issue.url,
                "commit": resolved.stdout.strip()[:12],
                "test": test_evidence.strip(),
                "local_revision": report_revision(report),
            }
        )
    return payload


def apply_close_plan(plan: dict[str, Any], runner: CommandRunner) -> dict[str, Any]:
    checked = plan["preflight"]
    if not checked.get("available") or not checked.get("repo"):
        raise SyncError(checked.get("reason") or "publication preflight failed")
    blocked = [item for item in plan["actions"] if item["action"] == "blocked"]
    if blocked:
        raise SyncError("close plan contains blocked report(s); nothing was changed")
    repo = str(checked["repo"])
    reports = _current_reports_for_plan(plan)
    results: list[dict[str, Any]] = []
    for action in plan["actions"]:
        report = reports[action["id"]]
        if action["action"] == "close":
            comment = (
                f"Resolved by commit {action['commit']}.\n\n"
                f"Validation: {action['test']}"
            )
            result = runner.run(
                [
                    "gh",
                    "issue",
                    "close",
                    str(action["issue"]),
                    "--repo",
                    repo,
                    "--comment",
                    comment,
                ]
            )
            if result.returncode != 0:
                raise SyncError(
                    f"cannot close issue #{action['issue']}: "
                    + (result.stderr.strip() or result.stdout.strip())
                )
        _rewrite_report_link(
            report,
            repo=repo,
            issue=int(action["issue"]),
            url=str(action.get("url", "")),
            state="closed",
        )
        results.append(action)
    return {"command": "close", "dry_run": False, "repo": repo, "results": results}


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--repo", help="expected GitHub owner/name; must match origin")
    parser.add_argument("--maintenance", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="read-only publication plan")
    _common_arguments(plan)
    plan.add_argument("--report", action="append")
    plan.add_argument("--cycle", type=int, required=True)

    push = subparsers.add_parser("push", help="publish or update reviewed reports")
    _common_arguments(push)
    push.add_argument("--report", action="append")
    push.add_argument("--cycle", type=int, required=True)
    push.add_argument("--yes", action="store_true", help="apply the printed plan")

    pull = subparsers.add_parser("pull", help="reconcile issue markers into local metadata")
    _common_arguments(pull)
    pull.add_argument("--yes", action="store_true", help="apply local metadata updates")

    close = subparsers.add_parser("close", help="close issues for locally resolved reports")
    _common_arguments(close)
    close.add_argument("--report", action="append", required=True)
    close.add_argument("--commit", required=True)
    close.add_argument("--test", required=True)
    close.add_argument("--yes", action="store_true", help="close issues and update metadata")
    return parser


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = CommandRunner()
    try:
        require_maintenance(args.maintenance)
        if args.command in ("plan", "push"):
            plan = build_push_plan(
                repo_root=args.repo_root.resolve(),
                expected_repo=args.repo,
                identifiers=args.report,
                cycle=args.cycle,
                runner=runner,
            )
            if args.command == "plan" or not args.yes:
                _print_json(plan)
                return 0 if plan["preflight"]["available"] else 2
            result = apply_push_plan(plan, runner)
            _print_json(result)
            return 0
        if args.command == "pull":
            plan = build_pull_plan(
                repo_root=args.repo_root.resolve(),
                expected_repo=args.repo,
                runner=runner,
            )
            if not args.yes:
                _print_json(plan)
                return 0 if plan["preflight"]["available"] else 2
            result = apply_pull_plan(plan)
            _print_json(result)
            return 0
        if args.command == "close":
            plan = build_close_plan(
                repo_root=args.repo_root.resolve(),
                expected_repo=args.repo,
                identifiers=args.report,
                commit=args.commit,
                test_evidence=args.test,
                runner=runner,
            )
            if not args.yes:
                _print_json(plan)
                return 0 if plan["preflight"]["available"] else 2
            result = apply_close_plan(plan, runner)
            _print_json(result)
            return 0
    except (OSError, SyncError, improve_log.ReportError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
