#!/usr/bin/env python3
"""Offline capture of defects, friction, ideas, and surprises about this skill.

Capture is a local, offline, private act. This script therefore never contacts
the network and never runs `git` or `gh`; it imports no network library at all,
and a test enforces that. Publication to a public tracker is a separate,
explicitly authorized maintenance act implemented only by ``improve_sync.py``.

Two further boundaries hold at all times:

- Reports live under a private state directory, never inside the repository and
  never inside either agent's memory surface. An improvement report describes the
  tool; memory describes the user's work. The two must not mix.
- A report is written only when the user asked for it, or accepted a suggestion
  to record one. Every report records which of the two happened.

Mechanical redaction covers only what can be identified without interpretation:
absolute home paths, electronic mail addresses, host names, distillation
metadata comments, long digests, and the private evidence block. Deciding whether
ordinary prose is sensitive is left to the agents and the user.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import distill_guard  # noqa: E402  (local module beside this script)

SCHEMA = "report/v1"
TRIAGE_SCHEMA = "triage/v1"
TOOL_NAME = "cross-agent-memory-distiller"

KINDS = ("bug", "friction", "idea", "docs", "question", "surprise")
SEVERITIES = ("blocking", "major", "minor", "cosmetic")
COMPONENTS = (
    "guard",
    "claude-adapter",
    "codex-adapter",
    "protocol",
    "install",
    "docs",
    "loop",
    "unknown",
)
STATUSES = ("new", "triaged", "published", "resolved", "dropped")
OPEN_STATUSES = ("new", "triaged", "published")
AGENTS = ("claude-code", "codex", "unknown")
# Ordinary use may create a report only on the user's request, or after the user
# accepted a suggestion to record one. The distinction is recorded rather than
# assumed, so an unprompted report is visible as such.
INITIATORS = ("user-request", "accepted-suggestion")

ID_RE = re.compile(r"^CAMD-R-\d{8}-[0-9A-HJ-NP-Z]{6}$")
ID_ALPHABET = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O

REPORT_HEADER_RE = re.compile(r"<!--\s*camd:report\s+(\{[^\n]*\})\s*-->")
EVIDENCE_RE = re.compile(
    r"<!--\s*camd:evidence:private\s*-->(.*?)<!--\s*/camd:evidence:private\s*-->",
    re.DOTALL,
)
TRIAGE_HEADER_RE = re.compile(r"<!--\s*camd:triage\s+(\{[^\n]*\})\s*-->")
TRIAGE_ITEM_RE = re.compile(
    r"<!--\s*camd:triage:item\s+(\{[^\n]*\})\s*-->(.*?)<!--\s*/camd:triage:item\s*-->",
    re.DOTALL,
)

# Maintenance thresholds. They only ever produce a recommendation; nothing in
# this script starts work, opens a session, or notifies anyone.
OPEN_REPORT_THRESHOLD = 5
STALE_REPORT_DAYS = 14


class ReportError(RuntimeError):
    """A condition that must stop the command before anything is written."""


# ---------------------------------------------------------------------------
# Mechanical redaction rules
# ---------------------------------------------------------------------------


def _home_path_pattern() -> re.Pattern[str]:
    home = os.environ.get("HOME", "")
    parts = [r"/home/[^/\s]+/\S*", r"/Users/[^/\s]+/\S*", r"~/\S+"]
    if home:
        parts.insert(0, re.escape(home) + r"\S*")
    return re.compile("|".join(parts))


REDACTION_RULES: tuple[tuple[str, str], ...] = (
    ("absolute-home-path", ""),  # pattern built per call, see _rules()
    ("email-address", r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    ("host-name", r"\b(?:[\w-]+\.)+(?:com|org|net|io|dev|ai|cn|edu|gov|local)\b"),
    ("ssh-target", r"\b[\w.-]+@[\w.-]+:[\w./-]+"),
    ("distillation-metadata", r"<!--\s*camd:(?:provenance|candidate|checklist)\b[^>]*-->"),
    ("long-digest", r"\b(?:sha256:)?[0-9a-f]{32,}\b"),
)


def _rules() -> list[tuple[str, re.Pattern[str]]]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for name, pattern in REDACTION_RULES:
        if name == "absolute-home-path":
            compiled.append((name, _home_path_pattern()))
        else:
            compiled.append((name, re.compile(pattern)))
    return compiled


def scan_redaction(text: str) -> list[dict[str, Any]]:
    """Locate publication blockers without echoing what they contain.

    Findings report a rule name, a position, and a length. They never include
    the matched text, because a redaction report that quotes private material
    reintroduces the very problem it exists to prevent.
    """

    findings: list[dict[str, Any]] = []
    lines = text.splitlines()
    for name, pattern in _rules():
        for number, line in enumerate(lines, start=1):
            for match in pattern.finditer(line):
                findings.append(
                    {
                        "rule": name,
                        "line": number,
                        "column": match.start() + 1,
                        "length": len(match.group(0)),
                    }
                )
    if EVIDENCE_RE.search(text):
        findings.append({"rule": "private-evidence-block", "line": 0, "column": 0, "length": 0})
    return sorted(findings, key=lambda item: (item["line"], item["column"], item["rule"]))


def strip_private_evidence(text: str) -> str:
    return EVIDENCE_RE.sub("", text).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def state_dir() -> Path:
    override = os.environ.get("CAMD_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or (
        Path(os.environ.get("HOME", str(Path.home()))) / ".local" / "state"
    )
    return Path(base) / TOOL_NAME


def reports_dir() -> Path:
    return state_dir() / "reports"


def _ensure_private_dir(path: Path) -> None:
    """Create the queue directories owner-only, including the state root."""

    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_new_private_file(path: Path, text: str) -> None:
    """Create a file that must not already exist, readable only by its owner.

    Delegates to the shared helper so that this queue gets the same guarantees
    as every other private artifact: exclusive creation, owner-only mode, no
    symlink following, a flush to disk, and removal of a partial file if the
    write fails. A partial report would otherwise survive as an unreadable entry.
    """

    distill_guard.write_private_exclusive(path, text)


def _rewrite_private_file(path: Path, text: str) -> None:
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_identifier(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    suffix = "".join(secrets.choice(ID_ALPHABET) for _ in range(6))
    return f"CAMD-R-{moment:%Y%m%d}-{suffix}"


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    path: Path
    metadata: dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return str(self.metadata.get("id", ""))

    @property
    def status(self) -> str:
        return str(self.metadata.get("status", ""))

    @property
    def title(self) -> str:
        for line in self.body.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return ""

    def public_view(self) -> str:
        return strip_private_evidence(self.body)

    def public_body(self) -> str:
        """The public view without the title line, which the packet supplies."""

        lines = self.public_view().splitlines()
        if lines and lines[0].startswith("# "):
            lines = lines[1:]
        return "\n".join(lines).strip()


def render_report(metadata: dict[str, Any], body: str) -> str:
    header = "<!-- camd:report " + json.dumps(metadata, ensure_ascii=False) + " -->"
    return f"{header}\n{body.rstrip()}\n"


def parse_report(path: Path) -> Report:
    text = path.read_text(encoding="utf-8")
    match = REPORT_HEADER_RE.search(text)
    if not match:
        raise ReportError(f"{path.name}: no camd:report marker")
    try:
        metadata = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ReportError(f"{path.name}: invalid JSON metadata: {exc.msg}") from exc
    if not isinstance(metadata, dict):
        raise ReportError(f"{path.name}: metadata must be a JSON object")
    body = text[match.end() :].lstrip("\n")
    return Report(path, metadata, body)


def scan_reports() -> tuple[list[Report], list[str]]:
    """Load every readable report while isolating malformed neighbours."""

    directory = reports_dir()
    if not directory.is_dir():
        return [], []
    reports = []
    warnings = []
    for path in sorted(directory.glob("CAMD-R-*.md")):
        try:
            reports.append(parse_report(path))
        except (OSError, ReportError) as error:
            warnings.append(str(error))
    return reports, warnings


def load_reports() -> list[Report]:
    """Return readable reports; callers needing diagnostics use scan_reports."""

    return scan_reports()[0]


def validate_metadata(metadata: dict[str, Any], context: str) -> list[str]:
    errors: list[str] = []

    def check(field: str, allowed: Sequence[str]) -> None:
        value = metadata.get(field)
        if value not in allowed:
            errors.append(f"{context}: {field} must be one of {', '.join(allowed)}")

    if metadata.get("schema") != SCHEMA:
        errors.append(f"{context}: schema must be {SCHEMA}")
    identifier = metadata.get("id")
    if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier):
        errors.append(f"{context}: id must match {ID_RE.pattern}")
    check("kind", KINDS)
    check("severity", SEVERITIES)
    check("component", COMPONENTS)
    check("status", STATUSES)
    check("agent", AGENTS)
    check("initiated_by", INITIATORS)
    fingerprint = metadata.get("fingerprint")
    if not isinstance(fingerprint, str) or not distill_guard.FINGERPRINT_RE.fullmatch(
        fingerprint
    ):
        errors.append(f"{context}: fingerprint must be sha256 plus 64 hex digits")
    if metadata.get("normalization") != distill_guard.NORMALIZATION:
        errors.append(
            f"{context}: normalization must be {distill_guard.NORMALIZATION}"
        )
    occurrences = metadata.get("occurrences")
    if not isinstance(occurrences, int) or occurrences < 1:
        errors.append(f"{context}: occurrences must be a positive integer")
    for field in ("created", "updated"):
        value = metadata.get(field)
        if not isinstance(value, str):
            errors.append(f"{context}: {field} must be a timestamp")
            continue
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{context}: {field} is not a valid ISO timestamp")
    return errors


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def add_report(
    *,
    kind: str,
    severity: str,
    component: str,
    title: str,
    body: str,
    initiated_by: str,
    agent: str = "unknown",
    evidence: str | None = None,
    skill_version: str | None = None,
) -> dict[str, Any]:
    """Create a report, or record another occurrence of an existing one."""

    if kind not in KINDS:
        raise ReportError(f"kind must be one of {', '.join(KINDS)}")
    if severity not in SEVERITIES:
        raise ReportError(f"severity must be one of {', '.join(SEVERITIES)}")
    if component not in COMPONENTS:
        raise ReportError(f"component must be one of {', '.join(COMPONENTS)}")
    if initiated_by not in INITIATORS:
        raise ReportError(
            "initiated_by must state how this report was requested: "
            + ", ".join(INITIATORS)
        )
    if agent not in AGENTS:
        raise ReportError(f"agent must be one of {', '.join(AGENTS)}")
    if not title.strip():
        raise ReportError("title must not be empty")

    fingerprint = distill_guard.fingerprint_text(f"{title}\n{body}")
    reports, warnings = scan_reports()
    existing = next(
        (
            report
            for report in reports
            if report.metadata.get("fingerprint") == fingerprint
            and report.status in OPEN_STATUSES
        ),
        None,
    )
    if existing is not None:
        result = _record_occurrence(existing, agent)
        result["warnings"] = warnings
        return result

    _ensure_private_dir(reports_dir())
    identifier = new_identifier()
    path = reports_dir() / f"{identifier}.md"
    while path.exists():  # pragma: no cover - identifier collision is improbable
        identifier = new_identifier()
        path = reports_dir() / f"{identifier}.md"

    stamp = _now()
    metadata = {
        "schema": SCHEMA,
        "id": identifier,
        "kind": kind,
        "severity": severity,
        "component": component,
        "agent": agent,
        "initiated_by": initiated_by,
        "status": "new",
        "visibility": "private",
        "fingerprint": fingerprint,
        "normalization": distill_guard.NORMALIZATION,
        "occurrences": 1,
        "skill_version": skill_version or "unknown",
        "created": stamp,
        "updated": stamp,
    }
    sections = [f"# {title.strip()}", "", body.strip()]
    if evidence:
        sections.extend(
            [
                "",
                "<!-- camd:evidence:private -->",
                evidence.strip(),
                "<!-- /camd:evidence:private -->",
            ]
        )
    body_text = "\n".join(sections)
    _write_new_private_file(path, render_report(metadata, body_text))
    return {
        "action": "created",
        "id": identifier,
        "path": path.as_posix(),
        "occurrences": 1,
        # The report body, not the file: a report's own metadata fingerprint is
        # not a redaction blocker.
        "redaction_flags": len(scan_redaction(body_text)),
        "warnings": warnings,
    }


def _record_occurrence(report: Report, agent: str) -> dict[str, Any]:
    metadata = dict(report.metadata)
    metadata["occurrences"] = int(metadata.get("occurrences", 1)) + 1
    metadata["updated"] = _now()
    body = report.body.rstrip()
    line = f"- Seen again on {metadata['updated']} by {agent}."
    if "## Occurrences" in body:
        body = f"{body}\n{line}"
    else:
        body = f"{body}\n\n## Occurrences\n\n{line}"
    _rewrite_private_file(report.path, render_report(metadata, body))
    return {
        "action": "occurrence-added",
        "id": report.id,
        "path": report.path.as_posix(),
        "occurrences": metadata["occurrences"],
    }


def set_status(identifier: str, status: str, note: str | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ReportError(f"status must be one of {', '.join(STATUSES)}")
    report = find_report(identifier)
    metadata = dict(report.metadata)
    metadata["status"] = status
    metadata["updated"] = _now()
    body = report.body.rstrip()
    if note:
        body = f"{body}\n\n## Status note {metadata['updated']}\n\n{note.strip()}"
    _rewrite_private_file(report.path, render_report(metadata, body))
    return {"id": report.id, "status": status, "path": report.path.as_posix()}


def find_report(identifier: str) -> Report:
    for report in load_reports():
        if report.id == identifier:
            return report
    raise ReportError(f"no such report: {identifier}")


def summarize(report: Report) -> dict[str, Any]:
    return {
        "id": report.id,
        "title": report.title,
        "kind": report.metadata.get("kind"),
        "severity": report.metadata.get("severity"),
        "component": report.metadata.get("component"),
        "status": report.status,
        "occurrences": report.metadata.get("occurrences"),
        "agent": report.metadata.get("agent"),
        "initiated_by": report.metadata.get("initiated_by"),
        "created": report.metadata.get("created"),
        "updated": report.metadata.get("updated"),
        "redaction_flags": len(scan_redaction(report.body)),
    }


def status_overview() -> dict[str, Any]:
    reports, warnings = scan_reports()
    open_reports = [item for item in reports if item.status in OPEN_STATUSES]
    now = datetime.now(timezone.utc)
    stale = []
    for report in open_reports:
        created = report.metadata.get("created")
        try:
            moment = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - moment).days >= STALE_REPORT_DAYS:
            stale.append(report.id)

    blocking = [item.id for item in open_reports if item.metadata.get("severity") == "blocking"]
    reasons = []
    if len(open_reports) >= OPEN_REPORT_THRESHOLD:
        reasons.append(f"{len(open_reports)} open reports (threshold {OPEN_REPORT_THRESHOLD})")
    if blocking:
        reasons.append(f"{len(blocking)} blocking report(s)")
    if stale:
        reasons.append(f"{len(stale)} report(s) older than {STALE_REPORT_DAYS} days")

    def tally(field: str, values: Sequence[str]) -> dict[str, int]:
        return {
            value: sum(1 for item in reports if item.metadata.get(field) == value)
            for value in values
            if any(item.metadata.get(field) == value for item in reports)
        }

    return {
        "state_dir": state_dir().as_posix(),
        "total": len(reports),
        "open": len(open_reports),
        "by_status": tally("status", STATUSES),
        "by_kind": tally("kind", KINDS),
        "by_severity": tally("severity", SEVERITIES),
        "with_redaction_flags": sum(
            1 for item in reports if scan_redaction(item.body)
        ),
        "maintenance_recommended": bool(reasons),
        "maintenance_reasons": reasons,
        "warnings": warnings,
        # This offline helper never probes GitHub. Availability is established
        # only by improve_sync.py inside an explicitly authorized maintenance
        # session, so ordinary status checks remain network-free.
        "publication": {
            "available": None,
            "reason": (
                "not checked by the offline logger; run improve_sync.py plan "
                "inside an explicit maintenance session"
            ),
        },
    }


def render_packet(reports: Sequence[Report]) -> str:
    """Render a read-only maintenance packet.

    The packet is an output artifact. It carries no checkboxes and confers no
    authority: dispositions are decided by the agents and the user in a
    maintenance session, not by this file. Its grammar is deliberately its own,
    because a memory checklist's agents, scopes, and origins have no meaning
    here.
    """

    generated = _now()
    header = {
        "schema": TRIAGE_SCHEMA,
        "generated": generated,
        "reports": len(reports),
        "tool": "improve_log",
        "read_only": True,
    }
    lines = [
        "<!-- camd:triage " + json.dumps(header, ensure_ascii=False) + " -->",
        "# Maintenance packet",
        "",
        f"Generated {generated} from the local queue. This file is a read-only",
        "summary: it records no decision and authorizes no change. Private",
        "evidence blocks are excluded.",
    ]
    for report in reports:
        item = {
            "schema": TRIAGE_SCHEMA,
            "id": report.id,
            "kind": report.metadata.get("kind"),
            "severity": report.metadata.get("severity"),
            "component": report.metadata.get("component"),
            "status": report.status,
            "occurrences": report.metadata.get("occurrences"),
            "fingerprint": report.metadata.get("fingerprint"),
            "normalization": report.metadata.get("normalization"),
            "redaction_flags": len(scan_redaction(report.body)),
        }
        lines.extend(
            [
                "",
                "<!-- camd:triage:item " + json.dumps(item, ensure_ascii=False) + " -->",
                f"## {report.id}: {report.title}",
                "",
                report.public_body(),
                "<!-- /camd:triage:item -->",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def validate_packet(text: str) -> list[str]:
    """Validate a packet with this tool's own grammar."""

    errors: list[str] = []
    headers = TRIAGE_HEADER_RE.findall(text)
    if len(headers) != 1:
        errors.append("packet: expected exactly one camd:triage marker")
    else:
        try:
            header = json.loads(headers[0])
        except json.JSONDecodeError as exc:
            errors.append(f"packet: invalid JSON header: {exc.msg}")
            header = {}
        if header.get("schema") != TRIAGE_SCHEMA:
            errors.append(f"packet: schema must be {TRIAGE_SCHEMA}")
        if header.get("read_only") is not True:
            errors.append("packet: read_only must be true")

    reports, report_warnings = scan_reports()
    errors.extend(report_warnings)
    known = {report.id: report for report in reports}
    items = list(TRIAGE_ITEM_RE.finditer(text))
    starts = len(re.findall(r"<!--\s*camd:triage:item\b", text))
    if starts != len(items):
        errors.append("packet: malformed or unclosed item block")
    for index, match in enumerate(items, start=1):
        context = f"item[{index}]"
        try:
            item = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            errors.append(f"{context}: invalid JSON metadata: {exc.msg}")
            continue
        identifier = item.get("id")
        context = identifier if isinstance(identifier, str) else context
        if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier):
            errors.append(f"{context}: id must match {ID_RE.pattern}")
            continue
        report = known.get(identifier)
        if report is None:
            errors.append(f"{context}: no matching report in the local queue")
            continue
        if item.get("status") != report.status:
            errors.append(f"{context}: status differs from the local report")
        if item.get("fingerprint") != report.metadata.get("fingerprint"):
            errors.append(f"{context}: fingerprint differs from the local report")
        if EVIDENCE_RE.search(match.group(2)):
            errors.append(f"{context}: private evidence must not appear in a packet")
    return errors


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _read_text_argument(inline: str | None, from_file: str | None) -> str:
    if inline is not None:
        return inline
    if from_file is None:
        return ""
    if from_file == "-":
        return sys.stdin.read()
    return Path(from_file).read_text(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="record a report, or another occurrence")
    add.add_argument("--kind", required=True, choices=KINDS)
    add.add_argument("--severity", default="minor", choices=SEVERITIES)
    add.add_argument("--component", default="unknown", choices=COMPONENTS)
    add.add_argument("--title", required=True)
    add.add_argument("--body")
    add.add_argument("--body-file")
    add.add_argument("--evidence-file")
    add.add_argument("--agent", default="unknown", choices=AGENTS)
    add.add_argument(
        "--initiated-by",
        required=True,
        choices=INITIATORS,
        help="how this report was requested; unprompted capture is not supported",
    )
    add.add_argument("--skill-version")

    listing = subparsers.add_parser("list", help="list reports")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--kind", choices=KINDS)
    listing.add_argument("--component", choices=COMPONENTS)
    listing.add_argument("--open", action="store_true", help="only open reports")

    show = subparsers.add_parser("show", help="print one report")
    show.add_argument("id")
    show.add_argument(
        "--public",
        action="store_true",
        help="omit the private evidence block and list redaction flags",
    )

    subparsers.add_parser("status", help="counts and a maintenance recommendation")

    mark = subparsers.add_parser("set-status", help="change the status of one report")
    mark.add_argument("id")
    mark.add_argument("--status", required=True, choices=STATUSES)
    mark.add_argument("--note")

    validate = subparsers.add_parser("validate", help="validate reports or a packet")
    validate.add_argument("--packet")

    draft = subparsers.add_parser(
        "draft", help="render a read-only maintenance packet"
    )
    draft.add_argument("--out")
    draft.add_argument("--open", action="store_true", default=True)
    draft.add_argument("--all", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "add":
            _print_json(
                add_report(
                    kind=args.kind,
                    severity=args.severity,
                    component=args.component,
                    title=args.title,
                    body=_read_text_argument(args.body, args.body_file),
                    initiated_by=args.initiated_by,
                    agent=args.agent,
                    evidence=(
                        Path(args.evidence_file).read_text(encoding="utf-8")
                        if args.evidence_file
                        else None
                    ),
                    skill_version=args.skill_version,
                )
            )
            return 0

        if args.command == "list":
            reports, warnings = scan_reports()
            for warning in warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
            if args.open:
                reports = [item for item in reports if item.status in OPEN_STATUSES]
            for field in ("status", "kind", "component"):
                wanted = getattr(args, field)
                if wanted:
                    reports = [
                        item for item in reports if item.metadata.get(field) == wanted
                    ]
            _print_json([summarize(item) for item in reports])
            return 0

        if args.command == "show":
            report = find_report(args.id)
            if args.public:
                _print_json(
                    {
                        "id": report.id,
                        "redaction_flags": scan_redaction(report.body),
                        "public_view": report.public_view(),
                    }
                )
            else:
                sys.stdout.write(report.path.read_text(encoding="utf-8"))
            return 0

        if args.command == "status":
            _print_json(status_overview())
            return 0

        if args.command == "set-status":
            _print_json(set_status(args.id, args.status, args.note))
            return 0

        if args.command == "validate":
            if args.packet:
                errors = validate_packet(
                    Path(args.packet).read_text(encoding="utf-8")
                )
                label = "packet"
            else:
                reports, errors = scan_reports()
                for report in reports:
                    errors.extend(validate_metadata(report.metadata, report.id))
                label = "reports"
            if errors:
                for error in errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 1
            print(f"OK: {label} validated")
            return 0

        if args.command == "draft":
            reports, warnings = scan_reports()
            for warning in warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
            if not args.all:
                reports = [item for item in reports if item.status in OPEN_STATUSES]
            packet = render_packet(reports)
            if args.out:
                destination = Path(args.out)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _rewrite_private_file(destination, packet)
                _print_json(
                    {"path": destination.as_posix(), "reports": len(reports)}
                )
            else:
                sys.stdout.write(packet)
            return 0
    except (ReportError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
