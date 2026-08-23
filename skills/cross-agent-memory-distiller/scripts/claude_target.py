#!/usr/bin/env python3
"""Codex-to-Claude-Code adapter for cross-agent memory distillation.

The adapter performs only mechanical work: discovering allowed source and target
memory, aligning projects by resolved path and repository identity, applying an
already approved checklist to Claude-native memory locations, and verifying the
result. Deciding what is durable, useful, novel, or conflicting remains with the
agent and the user.

Safety properties enforced here:

- Source memory under the Codex home is never opened for writing, and only
  allowed roles are readable through this adapter at all.
- Every root is explicit. There is no implicit default that could reach a live
  installation during a test.
- ``apply`` writes only candidates that the shared guard accepted and the user
  checked, is idempotent by fingerprint, and restores every touched file if any
  part of the run fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import distill_guard  # noqa: E402  (local module beside this script)


SOURCE_AGENT = "codex"
TARGET_AGENT = "claude-code"

# Claude-native locations. Global guidance maps only to the user-level
# instruction file; project guidance maps only to the auto-memory directory of
# the confirmed project. No global auto-memory tier is invented here.
GLOBAL_INSTRUCTIONS = "CLAUDE.md"
PROJECTS_DIR = "projects"
PROJECT_MEMORY_DIR = "memory"
PROJECT_MEMORY_INDEX = "MEMORY.md"

GLOBAL_SECTION_START = "<!-- camd:distilled:start -->"
GLOBAL_SECTION_END = "<!-- camd:distilled:end -->"
INDEX_SECTION_START = "<!-- camd:index:start -->"
INDEX_SECTION_END = "<!-- camd:index:end -->"

MEMORY_TYPES = ("user", "feedback", "project", "reference")
DEFAULT_MEMORY_TYPE = "feedback"
# A memory file name becomes a path, so it is validated rather than trusted.
# Without this, a hint such as "../../elsewhere" would write outside the aligned
# project's memory directory. Underscores are allowed because the installed
# convention names memory files `feedback_<topic>` and `project_<topic>`; the
# property this check must preserve is the absence of path separators, traversal,
# and surprising characters, not a particular word separator.
MEMORY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_MEMORY_NAMES = frozenset({PROJECT_MEMORY_INDEX.removesuffix(".md").lower()})

HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
LIST_MARKER_RE = re.compile(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+")
SENTENCE_END_RE = re.compile(r"(?<=[.。！？!?])\s")


class AdapterError(RuntimeError):
    """A condition that must stop the run before any target memory changes."""


# ---------------------------------------------------------------------------
# Digests and meaning fingerprints
# ---------------------------------------------------------------------------


# Normalization and fingerprinting are shared so that a fingerprint written by
# one adapter keeps its meaning for the other. See `distill_guard.NORMALIZATION`.
normalize_meaning = distill_guard.normalize_meaning
fingerprint_text = distill_guard.fingerprint_text


def digest_file(path: Path) -> str:
    return distill_guard.fingerprint_bytes(path.read_bytes())


# ---------------------------------------------------------------------------
# Source discovery: Codex memory
# ---------------------------------------------------------------------------

# Roles:
#   registry  - generated index, read for discovery only, never copied verbatim
#   candidate - durable notes that may contain importable guidance
#   context   - instructions read for comparison, not for import
#   excluded  - everything else, reported as counts and reasons only
EXCLUSION_REASONS = {
    "session-derived": "session or rollout derived material",
    "generated-derivative": "generated summary or raw dump of the registry",
    "not-memory": "skills, plugins, or other non-memory content",
    "unsupported": "outside the allowed memory surface",
}


@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    role: str
    size_bytes: int
    sha256: str
    modified: str
    title: str
    outline: tuple[str, ...]
    origin_agents: tuple[str, ...] = ()
    provenance_records: int = 0

    @property
    def echo_risk(self) -> bool:
        """True when this note is recorded as having originated at the target."""

        return TARGET_AGENT in self.origin_agents

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "role": self.role,
            "bytes": self.size_bytes,
            "sha256": self.sha256,
            "modified": self.modified,
            "title": self.title,
            "outline": list(self.outline),
            "provenance_records": self.provenance_records,
            "origin_agents": list(self.origin_agents),
            "echo_risk": self.echo_risk,
        }


def classify_source(relative_path: str) -> tuple[str, str]:
    """Classify one path under the Codex home without reading it."""

    parts = relative_path.split("/")
    if parts[0] == "AGENTS.md":
        return "context", "global Codex instructions, comparison only"
    if parts[0] != "memories":
        return "excluded", "unsupported"
    rest = parts[1:]
    if not rest:
        return "excluded", "unsupported"
    if any(part.startswith(".") for part in rest):
        return "excluded", "unsupported"
    if rest[0] == "rollout_summaries":
        return "excluded", "session-derived"
    if rest[0] in {"raw_memories.md", "memory_summary.md"}:
        return "excluded", "generated-derivative"
    if rest[0] == "skills":
        return "excluded", "not-memory"
    if rest == [PROJECT_MEMORY_INDEX]:
        return "registry", "generated Codex memory registry, discovery input"
    if rest[:2] == ["extensions", "ad_hoc"]:
        if rest[2:3] == ["notes"] and len(rest) == 4 and rest[3].endswith(".md"):
            return "candidate", "durable ad-hoc note"
        if rest[2:] == ["instructions.md"]:
            return "context", "extension instructions, comparison only"
        return "excluded", "unsupported"
    return "excluded", "unsupported"


def _outline(text: str, limit: int) -> tuple[str, tuple[str, ...]]:
    headings = [match.group(2).strip() for match in HEADING_RE.finditer(text)]
    title = headings[0] if headings else ""
    return title, tuple(headings[1 : limit + 1])


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def inventory_source(codex_home: Path, *, outline_limit: int = 8) -> dict[str, Any]:
    """Describe the allowed Codex memory surface without dumping its content."""

    if not codex_home.is_dir():
        raise AdapterError(f"Codex home is not a directory: {codex_home}")

    files: list[SourceFile] = []
    excluded: dict[str, int] = {}
    for path in _iter_files(codex_home):
        relative_path = path.relative_to(codex_home).as_posix()
        role, reason = classify_source(relative_path)
        if role == "excluded":
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        title, outline = _outline(text, outline_limit)
        stat = path.stat()
        records = _provenance_records(text)
        origins = tuple(
            sorted(
                {
                    record["origin_agent"]
                    for record in records
                    if isinstance(record.get("origin_agent"), str)
                }
            )
        )
        files.append(
            SourceFile(
                relative_path=relative_path,
                role=role,
                size_bytes=stat.st_size,
                sha256=digest_file(path),
                modified=datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                title=title,
                outline=outline,
                origin_agents=origins,
                provenance_records=len(records),
            )
        )

    candidates = [item for item in files if item.role == "candidate"]
    unprovenanced = [item for item in candidates if item.provenance_records == 0]
    return {
        "agent": SOURCE_AGENT,
        "root_class": "codex-home",
        "files": [item.to_json() for item in files],
        "counts": {
            role: sum(1 for item in files if item.role == role)
            for role in ("registry", "candidate", "context")
        },
        "excluded": [
            {"reason": reason, "detail": EXCLUSION_REASONS[reason], "files": count}
            for reason, count in sorted(excluded.items())
        ],
        # A note that this workflow wrote records its origin agent, so an echo is
        # detected mechanically. A note predating the workflow, or written by
        # hand, carries no provenance: its origin can only be established by
        # reading it, and a manual cross-agent merge can hide there.
        "echo_risk_notes": [item.relative_path for item in candidates if item.echo_risk],
        "unprovenanced_notes": len(unprovenanced),
        "historical_origin_review_required": bool(unprovenanced),
        "not_traversed": [
            "session records, rollout summaries, and generated registry derivatives",
            "skills, plugins, settings, histories, caches, databases, and credentials",
        ],
    }


def read_source(codex_home: Path, relative_path: str) -> str:
    """Read one source file, refusing any path outside the allowed roles."""

    candidate = (codex_home / relative_path).resolve()
    root = codex_home.resolve()
    if root not in candidate.parents:
        raise AdapterError(f"path escapes the Codex home: {relative_path}")
    role, reason = classify_source(candidate.relative_to(root).as_posix())
    if role == "excluded":
        raise AdapterError(
            f"refusing to read {relative_path}: {EXCLUSION_REASONS[reason]}"
        )
    if not candidate.is_file():
        raise AdapterError(f"source file not found: {relative_path}")
    return candidate.read_text(encoding="utf-8")


def source_manifest(codex_home: Path) -> dict[str, Any]:
    inventory = inventory_source(codex_home)
    return distill_guard.build_manifest(
        "codex-home",
        {item["path"]: item["sha256"] for item in inventory["files"]},
    )


def compare_source_manifest(codex_home: Path, manifest: dict[str, Any]) -> list[str]:
    """Return the differences that would mean the read-only source changed."""

    return distill_guard.compare_manifest(
        manifest,
        source_manifest(codex_home)["files"],
        root_class="codex-home",
    )


# ---------------------------------------------------------------------------
# Project alignment
# ---------------------------------------------------------------------------


def project_slug(project_root: Path) -> str:
    """Derive the Claude project directory name from a resolved root path."""

    resolved = project_root.resolve().as_posix()
    return "".join(char if char.isalnum() else "-" for char in resolved)


def git_identity(project_root: Path) -> str | None:
    """Return ``host/owner/repository`` when a stable remote exists."""

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
    project_root: Path,
    *,
    source_root: str | None = None,
) -> dict[str, Any]:
    """Resolve a project to its Claude memory directory and state the mapping.

    The mapping label records how the alignment was established so that the
    checklist can show the user why two roots are believed to be one project.
    Alignment is never inferred from a directory basename.
    """

    resolved = project_root.resolve()
    if not resolved.is_dir():
        raise AdapterError(f"project root is not a directory: {project_root}")
    toplevel = git_toplevel(resolved)
    identity = git_identity(resolved)
    slug = project_slug(resolved)
    memory_dir = claude_home / PROJECTS_DIR / slug / PROJECT_MEMORY_DIR

    declared_source = source_root or resolved.as_posix()
    if Path(declared_source).resolve().as_posix() == resolved.as_posix():
        mapping = "same-root"
    elif identity:
        mapping = "confirmed-remote"
    else:
        mapping = "user-mapping"

    return {
        "source_root": declared_source,
        "target_root": resolved.as_posix(),
        "git_toplevel": toplevel.as_posix() if toplevel else None,
        "git_identity": identity or "none",
        "mapping": mapping,
        "slug": slug,
        "memory_dir": memory_dir.as_posix(),
        "memory_dir_exists": memory_dir.is_dir(),
        "project_instructions": (
            (resolved / "CLAUDE.md").as_posix()
            if (resolved / "CLAUDE.md").exists()
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Target discovery: Claude Code memory
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Read the flat fields this adapter needs from a memory file header."""

    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) > 1:
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        fields[key.strip()] = value
    return fields


