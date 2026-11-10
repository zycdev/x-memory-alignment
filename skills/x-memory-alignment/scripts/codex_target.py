#!/usr/bin/env python3
"""Claude-Code-to-Codex adapter for cross-agent memory alignment.

The adapter performs mechanical discovery, project alignment, approved-only
application, and verification. The agent and user decide which guidance is
durable, useful, novel, or conflicting.

Every root is explicit so tests cannot fall through to a live installation.
The only target mutation is one new ad-hoc extension note per successful run;
generated Codex registries, summaries, databases, and indexes are read-only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alignment_guard  # noqa: E402


SOURCE_AGENT = "claude-code"
TARGET_AGENT = "codex"

GLOBAL_GUIDANCE = "CLAUDE.md"
GLOBAL_MEMORY_DIR = "memory"
PROJECTS_DIR = "projects"
PROJECT_MEMORY_DIR = "memory"
PROJECT_MEMORY_INDEX = "MEMORY.md"

CODEX_MEMORY_DIR = "memories"
CODEX_MEMORY_INDEX = "MEMORY.md"
CODEX_NOTES_DIR = Path("memories/extensions/ad_hoc/notes")
NOTE_SUFFIX = "claude-memory-alignment"

HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")


class AdapterError(RuntimeError):
    """A condition that must stop the run before target memory changes."""


# Shared with the Claude adapter so that one fingerprint means the same thing in
# both directions. See `alignment_guard.NORMALIZATION`.
normalize_meaning = alignment_guard.normalize_meaning
fingerprint_text = alignment_guard.fingerprint_text


def digest_file(path: Path) -> str:
    return alignment_guard.fingerprint_bytes(path.read_bytes())


def _outline(text: str, limit: int) -> tuple[str, tuple[str, ...]]:
    headings = [match.group(2).strip() for match in HEADING_RE.finditer(text)]
    title = headings[0] if headings else ""
    return title, tuple(headings[1 : limit + 1])


def project_slug(project_root: Path) -> str:
    resolved = project_root.resolve().as_posix()
    return "".join(char if char.isalnum() else "-" for char in resolved)


def git_identity(project_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    url = completed.stdout.strip()
    if not url:
        return None
    match = re.match(r"^(?:[a-z]+://)?(?:[^@/]+@)?([^/:]+)[/:](.+?)(?:\.git)?$", url)
    if not match:
        return None
    host, path = match.groups()
    return f"{host}/{path.strip('/')}"


def git_toplevel(project_root: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.strip()).resolve()


def align_project(
    claude_home: Path,
    target_root: Path,
    *,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Align a Claude project-memory directory with an actual target project."""

    target = target_root.resolve()
    if not target.is_dir():
        raise AdapterError(f"target project root is not a directory: {target_root}")
    source = (source_root or target_root).resolve()
    source_identity = git_identity(source) if source.is_dir() else None
    target_identity = git_identity(target)
    if source == target:
        mapping = "same-root"
    elif source_identity and target_identity and source_identity == target_identity:
        mapping = "confirmed-remote"
    else:
        mapping = "user-mapping"
    identity = target_identity or source_identity or "none"
    memory_dir = (
        claude_home / PROJECTS_DIR / project_slug(source) / PROJECT_MEMORY_DIR
    )
    return {
        "source_root": source.as_posix(),
        "target_root": target.as_posix(),
        "git_toplevel": (
            git_toplevel(target).as_posix() if git_toplevel(target) else None
        ),
        "git_identity": identity,
        "mapping": mapping,
        "source_slug": project_slug(source),
        "memory_dir": memory_dir.as_posix(),
        "memory_dir_exists": memory_dir.is_dir(),
    }


@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    role: str
    scope: str
    project_identity: str | None
    size_bytes: int
    sha256: str
    modified: str
    title: str
    outline: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "role": self.role,
            "scope": self.scope,
            "project_identity": self.project_identity,
            "bytes": self.size_bytes,
            "sha256": self.sha256,
            "modified": self.modified,
            "title": self.title,
            "outline": list(self.outline),
        }


