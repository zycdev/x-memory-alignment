#!/usr/bin/env python3
"""Mechanical guards for cross-agent memory alignment artifacts.

This tool deliberately does not judge semantic relevance or decide what memory
to import. Agents and users own those decisions. It validates the language-
neutral metadata, approval shape, scope, fingerprints, and provenance loops.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "camd/v1"
# Meaning normalization is versioned because a fingerprint written by one
# adapter must stay comparable with one computed later by the other adapter or
# by a future release. Changing the rule requires a new version, not a silent
# edit, or previously applied records stop matching and duplicates reappear.
NORMALIZATION = "camd-norm/v1"
# Both adapters emit exactly these mapping labels. A typo previously degraded an
# auditable claim about how a project was aligned into unvalidated prose.
MAPPING_VALUES = ("same-root", "confirmed-remote", "user-mapping")
AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ID_RE = re.compile(r"^CAMD-[A-Z0-9][A-Z0-9_-]{0,63}$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NORMALIZATION_RE = re.compile(r"^camd-norm/v[1-9][0-9]*$")
PROJECT_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
CHECKLIST_RE = re.compile(r"<!--\s*camd:checklist\s+(\{[^\n]*\})\s*-->")
CANDIDATE_RE = re.compile(
    r"<!--\s*camd:candidate\s+(\{[^\n]*\})\s*-->(.*?)"
    r"<!--\s*/camd:candidate\s*-->",
    re.DOTALL,
)
PROPOSED_RE = re.compile(
    r"<!--\s*camd:proposed:start\s*-->(.*?)"
    r"<!--\s*camd:proposed:end\s*-->",
    re.DOTALL,
)
CHECKBOX_RE = re.compile(r"(?m)^\s*-\s*\[([ xX])\]\s+")
PROVENANCE_RE = re.compile(
    r"<!--\s*camd:provenance\s+(\{[^\n]*\})\s*-->", re.DOTALL
)
# An unresolved revision request or a marker-shaped scaffold blocks an apply.
# A word that merely appears inside otherwise finished guidance is reported as a
# warning instead, because deciding what the prose means is the agent's judgment
# rather than this script's.
SCAFFOLD_ERROR_RE = re.compile(
    r"""
      【[^】]*】                      # inline revision request
    | ^\s*(?:TODO|TBD)\b             # a line that opens as a scaffold marker
    | \b(?:TODO|TBD)\s*[:：(（]        # TODO:, TBD:, TODO(owner)
    | \bPLACEHOLDER\b                # uppercase standalone scaffold token
    """,
    re.MULTILINE | re.VERBOSE,
)
SCAFFOLD_WARN_RE = re.compile(r"\b(?:TODO|TBD|placeholder)\b", re.IGNORECASE)
HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
LIST_MARKER_RE = re.compile(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+")


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...]
    candidates: int = 0
    selected: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class Candidate:
    """One parsed checklist candidate.

    Adapters consume this instead of re-deriving the checklist grammar, so the
    structure a target writes is always the structure this script validated.
    """

    id: str
    metadata: dict[str, Any]
    selected: bool
    proposed_text: str
    body: str

    @property
    def status(self) -> str | None:
        value = self.metadata.get("status")
        return value if isinstance(value, str) else None

    @property
    def scope(self) -> str | None:
        value = self.metadata.get("target_scope")
        return value if isinstance(value, str) else None

    @property
    def fingerprint(self) -> str | None:
        value = self.metadata.get("fingerprint")
        return value if isinstance(value, str) else None

    @property
    def project(self) -> dict[str, Any]:
        value = self.metadata.get("project")
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ChecklistDocument:
    """A parsed checklist plus the verdict of its mechanical validation."""

    result: ValidationResult
    header: dict[str, Any] | None
    candidates: tuple[Candidate, ...]

    @property
    def ok(self) -> bool:
        return self.result.ok

    def approved(self) -> tuple[Candidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.selected)


def _load_json(raw: str, context: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        errors.append(f"{context}: invalid JSON metadata: {exc.msg}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{context}: metadata must be a JSON object")
        return None
    return value


def _validate_agent(value: Any, field: str, context: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not AGENT_RE.fullmatch(value):
        errors.append(f"{context}: {field} must be a lowercase agent identifier")


def _validate_scope(
    meta: dict[str, Any], context: str, errors: list[str], *, require_roots: bool = True
) -> None:
    """Validate scope metadata.

    Durable provenance records may omit the resolved roots: `protocol.md`
    forbids copying private absolute paths into target memory when a stable
    abstract identifier is sufficient. Checklists are private temporary files,
    so they must carry the full mapping the user reviews.
    """

    source_scope = meta.get("source_scope")
    target_scope = meta.get("target_scope")
    for field, value in (
        ("source_scope", source_scope),
        ("target_scope", target_scope),
    ):
        if value not in {"global", "project"}:
            errors.append(f"{context}: {field} must be global or project")

    if source_scope != target_scope:
        errors.append(f"{context}: source and target scopes must match")

    if source_scope == "project" or target_scope == "project":
        project = meta.get("project")
        if not isinstance(project, dict):
            errors.append(f"{context}: project metadata is required for project scope")
            return
        required = (
            "source_root",
            "target_root",
            "git_identity",
            "mapping",
            "key",
        )
        if not require_roots:
            required = ("git_identity", "mapping")
        for field in required:
            if not isinstance(project.get(field), str) or not project[field].strip():
                errors.append(f"{context}: project.{field} must be a non-empty string")
        for field in ("source_root", "target_root"):
            value = project.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                errors.append(f"{context}: project.{field} must be a non-empty string")
        mapping = project.get("mapping")
        if isinstance(mapping, str) and mapping not in MAPPING_VALUES:
            errors.append(
                f"{context}: project.mapping must be one of "
                + ", ".join(MAPPING_VALUES)
            )
        key = project.get("key")
        if key is not None and (
            not isinstance(key, str) or not PROJECT_KEY_RE.fullmatch(key)
        ):
            errors.append(f"{context}: project.key must be 16 lowercase hex digits")
        target_root = project.get("target_root")
        if require_roots and isinstance(target_root, str) and target_root.strip():
            if key != project_key(target_root):
                errors.append(
                    f"{context}: project.key does not match project.target_root"
                )


def _validate_record(
    meta: dict[str, Any], context: str, errors: list[str], *, checklist: bool
) -> None:
    if checklist and meta.get("schema") != SCHEMA:
        errors.append(f"{context}: schema must be {SCHEMA}")

    candidate_id = meta.get("id")
    if not isinstance(candidate_id, str) or not ID_RE.fullmatch(candidate_id):
        errors.append(f"{context}: id must match {ID_RE.pattern}")
    if checklist:
        if meta.get("status") not in {"novel", "conflict"}:
            errors.append(f"{context}: status must be novel or conflict")

    for field in ("source_agent", "target_agent", "origin_agent"):
        _validate_agent(meta.get(field), field, context, errors)

    source_agent = meta.get("source_agent")
    target_agent = meta.get("target_agent")
    origin_agent = meta.get("origin_agent")
    if source_agent == target_agent:
        errors.append(f"{context}: source_agent and target_agent must differ")
    if origin_agent == target_agent:
        errors.append(f"{context}: round-trip echo: origin_agent equals target_agent")

    fingerprint = meta.get("fingerprint")
    if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
        errors.append(f"{context}: fingerprint must be sha256 plus 64 lowercase hex digits")

    normalization = meta.get("normalization")
    if checklist:
        if normalization != NORMALIZATION:
            errors.append(f"{context}: normalization must be {NORMALIZATION}")
    elif normalization is not None and (
        not isinstance(normalization, str)
        or not NORMALIZATION_RE.fullmatch(normalization)
    ):
        errors.append(
            f"{context}: normalization must match {NORMALIZATION_RE.pattern}"
        )

    _validate_scope(meta, context, errors, require_roots=checklist)


def parse_checklist(text: str) -> ChecklistDocument:
    """Parse and validate a checklist, returning both the verdict and content.

    Adapters call this so that the applied artifact is derived from exactly the
    structure that was validated, rather than from a private second parser.
    """

    errors: list[str] = []
    warnings: list[str] = []
    headers = CHECKLIST_RE.findall(text)
    if len(headers) != 1:
        errors.append("document: expected exactly one camd:checklist marker")
        header: dict[str, Any] | None = None
    else:
        header = _load_json(headers[0], "document", errors)

    if header is not None:
        if header.get("schema") != SCHEMA:
            errors.append(f"document: schema must be {SCHEMA}")
        for field in ("source_agent", "target_agent"):
            _validate_agent(header.get(field), field, "document", errors)
        if header.get("source_agent") == header.get("target_agent"):
            errors.append("document: source_agent and target_agent must differ")

    matches = list(CANDIDATE_RE.finditer(text))
    seen_ids: set[str] = set()
    fingerprints: dict[str, tuple[str, bool]] = {}
    candidates: list[Candidate] = []
    selected = 0

    for index, match in enumerate(matches, start=1):
        context = f"candidate[{index}]"
        meta = _load_json(match.group(1), context, errors)
        body = match.group(2)
        if meta is None:
            continue

        candidate_id = meta.get("id")
        if isinstance(candidate_id, str):
            context = candidate_id
            if candidate_id in seen_ids:
                errors.append(f"{context}: duplicate candidate id")
            seen_ids.add(candidate_id)

        _validate_record(meta, context, errors, checklist=True)

        if header is not None:
            for field in ("source_agent", "target_agent"):
                if meta.get(field) != header.get(field):
                    errors.append(f"{context}: {field} differs from document direction")

        # The approval control is the checkbox outside the proposed region.
        # Proposed guidance may itself contain Markdown task syntax.
        proposed_matches = list(PROPOSED_RE.finditer(body))
        control_region = body
        for proposed_match in reversed(proposed_matches):
            control_region = (
                control_region[: proposed_match.start()]
                + control_region[proposed_match.end() :]
            )

        checkboxes = CHECKBOX_RE.findall(control_region)
        if len(checkboxes) != 1:
            errors.append(f"{context}: expected exactly one checkbox")
            is_selected = False
        else:
            is_selected = checkboxes[0].lower() == "x"
            selected += int(is_selected)
            if proposed_matches:
                if CHECKBOX_RE.search(body[: proposed_matches[0].start()]) is None:
                    errors.append(
                        f"{context}: checkbox must precede the proposed-text region"
                    )

        proposed_text = ""
        if len(proposed_matches) != 1:
            errors.append(f"{context}: expected exactly one proposed-text region")
        else:
            proposed_text = proposed_matches[0].group(1).strip()
            if is_selected:
                if not proposed_text:
                    errors.append(f"{context}: selected candidate has empty proposed text")
                if SCAFFOLD_ERROR_RE.search(proposed_text):
                    errors.append(f"{context}: selected candidate has unresolved annotation")
                elif SCAFFOLD_WARN_RE.search(proposed_text):
                    warnings.append(
                        f"{context}: proposed text mentions a scaffold word; "
                        "confirm it is intended guidance"
                    )

        fingerprint = meta.get("fingerprint")
        if isinstance(fingerprint, str) and FINGERPRINT_RE.fullmatch(fingerprint):
            identity = fingerprint_identity(meta)
            previous = fingerprints.get(identity)
            if previous is not None:
                other_id, other_selected = previous
                message = (
                    f"{context}: duplicate fingerprint shared with {other_id}"
                )
                if is_selected and other_selected:
                    errors.append(message)
                else:
                    warnings.append(message)
                fingerprints[identity] = (
                    other_id,
                    other_selected or is_selected,
                )
            else:
                fingerprints[identity] = (
                    candidate_id if isinstance(candidate_id, str) else context,
                    is_selected,
                )

        candidates.append(
            Candidate(
                id=candidate_id if isinstance(candidate_id, str) else context,
                metadata=meta,
                selected=is_selected,
                proposed_text=proposed_text,
                body=body,
            )
        )

    stray_starts = len(re.findall(r"<!--\s*camd:candidate\b", text))
    if stray_starts != len(matches):
        errors.append("document: malformed or unclosed candidate block")

    result = ValidationResult(
        tuple(errors), len(matches), selected, tuple(warnings)
    )
    return ChecklistDocument(result, header, tuple(candidates))


def validate_checklist_text(text: str) -> ValidationResult:
    return parse_checklist(text).result


def validate_provenance_text(text: str, expected_target: str | None = None) -> ValidationResult:
    errors: list[str] = []
    blocks = PROVENANCE_RE.findall(text)
    if not blocks:
        errors.append("output: no camd:provenance marker found")
        return ValidationResult(tuple(errors))

    records_seen = 0
    fingerprints: set[str] = set()
    for block_index, raw in enumerate(blocks, start=1):
        block = _load_json(raw, f"provenance[{block_index}]", errors)
        if block is None:
            continue
        if block.get("schema") != SCHEMA:
            errors.append(f"provenance[{block_index}]: schema must be {SCHEMA}")
        records = block.get("records")
        if not isinstance(records, list) or not records:
            errors.append(f"provenance[{block_index}]: records must be a non-empty list")
            continue
        for record_index, record in enumerate(records, start=1):
            context = f"provenance[{block_index}].records[{record_index}]"
            if not isinstance(record, dict):
                errors.append(f"{context}: record must be an object")
                continue
            records_seen += 1
            _validate_record(record, context, errors, checklist=False)
            if expected_target and record.get("target_agent") != expected_target:
                errors.append(f"{context}: target_agent differs from expected target")
            fingerprint = record.get("fingerprint")
            if isinstance(fingerprint, str):
                identity = fingerprint_identity(record)
                if identity in fingerprints:
                    errors.append(f"{context}: duplicate fingerprint")
                fingerprints.add(identity)

    return ValidationResult(tuple(errors), records_seen, records_seen)


def fingerprint_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_private_exclusive(path: Path, text: str) -> None:
    """Create one private review artifact without replacing an existing path."""

    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def normalize_meaning(text: str) -> str:
    """Reduce prose to a comparable form for fingerprinting (`camd-norm/v1`).

    Normalization removes formatting noise that changes bytes without changing
    meaning, and nothing else. It never decides whether two differently worded
    rules mean the same thing, which remains the agent's judgment. Both adapters
    share this function so that a fingerprint means the same thing on both sides
    of the workflow.
    """

    value = unicodedata.normalize("NFKC", text)
    value = LIST_MARKER_RE.sub("", value)
    value = HEADING_RE.sub(lambda match: match.group(2), value)
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.DOTALL)
    value = re.sub(r"[`*_]+", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def fingerprint_text(text: str) -> str:
    """Fingerprint normalized meaning rather than raw bytes."""

    return fingerprint_bytes(normalize_meaning(text).encode("utf-8"))


def fingerprint_identity(metadata: dict[str, Any]) -> str:
    """Return a version-qualified identity for one meaning fingerprint.

    Provenance written before the field was introduced used the only rule that
    existed at the time, `camd-norm/v1`. Keeping that explicit fallback prevents
    those records from being silently reinterpreted if a future release adopts
    v2, while new checklist candidates must always name the current rule.
    """

    normalization = metadata.get("normalization")
    if not isinstance(normalization, str):
        normalization = "camd-norm/v1"
    return f"{normalization}|{metadata.get('fingerprint', '')}"


def project_key(root: str | Path) -> str:
    """Return a stable disambiguator for a project root.

    Repository identity is the primary project label, but it is `none` for a
    repository without a stable remote, so several distinct projects can share
    one label inside a single target memory. This key separates them without
    writing a private absolute path into durable memory. It identifies a project
    within one installation; cross-host identity remains `git_identity`.
    """

    resolved = Path(root).resolve().as_posix()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def build_manifest(root_class: str, files: dict[str, str]) -> dict[str, Any]:
    """Describe a read-only source tree by digest, without private paths."""

    return {"schema": SCHEMA, "root_class": root_class, "files": dict(files)}


def compare_manifest(
    recorded: dict[str, Any], current: dict[str, str], *, root_class: str
) -> list[str]:
    """Report every difference that would mean a read-only source changed."""

    problems = []
    if recorded.get("schema") != SCHEMA:
        problems.append(f"manifest: schema must be {SCHEMA}")
    if recorded.get("root_class") != root_class:
        problems.append(f"manifest: root_class must be {root_class}")
    previous = recorded.get("files")
    if not isinstance(previous, dict):
        problems.append("manifest: files must be a JSON object")
        return problems
    for path, digest in previous.items():
        if path not in current:
            problems.append(f"source file disappeared: {path}")
        elif current[path] != digest:
            problems.append(f"source file changed: {path}")
    for path in current:
        if path not in previous:
            problems.append(f"source file appeared: {path}")
    return problems


def _print_result(result: ValidationResult, label: str, *, strict: bool = False) -> int:
    for warning in result.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if result.ok and not (strict and result.warnings):
        print(
            f"OK: {label}: {result.candidates} record(s), "
            f"{result.selected} selected"
        )
        return 0
    for error in result.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if strict and result.warnings and result.ok:
        print("ERROR: warnings present and --strict was requested", file=sys.stderr)
    return 1


def _read_path(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    checklist = subparsers.add_parser(
        "validate-checklist", help="validate an edited checklist before apply"
    )
    checklist.add_argument("path")
    checklist.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )

    provenance = subparsers.add_parser(
        "audit-output", help="audit provenance embedded in a target output"
    )
    provenance.add_argument("path")
    provenance.add_argument("--target-agent")

    fingerprint = subparsers.add_parser(
        "fingerprint", help="print a SHA-256 fingerprint without file contents"
    )
    source = fingerprint.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("--stdin", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-checklist":
        return _print_result(
            validate_checklist_text(_read_path(args.path)),
            "checklist",
            strict=args.strict,
        )
    if args.command == "audit-output":
        return _print_result(
            validate_provenance_text(
                _read_path(args.path), expected_target=args.target_agent
            ),
            "provenance",
        )
    if args.command == "fingerprint":
        data = Path(args.file).read_bytes() if args.file else sys.stdin.buffer.read()
        print(fingerprint_bytes(data))
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