def project_memory_dir(claude_home: Path, slug: str) -> Path:
    return claude_home / PROJECTS_DIR / slug / PROJECT_MEMORY_DIR


def inventory_target(
    claude_home: Path, project_root: Path | None = None
) -> dict[str, Any]:
    """Describe target memory at index level for relevance and deduplication."""

    if not claude_home.is_dir():
        raise AdapterError(f"Claude home is not a directory: {claude_home}")

    global_file = claude_home / GLOBAL_INSTRUCTIONS
    global_info: dict[str, Any] = {"path": global_file.as_posix(), "exists": False}
    if global_file.is_file():
        text = global_file.read_text(encoding="utf-8")
        title, outline = _outline(text, 12)
        global_info.update(
            {
                "exists": True,
                "bytes": global_file.stat().st_size,
                "sha256": digest_file(global_file),
                "title": title,
                "outline": list(outline),
                "has_distilled_section": GLOBAL_SECTION_START in text,
            }
        )

    result: dict[str, Any] = {
        "agent": TARGET_AGENT,
        "root_class": "claude-home",
        "global_instructions": global_info,
        "applied_fingerprints": [],
    }

    if project_root is not None:
        alignment = align_project(claude_home, project_root)
        memory_dir = Path(alignment["memory_dir"])
        entries = []
        if memory_dir.is_dir():
            for path in sorted(memory_dir.glob("*.md")):
                if path.name == PROJECT_MEMORY_INDEX:
                    continue
                fields = _parse_frontmatter(
                    path.read_text(encoding="utf-8", errors="replace")
                )
                entries.append(
                    {
                        "file": path.name,
                        "name": fields.get("name", path.stem),
                        "description": fields.get("description", ""),
                        "type": fields.get("type", ""),
                        "bytes": path.stat().st_size,
                    }
                )
        result["project"] = {
            **alignment,
            "index_exists": (memory_dir / PROJECT_MEMORY_INDEX).is_file(),
            "memories": entries,
        }

    result["applied_fingerprints"] = sorted(
        collect_applied_fingerprints(
            claude_home, [project_root] if project_root is not None else []
        )
    )
    return result