def _allowed_source_paths(
    claude_home: Path, project_roots: Sequence[Path]
) -> dict[Path, tuple[str, str, str | None]]:
    """Return exact allowed Markdown paths and their role/scope/identity."""

    allowed: dict[Path, tuple[str, str, str | None]] = {}
    global_file = claude_home / GLOBAL_GUIDANCE
    if global_file.is_file():
        allowed[global_file] = ("candidate", "global", None)

    global_memory = claude_home / GLOBAL_MEMORY_DIR
    if global_memory.is_dir():
        for path in sorted(global_memory.glob("*.md")):
            if path.is_symlink():
                continue
            role = "registry" if path.name == PROJECT_MEMORY_INDEX else "candidate"
            allowed[path] = (role, "global", None)

    for root in project_roots:
        alignment = align_project(claude_home, root)
        memory_dir = Path(alignment["memory_dir"])
        if not memory_dir.is_dir():
            continue
        identity = str(alignment["git_identity"])
        for path in sorted(memory_dir.glob("*.md")):
            if path.is_symlink():
                continue
            role = "registry" if path.name == PROJECT_MEMORY_INDEX else "candidate"
            allowed[path] = (role, "project", identity)
    return allowed


def inventory_source(
    claude_home: Path,
    project_roots: Sequence[Path] = (),
    *,
    outline_limit: int = 8,
) -> dict[str, Any]:
    """Describe only explicitly allowed Claude semantic memory sources."""

    if not claude_home.is_dir():
        raise AdapterError(f"Claude home is not a directory: {claude_home}")
    files: list[SourceFile] = []
    for path, (role, scope, identity) in _allowed_source_paths(
        claude_home, project_roots
    ).items():
        text = path.read_text(encoding="utf-8", errors="replace")
        title, outline = _outline(text, outline_limit)
        stat = path.stat()
        files.append(
            SourceFile(
                relative_path=path.relative_to(claude_home).as_posix(),
                role=role,
                scope=scope,
                project_identity=identity,
                size_bytes=stat.st_size,
                sha256=digest_file(path),
                modified=datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                title=title,
                outline=outline,
            )
        )
    return {
        "agent": SOURCE_AGENT,
        "root_class": "claude-home",
        "files": [item.to_json() for item in files],
        "counts": {
            role: sum(1 for item in files if item.role == role)
            for role in ("registry", "candidate")
        },
        "not_traversed": [
            "projects not named by an explicit project root",
            "plans, transcripts, histories, settings, skills, caches, and databases",
        ],
    }


def read_source(
    claude_home: Path, relative_path: str, project_roots: Sequence[Path] = ()
) -> str:
    root = claude_home.resolve()
    candidate = (root / relative_path).resolve()
    allowed = {
        path.resolve(): role
        for path, role in _allowed_source_paths(claude_home, project_roots).items()
    }
    if candidate not in allowed:
        raise AdapterError(f"refusing to read unsupported Claude source: {relative_path}")
    if root != candidate and root not in candidate.parents:
        raise AdapterError(f"path escapes the Claude home: {relative_path}")
    return candidate.read_text(encoding="utf-8")


def source_manifest(
    claude_home: Path, project_roots: Sequence[Path] = ()
) -> dict[str, Any]:
    inventory = inventory_source(claude_home, project_roots)
    return alignment_guard.build_manifest(
        "claude-home",
        {item["path"]: item["sha256"] for item in inventory["files"]},
    )


def compare_source_manifest(
    claude_home: Path,
    manifest: dict[str, Any],
    project_roots: Sequence[Path] = (),
) -> list[str]:
    return alignment_guard.compare_manifest(
        manifest,
        source_manifest(claude_home, project_roots)["files"],
        root_class="claude-home",
    )


def _provenance_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in alignment_guard.PROVENANCE_RE.findall(text):
        try:
            block = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = block.get("records")
        if isinstance(found, list):
            records.extend(item for item in found if isinstance(item, dict))
    return records


def notes_dir(codex_home: Path) -> Path:
    return codex_home / CODEX_NOTES_DIR