def _provenance_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in distill_guard.PROVENANCE_RE.findall(text):
        try:
            block = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = block.get("records")
        if isinstance(found, list):
            records.extend(item for item in found if isinstance(item, dict))
    return records


def _applied_files(
    claude_home: Path, project_roots: Sequence[Path] = ()
) -> list[Path]:
    paths = [claude_home / GLOBAL_INSTRUCTIONS]
    for project_root in project_roots:
        memory_dir = project_memory_dir(claude_home, project_slug(project_root))
        if memory_dir.is_dir():
            paths.extend(sorted(memory_dir.glob("*.md")))
    return [path for path in paths if path.is_file()]


def collect_applied_fingerprints(
    claude_home: Path, project_roots: Sequence[Path] = ()
) -> dict[str, str]:
    """Map every fingerprint already present in target memory to its location."""

    applied: dict[str, str] = {}
    for path in _applied_files(claude_home, project_roots):
        text = path.read_text(encoding="utf-8", errors="replace")
        for record in _provenance_records(text):
            fingerprint = record.get("fingerprint")
            if isinstance(fingerprint, str):
                applied.setdefault(distill_guard.fingerprint_identity(record), path.name)
    return applied


# ---------------------------------------------------------------------------
# Rendering Claude-native artifacts
# ---------------------------------------------------------------------------