def _note_files(codex_home: Path) -> list[Path]:
    directory = notes_dir(codex_home)
    if not directory.is_dir():
        return []
    return [
        path
        for path in sorted(directory.glob("*.md"))
        if path.is_file() and not path.is_symlink()
    ]


def collect_applied_fingerprints(codex_home: Path) -> dict[str, str]:
    applied: dict[str, str] = {}
    for path in _note_files(codex_home):
        text = path.read_text(encoding="utf-8", errors="replace")
        for record in _provenance_records(text):
            fingerprint = record.get("fingerprint")
            if isinstance(fingerprint, str):
                applied.setdefault(alignment_guard.fingerprint_identity(record), path.name)
    return applied


def inventory_target(
    codex_home: Path, project_roots: Sequence[Path] = (), *, outline_limit: int = 8
) -> dict[str, Any]:
    """Describe Codex memory without exposing note bodies."""

    if not codex_home.is_dir():
        raise AdapterError(f"Codex home is not a directory: {codex_home}")
    registry = codex_home / CODEX_MEMORY_DIR / CODEX_MEMORY_INDEX
    registry_info: dict[str, Any] = {"exists": registry.is_file()}
    if registry.is_file():
        text = registry.read_text(encoding="utf-8", errors="replace")
        title, outline = _outline(text, outline_limit)
        registry_info.update(
            {
                "bytes": registry.stat().st_size,
                "sha256": digest_file(registry),
                "title": title,
                "outline": list(outline),
            }
        )

    notes: list[dict[str, Any]] = []
    unprovenanced = 0
    for path in _note_files(codex_home):
        text = path.read_text(encoding="utf-8", errors="replace")
        title, outline = _outline(text, outline_limit)
        records = _provenance_records(text)
        if not records:
            unprovenanced += 1
        notes.append(
            {
                "file": path.name,
                "title": title,
                "outline": list(outline),
                "bytes": path.stat().st_size,
                "provenance_records": len(records),
            }
        )

    contexts = []
    for root in project_roots:
        resolved = root.resolve()
        contexts.append(
            {
                "project_identity": git_identity(resolved) or "none",
                "instruction_files": [
                    name
                    for name in ("AGENTS.md", "CLAUDE.md")
                    if (resolved / name).exists()
                ],
            }
        )
    return {
        "agent": TARGET_AGENT,
        "root_class": "codex-home",
        "registry": registry_info,
        "notes": notes,
        "project_contexts": contexts,
        "applied_fingerprints": sorted(collect_applied_fingerprints(codex_home)),
        "unprovenanced_notes": unprovenanced,
        "historical_origin_review_required": unprovenanced > 0,
    }


def _provenance_comment(records: Sequence[dict[str, Any]]) -> str:
    payload = {"schema": alignment_guard.SCHEMA, "records": list(records)}
    return "<!-- camd:provenance " + json.dumps(payload, ensure_ascii=False) + " -->"


def _record_for(candidate: alignment_guard.Candidate, applied_at: str) -> dict[str, Any]:
    meta = candidate.metadata
    record = {
        "id": candidate.id,
        "source_agent": meta.get("source_agent"),
        "target_agent": meta.get("target_agent"),
        "origin_agent": meta.get("origin_agent"),
        "source_scope": meta.get("source_scope"),
        "target_scope": meta.get("target_scope"),
        "normalization": meta.get("normalization"),
        "fingerprint": meta.get("fingerprint"),
        "applied_at": applied_at,
    }
    if candidate.project:
        project = {
            "git_identity": candidate.project.get("git_identity"),
            "mapping": candidate.project.get("mapping"),
        }
        target_root = candidate.project.get("target_root")
        if isinstance(target_root, str) and target_root:
            project["key"] = alignment_guard.project_key(target_root)
        record["project"] = project
    return record


def _safe_heading(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value).strip() or "unidentified-project"