def _provenance_comment(records: Sequence[dict[str, Any]]) -> str:
    payload = {"schema": distill_guard.SCHEMA, "records": list(records)}
    return "<!-- camd:provenance " + json.dumps(payload, ensure_ascii=False) + " -->"


def _record_for(candidate: distill_guard.Candidate, applied_at: str) -> dict[str, Any]:
    """Keep provenance minimal: enough to audit and deduplicate, nothing more."""

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
    project = candidate.project
    if project:
        details = {
            "git_identity": project.get("git_identity"),
            "mapping": project.get("mapping"),
        }
        target_root = project.get("target_root")
        if isinstance(target_root, str) and target_root:
            details["key"] = distill_guard.project_key(target_root)
        record["project"] = details
    return record


def slugify(text: str, *, words: int = 6) -> str:
    value = unicodedata.normalize("NFKC", text).strip().lower()
    value = re.sub(r"[^\w\s-]", " ", value, flags=re.UNICODE)
    tokens = [token for token in re.split(r"[\s_-]+", value) if token][:words]
    slug = "-".join(tokens).strip("-")
    return slug or "distilled-memory"


def _first_sentence(text: str, limit: int = 200) -> str:
    flattened = re.sub(r"\s+", " ", LIST_MARKER_RE.sub("", text)).strip()
    sentence = SENTENCE_END_RE.split(flattened, maxsplit=1)[0].strip()
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