def render_note(
    candidates: Sequence[alignment_guard.Candidate], *, applied_at: str
) -> str:
    global_items = [item for item in candidates if item.scope == "global"]
    project_groups: dict[tuple[str, str | None], list[alignment_guard.Candidate]] = {}
    for candidate in candidates:
        if candidate.scope != "project":
            continue
        identity = _safe_heading(str(candidate.project.get("git_identity", "none")))
        key = str(candidate.project.get("key")) if identity == "none" else None
        project_groups.setdefault((identity, key), []).append(candidate)

    lines = [
        "# Claude-to-Codex Memory Alignment",
        "",
        f"Applied at `{applied_at}` after an editable checklist approval.",
        "This note contains only selected target-useful guidance.",
    ]
    if global_items:
        lines.extend(["", "## Global guidance"])
        for candidate in global_items:
            lines.extend(
                [
                    "",
                    _provenance_comment([_record_for(candidate, applied_at)]),
                    candidate.proposed_text.strip(),
                ]
            )
    for (identity, key), items in project_groups.items():
        label = identity if key is None else f"{identity} [key: {key}]"
        lines.extend(["", f"## Project: {label}"])
        for candidate in items:
            lines.extend(
                [
                    "",
                    _provenance_comment([_record_for(candidate, applied_at)]),
                    candidate.proposed_text.strip(),
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _timestamp(value: str | None) -> str:
    if value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AdapterError(f"invalid ISO timestamp: {value}") from exc
        return value
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _filename_stamp(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _choose_note_path(directory: Path, timestamp: str) -> Path:
    stem = f"{_filename_stamp(timestamp)}-{NOTE_SUFFIX}"
    path = directory / f"{stem}.md"
    suffix = 2
    while path.exists():
        path = directory / f"{stem}-{suffix}.md"
        suffix += 1
    return path


def _exclusive_create(path: Path, text: str) -> None:
    """Create a private complete note without ever replacing an existing path.

    Delegates to the shared helper, which adds refusal to follow a symlink at
    the target path to the guarantees this function already had.
    """

    alignment_guard.write_private_exclusive(path, text)


@dataclass
class ApplyOutcome:
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self, *, dry_run: bool) -> dict[str, Any]:
        return {
            "direction": f"{SOURCE_AGENT}->{TARGET_AGENT}",
            "dry_run": dry_run,
            "applied": self.applied,
            "skipped": self.skipped,
            "files": self.files,
            "warnings": self.warnings,
        }


def _confirmed_projects(project_roots: Sequence[Path]) -> set[Path]:
    confirmed: set[Path] = set()
    for root in project_roots:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise AdapterError(f"confirmed project root is not a directory: {root}")
        confirmed.add(resolved)
    return confirmed


def apply_checklist(
    checklist_path: Path,
    codex_home: Path,
    *,
    project_roots: Sequence[Path] = (),
    dry_run: bool = False,
    create_notes_dir: bool = False,
    acknowledge_warnings: bool = False,
    applied_at: str | None = None,
) -> ApplyOutcome:
    """Create one Codex ad-hoc note from approved checklist items."""

    if not codex_home.is_dir():
        raise AdapterError(f"Codex home is not a directory: {codex_home}")
    document = alignment_guard.parse_checklist(
        checklist_path.read_text(encoding="utf-8")
    )
    if not document.ok:
        raise AdapterError(
            "checklist failed validation:\n  "
            + "\n  ".join(document.result.errors)
        )
    if document.result.warnings and not acknowledge_warnings:
        raise AdapterError(
            "checklist has warnings that must be reconciled explicitly:\n  "
            + "\n  ".join(document.result.warnings)
        )
    header = document.header or {}
    if (header.get("source_agent"), header.get("target_agent")) != (
        SOURCE_AGENT,
        TARGET_AGENT,
    ):
        raise AdapterError(
            f"this adapter applies only {SOURCE_AGENT} to {TARGET_AGENT} checklists"
        )

    outcome = ApplyOutcome(warnings=list(document.result.warnings))
    approved = document.approved()
    if not approved:
        return outcome

    confirmed = _confirmed_projects(project_roots)
    for candidate in document.approved():
        if candidate.metadata.get("origin_agent") == TARGET_AGENT:
            raise AdapterError(f"{candidate.id}: refusing a round-trip echo")
        if candidate.scope == "project":
            target_root = candidate.project.get("target_root")
            if not isinstance(target_root, str) or not target_root:
                raise AdapterError(f"{candidate.id}: project.target_root is required")
            if Path(target_root).resolve() not in confirmed:
                raise AdapterError(
                    f"{candidate.id}: project target_root is not among the "
                    "explicitly confirmed project roots"
                )

    directory = notes_dir(codex_home)
    if not directory.is_dir() and not create_notes_dir:
        raise AdapterError(
            f"Codex ad-hoc notes directory does not exist: {directory}. "
            "Confirm the target, then rerun with --create-notes-dir."
        )

    applied_fingerprints = collect_applied_fingerprints(codex_home)
    selected: list[alignment_guard.Candidate] = []
    for candidate in approved:
        fingerprint = alignment_guard.fingerprint_identity(candidate.metadata)
        if fingerprint in applied_fingerprints:
            outcome.skipped.append(
                {
                    "id": candidate.id,
                    "reason": "already-applied",
                    "location": applied_fingerprints[fingerprint],
                }
            )
            continue
        selected.append(candidate)
        outcome.applied.append(
            {
                "id": candidate.id,
                "scope": candidate.scope,
                "project_identity": (
                    candidate.project.get("git_identity")
                    if candidate.scope == "project"
                    else None
                ),
            }
        )
        applied_fingerprints[fingerprint] = "planned-note"

    if not selected:
        return outcome

    stamp = _timestamp(applied_at)
    path = _choose_note_path(directory, stamp)
    outcome.files = [path.as_posix()]
    if dry_run:
        return outcome

    created_dirs: list[Path] = []
    if not directory.is_dir():
        missing = [directory, *(parent for parent in directory.parents if not parent.exists())]
        directory.mkdir(parents=True)
        created_dirs = list(reversed(missing))
    created_note = False
    try:
        _exclusive_create(path, render_note(selected, applied_at=stamp))
        created_note = True
        problems = verify_note(path, expect_ids=[item.id for item in selected])
        if problems:
            raise AdapterError(
                "verification failed after writing; the note was removed:\n  "
                + "\n  ".join(problems)
            )
    except Exception:
        if created_note:
            path.unlink(missing_ok=True)
        for created in reversed(created_dirs):
            try:
                created.rmdir()
            except OSError:
                pass
        raise
    return outcome


def verify_note(path: Path, *, expect_ids: Sequence[str] = ()) -> list[str]:
    problems: list[str] = []
    if not path.is_file():
        return [f"note does not exist: {path}"]
    text = path.read_text(encoding="utf-8", errors="replace")
    result = alignment_guard.validate_provenance_text(text, expected_target=TARGET_AGENT)
    problems.extend(result.errors)
    records = _provenance_records(text)
    seen_ids = {
        record.get("id") for record in records if isinstance(record.get("id"), str)
    }
    for expected in expect_ids:
        if expected not in seen_ids:
            problems.append(f"{expected}: approved candidate is missing after apply")
    if alignment_guard.SCAFFOLD_ERROR_RE.search(text):
        problems.append("note contains an unresolved annotation")
    return problems


def verify_target(codex_home: Path) -> list[str]:
    problems: list[str] = []
    for path in _note_files(codex_home):
        text = path.read_text(encoding="utf-8", errors="replace")
        if alignment_guard.PROVENANCE_RE.search(text):
            problems.extend(f"{path.name}: {item}" for item in verify_note(path))
    return problems


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser(
        "inventory", help="describe allowed source and target memory"
    )
    inventory.add_argument("--claude-home", required=True)
    inventory.add_argument("--codex-home", required=True)
    inventory.add_argument("--project-root", action="append", default=[])
    inventory.add_argument("--outline-limit", type=int, default=8)

    show = subparsers.add_parser(
        "show-source", help="print one allowed Claude source file"
    )
    show.add_argument("--claude-home", required=True)
    show.add_argument("--path", required=True)
    show.add_argument("--project-root", action="append", default=[])

    align = subparsers.add_parser(
        "align-project", help="align Claude project memory with a target project"
    )
    align.add_argument("--claude-home", required=True)
    align.add_argument("--target-root", required=True)
    align.add_argument("--source-root")

    snapshot = subparsers.add_parser(
        "snapshot-source", help="record source digests for immutability checks"
    )
    snapshot.add_argument("--claude-home", required=True)
    snapshot.add_argument("--project-root", action="append", default=[])
    snapshot.add_argument("--out", required=True)

    fingerprint = subparsers.add_parser(
        "fingerprint", help="fingerprint normalized meaning"
    )
    group = fingerprint.add_mutually_exclusive_group(required=True)
    group.add_argument("--file")
    group.add_argument("--stdin", action="store_true")

    apply_parser = subparsers.add_parser(
        "apply", help="create one Codex note from an approved checklist"
    )
    apply_parser.add_argument("--checklist", required=True)
    apply_parser.add_argument("--codex-home", required=True)
    apply_parser.add_argument("--project-root", action="append", default=[])
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--create-notes-dir", action="store_true")
    apply_parser.add_argument("--acknowledge-warnings", action="store_true")
    apply_parser.add_argument("--timestamp")

    verify = subparsers.add_parser(
        "verify", help="audit Codex notes and optional Claude source immutability"
    )
    verify.add_argument("--codex-home", required=True)
    verify.add_argument("--note", action="append", default=[])
    verify.add_argument("--claude-home")
    verify.add_argument("--project-root", action="append", default=[])
    verify.add_argument("--source-manifest")
    return parser


def _paths(values: Sequence[str]) -> list[Path]:
    return [Path(value) for value in values]


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inventory":
            roots = _paths(args.project_root)
            _print_json(
                {
                    "source": inventory_source(
                        Path(args.claude_home), roots, outline_limit=args.outline_limit
                    ),
                    "target": inventory_target(
                        Path(args.codex_home), roots, outline_limit=args.outline_limit
                    ),
                }
            )
            return 0
        if args.command == "show-source":
            sys.stdout.write(
                read_source(
                    Path(args.claude_home), args.path, _paths(args.project_root)
                )
            )
            return 0
        if args.command == "align-project":
            _print_json(
                align_project(
                    Path(args.claude_home),
                    Path(args.target_root),
                    source_root=(Path(args.source_root) if args.source_root else None),
                )
            )
            return 0
        if args.command == "snapshot-source":
            manifest = source_manifest(
                Path(args.claude_home), _paths(args.project_root)
            )
            try:
                alignment_guard.write_private_exclusive(
                    Path(args.out), json.dumps(manifest, indent=2) + "\n"
                )
            except FileExistsError as error:
                raise AdapterError(
                    f"snapshot destination already exists: {args.out}; choose a "
                    "new --out path, or remove the reviewed temporary file first"
                ) from error
            print(f"OK: recorded {len(manifest['files'])} source digest(s)")
            return 0
        if args.command == "fingerprint":
            data = (
                Path(args.file).read_text(encoding="utf-8")
                if args.file
                else sys.stdin.read()
            )
            print(fingerprint_text(data))
            return 0
        if args.command == "apply":
            outcome = apply_checklist(
                Path(args.checklist),
                Path(args.codex_home),
                project_roots=_paths(args.project_root),
                dry_run=args.dry_run,
                create_notes_dir=args.create_notes_dir,
                acknowledge_warnings=args.acknowledge_warnings,
                applied_at=args.timestamp,
            )
            _print_json(outcome.to_json(dry_run=args.dry_run))
            return 0
        if args.command == "verify":
            problems: list[str] = []
            if args.note:
                for note in args.note:
                    problems.extend(verify_note(Path(note)))
            else:
                problems.extend(verify_target(Path(args.codex_home)))
            if bool(args.claude_home) != bool(args.source_manifest):
                raise AdapterError(
                    "--claude-home and --source-manifest must be supplied together"
                )
            if args.claude_home:
                manifest = json.loads(
                    Path(args.source_manifest).read_text(encoding="utf-8")
                )
                problems.extend(
                    compare_source_manifest(
                        Path(args.claude_home),
                        manifest,
                        _paths(args.project_root),
                    )
                )
            if problems:
                raise AdapterError("verification failed:\n  " + "\n  ".join(problems))
            print("OK: Codex target and source immutability checks passed")
            return 0
    except (AdapterError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