def _hint(candidate: distill_guard.Candidate, key: str) -> str | None:
    hint = candidate.metadata.get("target_hint")
    if isinstance(hint, dict):
        value = hint.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def reject_scaffold(candidate_id: str, text: str, where: str) -> None:
    """Refuse rendered output that still carries an unresolved annotation.

    The guard checks the approved text, but a target hint is metadata rather
    than proposed text, so an annotation can still reach a rendered file name,
    title, or description. This check covers only what this run renders; the
    user's pre-existing target content is never reclassified as new output.
    """

    if distill_guard.SCAFFOLD_ERROR_RE.search(text):
        raise AdapterError(
            f"{candidate_id}: unresolved annotation in the rendered {where}"
        )


def validated_memory_name(candidate: distill_guard.Candidate, fallback: str) -> str:
    name = _hint(candidate, "name") or fallback
    if not MEMORY_NAME_RE.fullmatch(name):
        raise AdapterError(
            f"{candidate.id}: memory name must match {MEMORY_NAME_RE.pattern}; "
            f"received {name!r}"
        )
    if name.lower() in RESERVED_MEMORY_NAMES:
        raise AdapterError(
            f"{candidate.id}: {name!r} is reserved for the memory index"
        )
    return name


def render_memory_file(
    candidate: distill_guard.Candidate, *, name: str, applied_at: str
) -> str:
    """Render one Claude project memory file in the native front-matter format."""

    memory_type = _hint(candidate, "memory_type") or DEFAULT_MEMORY_TYPE
    if memory_type not in MEMORY_TYPES:
        raise AdapterError(
            f"{candidate.id}: memory_type must be one of {', '.join(MEMORY_TYPES)}"
        )
    description = _hint(candidate, "description") or _first_sentence(
        candidate.proposed_text
    )
    body = candidate.proposed_text.strip()
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "metadata:\n"
        "  node_type: memory\n"
        f"  type: {memory_type}\n"
        f"  modified: {applied_at}\n"
        "---\n\n"
        f"{body}\n\n"
        f"{_provenance_comment([_record_for(candidate, applied_at)])}\n"
    )


def render_index_entry(
    candidate: distill_guard.Candidate, *, file_name: str
) -> str:
    title = _hint(candidate, "title") or _first_sentence(
        candidate.proposed_text, limit=70
    )
    title = title.replace("[", "(").replace("]", ")")
    hook = _hint(candidate, "description") or _first_sentence(
        candidate.proposed_text, limit=140
    )
    return f"- [{title}]({file_name}) — {hook}"


def upsert_global_section(
    text: str, blocks: Sequence[str], *, heading: str
) -> str:
    """Insert distilled guidance additively into the user instruction file."""

    addition = "\n\n".join(block.strip() for block in blocks)
    if GLOBAL_SECTION_START in text and GLOBAL_SECTION_END in text:
        head, _, remainder = text.partition(GLOBAL_SECTION_END)
        return f"{head.rstrip()}\n\n{addition}\n{GLOBAL_SECTION_END}{remainder}"
    prefix = text.rstrip("\n")
    separator = "\n\n" if prefix else ""
    return (
        f"{prefix}{separator}{GLOBAL_SECTION_START}\n"
        f"## {heading}\n\n"
        f"{addition}\n"
        f"{GLOBAL_SECTION_END}\n"
    )


def upsert_index_section(
    text: str, entries: Sequence[str], *, heading: str
) -> str:
    addition = "\n".join(entries)
    if INDEX_SECTION_START in text and INDEX_SECTION_END in text:
        head, _, remainder = text.partition(INDEX_SECTION_END)
        return f"{head.rstrip()}\n{addition}\n{INDEX_SECTION_END}{remainder}"
    prefix = text.rstrip("\n")
    separator = "\n\n" if prefix else ""
    return (
        f"{prefix}{separator}{INDEX_SECTION_START}\n"
        f"## {heading}\n\n"
        f"{addition}\n"
        f"{INDEX_SECTION_END}\n"
    )


# ---------------------------------------------------------------------------
# Atomic application with rollback
# ---------------------------------------------------------------------------


@dataclass
class Transaction:
    """Collect file writes, then commit them atomically or restore the tree."""

    writes: dict[Path, str] = field(default_factory=dict)
    _previous: dict[Path, str | None] = field(default_factory=dict)
    _created_dirs: list[Path] = field(default_factory=list)

    def stage(self, path: Path, text: str) -> None:
        self.writes[path] = text

    def commit(self) -> None:
        try:
            for path, text in self.writes.items():
                self._previous[path] = (
                    path.read_text(encoding="utf-8") if path.is_file() else None
                )
                for parent in reversed(list(path.parents)):
                    if not parent.exists():
                        parent.mkdir()
                        self._created_dirs.append(parent)
                self._atomic_write(path, text)
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        for path, previous in self._previous.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                self._atomic_write(path, previous)
        for directory in reversed(self._created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        self._previous.clear()
        self._created_dirs.clear()

    @staticmethod
    def _target_mode(path: Path) -> int:
        """Keep an existing file's permissions; use the umask default for new ones.

        A temporary file is created private, so replacing an existing file with
        it would silently narrow that file's permissions.
        """

        if path.is_file():
            return path.stat().st_mode & 0o777
        umask = os.umask(0o022)
        os.umask(umask)
        return 0o666 & ~umask

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        mode = Transaction._target_mode(path)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(handle.name, mode)
            os.replace(handle.name, path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


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
    claude_home: Path,
    *,
    project_roots: Sequence[Path] = (),
    dry_run: bool = False,
    create_project_memory: bool = False,
    acknowledge_warnings: bool = False,
    applied_at: str | None = None,
    global_heading: str = "Distilled Cross-Agent Guidance",
    index_heading: str = "Distilled Cross-Agent Guidance",
) -> ApplyOutcome:
    """Apply the approved items of an edited checklist to Claude memory."""

    if not claude_home.is_dir():
        raise AdapterError(f"Claude home is not a directory: {claude_home}")

    document = distill_guard.parse_checklist(
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
            "this adapter applies only "
            f"{SOURCE_AGENT} to {TARGET_AGENT} checklists"
        )

    stamp = _timestamp(applied_at)
    outcome = ApplyOutcome(warnings=list(document.result.warnings))
    # Preflight every approved project mapping before the idempotence scan, so
    # that the scan already covers each directory this run may write to.
    confirmed = _confirmed_projects(project_roots)
    touched_roots: list[Path] = sorted(confirmed)
    for candidate in document.approved():
        if candidate.scope != "project":
            continue
        target_root = candidate.project.get("target_root")
        if not isinstance(target_root, str) or not target_root:
            raise AdapterError(f"{candidate.id}: project.target_root is required")
        if Path(target_root).resolve() not in confirmed:
            raise AdapterError(
                f"{candidate.id}: project target_root is not among the "
                "explicitly confirmed project roots"
            )
    applied_fingerprints = collect_applied_fingerprints(claude_home, touched_roots)

    global_blocks: list[str] = []
    project_files: dict[Path, str] = {}
    index_entries: dict[Path, list[str]] = {}

    for candidate in document.approved():
        meta = candidate.metadata
        if meta.get("origin_agent") == TARGET_AGENT:
            raise AdapterError(f"{candidate.id}: refusing a round-trip echo")

        fingerprint = distill_guard.fingerprint_identity(candidate.metadata)
        if fingerprint in applied_fingerprints:
            outcome.skipped.append(
                {
                    "id": candidate.id,
                    "reason": "already-applied",
                    "location": applied_fingerprints[fingerprint],
                }
            )
            continue

        if candidate.scope == "global":
            record = _record_for(candidate, stamp)
            block = f"{_provenance_comment([record])}\n{candidate.proposed_text.strip()}"
            reject_scaffold(candidate.id, block, "global guidance")
            global_blocks.append(block)
            outcome.applied.append(
                {
                    "id": candidate.id,
                    "scope": "global",
                    "target": GLOBAL_INSTRUCTIONS,
                }
            )
            applied_fingerprints[fingerprint] = GLOBAL_INSTRUCTIONS
            continue

        project = candidate.project
        target_root = project.get("target_root")
        if not isinstance(target_root, str) or not target_root:
            raise AdapterError(f"{candidate.id}: project.target_root is required")
        resolved_root = Path(target_root)
        slug = project_slug(resolved_root)
        memory_dir = project_memory_dir(claude_home, slug)
        if not memory_dir.is_dir() and not create_project_memory:
            raise AdapterError(
                f"{candidate.id}: project memory directory does not exist: "
                f"{memory_dir}. Confirm the project mapping, then rerun with "
                "--create-project-memory."
            )
        name = validated_memory_name(
            candidate, slugify(_first_sentence(candidate.proposed_text, limit=80))
        )
        file_path = memory_dir / f"{name}.md"
        if file_path.exists() or file_path in project_files:
            name = f"{name}-{candidate.id.lower()}"
            file_path = memory_dir / f"{name}.md"
        rendered = render_memory_file(candidate, name=name, applied_at=stamp)
        reject_scaffold(candidate.id, rendered, "memory file")
        project_files[file_path] = rendered
        index_path = memory_dir / PROJECT_MEMORY_INDEX
        index_entry = render_index_entry(candidate, file_name=file_path.name)
        reject_scaffold(candidate.id, index_entry, "index entry")
        index_entries.setdefault(index_path, []).append(index_entry)
        outcome.applied.append(
            {
                "id": candidate.id,
                "scope": "project",
                "target": file_path.name,
                "project_slug": slug,
            }
        )
        applied_fingerprints[fingerprint] = file_path.name

    transaction = Transaction()
    if global_blocks:
        global_file = claude_home / GLOBAL_INSTRUCTIONS
        current = (
            global_file.read_text(encoding="utf-8") if global_file.is_file() else ""
        )
        transaction.stage(
            global_file,
            upsert_global_section(current, global_blocks, heading=global_heading),
        )
    for path, text in project_files.items():
        transaction.stage(path, text)
    for index_path, entries in index_entries.items():
        current = (
            index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
        )
        transaction.stage(
            index_path, upsert_index_section(current, entries, heading=index_heading)
        )

    outcome.files = [path.as_posix() for path in transaction.writes]
    if dry_run or not transaction.writes:
        return outcome

    transaction.commit()
    problems = verify_target(
        claude_home, project_roots=touched_roots, expect=outcome.applied
    )
    if problems:
        transaction.rollback()
        raise AdapterError(
            "verification failed after writing; the target was restored:\n  "
            + "\n  ".join(problems)
        )
    return outcome


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_target(
    claude_home: Path,
    *,
    project_roots: Sequence[Path] = (),
    expect: Sequence[dict[str, Any]] | None = None,
) -> list[str]:
    """Audit written target memory for provenance, scope, and leftover markers."""

    problems: list[str] = []
    seen_ids: set[str] = set()
    for path in _applied_files(claude_home, project_roots):
        text = path.read_text(encoding="utf-8", errors="replace")
        if distill_guard.PROVENANCE_RE.search(text) is None:
            continue
        result = distill_guard.validate_provenance_text(
            text, expected_target=TARGET_AGENT
        )
        problems.extend(f"{path.name}: {error}" for error in result.errors)
        for record in _provenance_records(text):
            identifier = record.get("id")
            if isinstance(identifier, str):
                seen_ids.add(identifier)
            scope = record.get("target_scope")
            is_global = path.name == GLOBAL_INSTRUCTIONS
            if is_global and scope != "global":
                problems.append(f"{path.name}: {identifier}: non-global record in the user file")
            if not is_global and scope != "project":
                problems.append(
                    f"{path.name}: {identifier}: non-project record in project memory"
                )
    for item in expect or []:
        if item["id"] not in seen_ids:
            problems.append(f"{item['id']}: approved candidate is missing after apply")
    return problems


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser(
        "inventory", help="describe allowed source and target memory"
    )
    inventory.add_argument("--codex-home", required=True)
    inventory.add_argument("--claude-home", required=True)
    inventory.add_argument("--project-root")
    inventory.add_argument("--outline-limit", type=int, default=8)

    show = subparsers.add_parser(
        "show-source", help="print one allowed source file, refusing the rest"
    )
    show.add_argument("--codex-home", required=True)
    show.add_argument("--path", required=True)

    align = subparsers.add_parser(
        "align-project", help="resolve a project to its Claude memory directory"
    )
    align.add_argument("--claude-home", required=True)
    align.add_argument("--project-root", required=True)
    align.add_argument("--source-root")

    snapshot = subparsers.add_parser(
        "snapshot-source", help="record source digests for an immutability check"
    )
    snapshot.add_argument("--codex-home", required=True)
    snapshot.add_argument("--out", required=True)

    fingerprint = subparsers.add_parser(
        "fingerprint", help="fingerprint normalized meaning rather than raw bytes"
    )
    group = fingerprint.add_mutually_exclusive_group(required=True)
    group.add_argument("--file")
    group.add_argument("--stdin", action="store_true")

    apply_parser = subparsers.add_parser(
        "apply", help="apply an approved checklist to Claude memory"
    )
    apply_parser.add_argument("--checklist", required=True)
    apply_parser.add_argument("--claude-home", required=True)
    apply_parser.add_argument(
        "--project-root",
        action="append",
        default=[],
        help="confirm one aligned project root; repeat for several",
    )
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--create-project-memory", action="store_true")
    apply_parser.add_argument("--acknowledge-warnings", action="store_true")
    apply_parser.add_argument("--timestamp")
    apply_parser.add_argument("--global-heading", default="Distilled Cross-Agent Guidance")
    apply_parser.add_argument("--index-heading", default="Distilled Cross-Agent Guidance")

    verify = subparsers.add_parser(
        "verify", help="audit applied memory and source immutability"
    )
    verify.add_argument("--claude-home", required=True)
    verify.add_argument(
        "--project-root",
        action="append",
        default=[],
        help="may be repeated for several aligned projects",
    )
    verify.add_argument("--codex-home")
    verify.add_argument("--source-manifest")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inventory":
            payload = {
                "source": inventory_source(
                    Path(args.codex_home), outline_limit=args.outline_limit
                ),
                "target": inventory_target(
                    Path(args.claude_home),
                    Path(args.project_root) if args.project_root else None,
                ),
            }
            _print_json(payload)
            return 0

        if args.command == "show-source":
            sys.stdout.write(read_source(Path(args.codex_home), args.path))
            return 0

        if args.command == "align-project":
            _print_json(
                align_project(
                    Path(args.claude_home),
                    Path(args.project_root),
                    source_root=args.source_root,
                )
            )
            return 0

        if args.command == "snapshot-source":
            manifest = source_manifest(Path(args.codex_home))
            try:
                distill_guard.write_private_exclusive(
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
                Path(args.claude_home),
                project_roots=[Path(root) for root in args.project_root],
                dry_run=args.dry_run,
                create_project_memory=args.create_project_memory,
                acknowledge_warnings=args.acknowledge_warnings,
                applied_at=args.timestamp,
                global_heading=args.global_heading,
                index_heading=args.index_heading,
            )
            _print_json(outcome.to_json(dry_run=args.dry_run))
            return 0

        if args.command == "verify":
            problems = verify_target(
                Path(args.claude_home),
                project_roots=[Path(root) for root in args.project_root],
            )
            if bool(args.codex_home) != bool(args.source_manifest):
                raise AdapterError(
                    "--codex-home and --source-manifest must be supplied together"
                )
            if args.source_manifest:
                manifest = json.loads(
                    Path(args.source_manifest).read_text(encoding="utf-8")
                )
                problems.extend(compare_source_manifest(Path(args.codex_home), manifest))
            if problems:
                for problem in problems:
                    print(f"ERROR: {problem}", file=sys.stderr)
                return 1
            print("OK: applied memory, scope labels, and source digests verified")
            return 0
    except (AdapterError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
