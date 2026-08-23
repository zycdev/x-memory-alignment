#!/usr/bin/env python3
"""Install the canonical skill where Claude Code and Codex discover it.

The installer is mechanical and offline. It resolves each agent's skill
directory, reports exactly what it would do, and changes nothing that it did not
create itself. It never contacts the network, never fetches a Git ref, and never
writes to any memory surface.

Modes:

- ``link-working``  symlink the agents at the skill inside this working tree.
  Immediate feedback while developing, at the cost of running whatever is
  checked out.
- ``link-release``  maintain a detached worktree of a release ref under the
  state directory and symlink the agents at that. Daily use then runs a known
  ref while development continues on another branch.
- ``copy``  copy the skill directory into each destination, independent of this
  repository and frozen until reinstalled.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = "install/v1"
SKILL_NAME = "cross-agent-memory-distiller"
REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SKILL = REPO_ROOT / "skills" / SKILL_NAME
COPY_MARKER = ".camd-install.json"
MODES = ("link-working", "link-release", "copy")
DEFAULT_RELEASE_REF = "main"
HELPER_SCRIPTS = (
    "distill_guard.py",
    "claude_target.py",
    "codex_target.py",
    "improve_log.py",
    "improve_sync.py",
)

# Verification hints are printed rather than run: confirming discovery requires a
# fresh session of the respective agent, which this script cannot start.
VERIFY_HINT = {
    "claude": "start a new Claude Code session; the skill appears in its skill list",
    "codex": "start a new Codex session and list skills",
    "agents": "restart whichever loader reads the Agent Skills directory",
}


class InstallError(RuntimeError):
    """A condition that must stop the run before anything is changed."""


@dataclass(frozen=True)
class Target:
    name: str
    skills_dir: Path
    opportunistic: bool = False

    @property
    def destination(self) -> Path:
        return self.skills_dir / SKILL_NAME


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def state_dir() -> Path:
    override = os.environ.get("CAMD_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or (_home() / ".local" / "state")
    return Path(base) / SKILL_NAME


def receipt_path() -> Path:
    return state_dir() / "install-receipt.json"


def release_worktree() -> Path:
    return state_dir() / "release"


def known_targets() -> dict[str, Target]:
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (_home() / ".claude"))
    codex_home = Path(os.environ.get("CODEX_HOME") or (_home() / ".codex"))
    agents_home = _home() / ".agents"
    return {
        "claude": Target("claude", claude_home / "skills"),
        "codex": Target("codex", codex_home / "skills"),
        # Installed only where a loader already reads it: creating this tree on a
        # host where nothing reads it would be clutter, and skipping it where a
        # loader does read it would silently omit an agent.
        "agents": Target("agents", agents_home / "skills", opportunistic=True),
    }


def resolve_targets(names: Sequence[str] | None) -> list[Target]:
    catalogue = known_targets()
    if names:
        unknown = [name for name in names if name not in catalogue]
        if unknown:
            raise InstallError(
                f"unknown target(s): {', '.join(unknown)}; "
                f"choose from {', '.join(catalogue)}"
            )
        return [catalogue[name] for name in names]
    return [
        target
        for target in catalogue.values()
        if not target.opportunistic or target.skills_dir.is_dir()
    ]


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def load_receipt() -> dict[str, Any]:
    path = receipt_path()
    if not path.is_file():
        return {"schema": SCHEMA, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallError(f"install receipt is not valid JSON: {exc.msg}") from exc
    if not isinstance(data.get("entries"), dict):
        raise InstallError("install receipt is malformed: entries must be an object")
    return data


def save_receipt(receipt: dict[str, Any]) -> None:
    path = receipt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(receipt, indent=2) + "\n"
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(path, 0o600)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


# ---------------------------------------------------------------------------
# Git helpers, all local
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    # The repository root is read at call time so that tests can point the
    # installer at a temporary repository instead of this one.
    return subprocess.run(
        ["git", "-C", str(cwd or REPO_ROOT), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def resolve_local_ref(ref: str) -> str:
    """Resolve a ref that must already exist locally; never fetch."""

    completed = _git("rev-parse", "--verify", f"{ref}^{{commit}}")
    if completed.returncode != 0:
        raise InstallError(
            f"ref {ref!r} does not exist locally. Fetch or create it yourself; "
            "this installer never contacts the network."
        )
    return completed.stdout.strip()


def ensure_release_worktree(ref: str) -> tuple[Path, str]:
    commit = resolve_local_ref(ref)
    worktree = release_worktree()
    if worktree.is_dir() and (worktree / ".git").exists():
        completed = _git("checkout", "--detach", commit, cwd=worktree)
        if completed.returncode != 0:
            raise InstallError(
                f"cannot update the release worktree: {completed.stderr.strip()}"
            )
    else:
        if worktree.exists():
            raise InstallError(
                f"release path exists but is not a worktree: {worktree}"
            )
        worktree.parent.mkdir(parents=True, exist_ok=True)
        completed = _git("worktree", "add", "--detach", str(worktree), commit)
        if completed.returncode != 0:
            raise InstallError(
                f"cannot create the release worktree: {completed.stderr.strip()}"
            )
    skill = worktree / "skills" / SKILL_NAME
    if not (skill / "SKILL.md").is_file():
        raise InstallError(f"ref {ref!r} does not contain {SKILL_NAME}")
    return skill, commit


def release_commit_problem(entry: dict[str, Any]) -> str | None:
    """Return a problem when a release worktree no longer matches its receipt.

    In v1 every release installation shares one worktree, so that both agents
    run the same version by default. A consequence is that reinstalling any one
    release target moves the worktree for all of them: a target can therefore be
    reported as drifted although nothing was done to that target itself. The
    message says so, and points at the only repair that converges: bring every
    release target to one ref in a single operation. Reinstalling just the
    target named in the report would move the drift to the others instead of
    ending it.
    """

    recorded = entry.get("commit")
    if not isinstance(recorded, str) or not recorded:
        return "release receipt has no recorded commit"
    completed = _git(
        "rev-parse", "--verify", "HEAD^{commit}", cwd=release_worktree()
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "Git could not resolve HEAD"
        return f"cannot resolve release worktree HEAD: {detail}"
    actual = completed.stdout.strip()
    if actual != recorded:
        return (
            f"release worktree HEAD {actual[:12]} differs from "
            f"recorded commit {recorded[:12]}; the release worktree is shared, "
            "so reinstalling one target at a different ref moves it for every "
            "release installation. Reinstall every link-release target to the "
            "same ref in one operation: reinstalling only this target would "
            "move the drift to the others. Do not edit the receipt"
        )
    return None


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def check_skill_source(source: Path) -> None:
    skill_file = source / "SKILL.md"
    if not skill_file.is_file():
        raise InstallError(f"no SKILL.md under {source}")
    text = skill_file.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise InstallError("SKILL.md has no front matter")
    if f"name: {SKILL_NAME}" not in text.split("---")[1]:
        raise InstallError("SKILL.md front matter does not declare the skill name")
    for script in HELPER_SCRIPTS:
        path = source / "scripts" / script
        if not path.is_file():
            raise InstallError(f"missing script: {script}")
        check_script_syntax(path)


def check_script_syntax(path: Path) -> None:
    """Parse an installed helper without importing it or writing bytecode."""

    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
    except (SyntaxError, ValueError) as error:
        line = getattr(error, "lineno", None)
        location = f" at line {line}" if line is not None else ""
        raise InstallError(
            f"invalid Python syntax in {path.name}{location}"
        ) from error


def describe(target: Target, receipt: dict[str, Any]) -> dict[str, Any]:
    """Describe a destination without changing anything."""

    destination = target.destination
    entry = receipt["entries"].get(target.name)
    info: dict[str, Any] = {
        "target": target.name,
        "destination": destination.as_posix(),
        "skills_dir_exists": target.skills_dir.is_dir(),
        "state": "absent",
        "ours": False,
        "recorded_mode": entry.get("mode") if entry else None,
        "recorded_origin": entry.get("origin") if entry else None,
    }
    if destination.is_symlink():
        raw = destination.readlink()
        resolved = destination.resolve()
        info["state"] = "symlink" if destination.exists() else "dangling-symlink"
        info["points_to"] = raw.as_posix()
        info["resolves_to"] = resolved.as_posix() if destination.exists() else None
        info["ours"] = bool(entry) and entry.get("destination") == destination.as_posix()
    elif destination.is_dir():
        marker = destination / COPY_MARKER
        info["state"] = "copy" if marker.is_file() else "directory"
        info["ours"] = (
            marker.is_file()
            and bool(entry)
            and entry.get("destination") == destination.as_posix()
        )
    elif destination.exists():
        info["state"] = "file"
    return info


# ---------------------------------------------------------------------------
# Planning and application
# ---------------------------------------------------------------------------


def intended_source(mode: str) -> Path | None:
    """Where a link mode would point, without creating anything."""

    if mode == "link-working":
        return CANONICAL_SKILL
    if mode == "link-release":
        return release_worktree() / "skills" / SKILL_NAME
    return None


def plan_actions(
    targets: Sequence[Target],
    *,
    mode: str,
    ref: str,
    force_replace: bool,
    receipt: dict[str, Any],
) -> list[dict[str, Any]]:
    if mode not in MODES:
        raise InstallError(f"mode must be one of {', '.join(MODES)}")
    expected = intended_source(mode)
    actions: list[dict[str, Any]] = []
    for target in targets:
        current = describe(target, receipt)
        action = {
            "target": target.name,
            "destination": current["destination"],
            "mode": mode,
            "current_state": current["state"],
        }
        already_correct = (
            expected is not None
            and current["state"] == "symlink"
            and current.get("resolves_to") == expected.resolve().as_posix()
        )
        if current["state"] == "absent":
            action["action"] = "create"
        elif current["ours"]:
            action["action"] = "update"
        elif already_correct:
            # A hand-made link that already points where this mode would point is
            # adopted into the receipt rather than replaced. Recording an existing
            # correct state changes nothing on disk.
            action["action"] = "adopt"
            action["reason"] = "existing link already points at the intended source"
        elif force_replace:
            action["action"] = "replace"
            action["reason"] = "an unrecognized destination is moved aside"
        else:
            action["action"] = "conflict"
            action["reason"] = (
                "destination exists and was not created by this installer; "
                "inspect it, then rerun with --force-replace to move it aside"
            )
        actions.append(action)
    return actions


def _move_aside(destination: Path) -> Path:
    backup = destination.with_name(
        f"{destination.name}.replaced-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    )
    destination.rename(backup)
    return backup


def _install_symlink(
    destination: Path, source: Path, *, allow_managed_copy: bool = False
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.exists():
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif allow_managed_copy and (destination / COPY_MARKER).is_file():
            shutil.rmtree(destination)
        else:
            raise InstallError(
                "refusing to remove a directory while installing a symlink; "
                "the caller did not identify it as an installer-managed copy"
            )
    destination.symlink_to(source, target_is_directory=True)


def _install_copy(destination: Path, source: Path, marker: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        destination.unlink()
    elif destination.is_dir():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (destination / COPY_MARKER).write_text(
        json.dumps(marker, indent=2) + "\n", encoding="utf-8"
    )


def install(
    targets: Sequence[Target],
    *,
    mode: str,
    ref: str = DEFAULT_RELEASE_REF,
    force_replace: bool = False,
) -> dict[str, Any]:
    receipt = load_receipt()
    actions = plan_actions(
        targets, mode=mode, ref=ref, force_replace=force_replace, receipt=receipt
    )
    conflicts = [item for item in actions if item["action"] == "conflict"]
    if conflicts:
        names = ", ".join(item["target"] for item in conflicts)
        raise InstallError(
            f"refusing to touch existing destination(s) for: {names}. "
            + conflicts[0]["reason"]
        )

    if mode == "link-release":
        source, commit = ensure_release_worktree(ref)
        source_ref: str | None = ref
    else:
        # link-working and copy both take the working tree as it stands. No ref
        # is recorded, because none was used; the commit records what was there.
        source, commit = CANONICAL_SKILL, resolve_local_ref("HEAD")
        source_ref = None
    check_skill_source(source)

    results = []
    for action in actions:
        target = next(item for item in targets if item.name == action["target"])
        destination = target.destination
        previous = receipt["entries"].get(target.name, {})
        if action["action"] == "replace":
            action["moved_to"] = _move_aside(destination).as_posix()
        if action["action"] == "adopt":
            origin = "adopted"
        elif previous:
            origin = previous.get(
                "origin", "adopted" if previous.get("adopted") else "created"
            )
        else:
            origin = "created"
        entry = {
            "target": target.name,
            "destination": destination.as_posix(),
            "mode": mode,
            "source": source.as_posix(),
            "ref": source_ref,
            "commit": commit,
            "installed_at": _now(),
            "origin": origin,
        }
        if action["action"] == "adopt":
            entry["adopted"] = True
        elif mode == "copy":
            _install_copy(destination, source, entry)
        else:
            _install_symlink(
                destination,
                source,
                allow_managed_copy=(
                    action["action"] == "update"
                    and action["current_state"] == "copy"
                ),
            )
        receipt["entries"][target.name] = entry
        results.append(action)

    receipt["schema"] = SCHEMA
    save_receipt(receipt)
    return {"mode": mode, "source": source.as_posix(), "actions": results}


def uninstall(targets: Sequence[Target]) -> list[dict[str, Any]]:
    receipt = load_receipt()
    results = []
    changed = False
    for target in targets:
        current = describe(target, receipt)
        result = {
            "target": target.name,
            "destination": current["destination"],
            "recorded_origin": current["recorded_origin"],
        }
        if current["state"] == "absent":
            result["action"] = "skip"
            result["reason"] = "nothing installed"
        elif not current["ours"]:
            result["action"] = "skip"
            result["reason"] = "not created by this installer; left untouched"
        else:
            destination = target.destination
            if destination.is_symlink():
                destination.unlink()
            else:
                shutil.rmtree(destination)
            receipt["entries"].pop(target.name, None)
            changed = True
            result["action"] = "removed"
        results.append(result)
    if changed:
        save_receipt(receipt)
    return results


def verify(targets: Sequence[Target]) -> list[dict[str, Any]]:
    receipt = load_receipt()
    results = []
    for target in targets:
        current = describe(target, receipt)
        problems: list[str] = []
        destination = target.destination
        if current["state"] == "absent":
            problems.append("not installed")
        elif current["state"] == "dangling-symlink":
            problems.append("symlink does not resolve")
        else:
            try:
                check_skill_source(destination)
            except InstallError as error:
                problems.append(str(error))
        entry = receipt["entries"].get(target.name)
        if entry and entry.get("mode") == "link-release":
            problem = release_commit_problem(entry)
            if problem:
                problems.append(problem)
        results.append(
            {
                **current,
                "problems": problems,
                "ok": not problems,
                "verify_hint": VERIFY_HINT.get(target.name, ""),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("status", "report what is installed for each agent"),
        ("verify", "re-read each destination and check it is usable"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--targets", nargs="+")

    for name, help_text in (
        ("plan", "print what install would do, changing nothing"),
        ("install", "create or update the installation"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--targets", nargs="+")
        sub.add_argument("--mode", choices=MODES, default="link-working")
        sub.add_argument("--ref", default=DEFAULT_RELEASE_REF)
        sub.add_argument("--force-replace", action="store_true")
        if name == "install":
            sub.add_argument("--yes", action="store_true")

    remove = subparsers.add_parser(
        "uninstall", help="remove only what this installer created"
    )
    remove.add_argument("--targets", nargs="+")
    remove.add_argument("--yes", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        targets = resolve_targets(getattr(args, "targets", None))
        if args.command == "status":
            receipt = load_receipt()
            _print_json(
                {
                    "receipt": receipt_path().as_posix(),
                    "targets": [describe(target, receipt) for target in targets],
                }
            )
            return 0
        if args.command == "verify":
            results = verify(targets)
            _print_json(results)
            return 0 if all(item["ok"] for item in results) else 1
        if args.command == "plan":
            _print_json(
                plan_actions(
                    targets,
                    mode=args.mode,
                    ref=args.ref,
                    force_replace=args.force_replace,
                    receipt=load_receipt(),
                )
            )
            return 0
        if args.command == "install":
            if not args.yes:
                _print_json(
                    {
                        "dry_run": True,
                        "note": "rerun with --yes to apply",
                        "actions": plan_actions(
                            targets,
                            mode=args.mode,
                            ref=args.ref,
                            force_replace=args.force_replace,
                            receipt=load_receipt(),
                        ),
                    }
                )
                return 0
            _print_json(
                install(
                    targets,
                    mode=args.mode,
                    ref=args.ref,
                    force_replace=args.force_replace,
                )
            )
            return 0
        if args.command == "uninstall":
            if not args.yes:
                _print_json(
                    {
                        "dry_run": True,
                        "note": "rerun with --yes to apply",
                        "targets": [
                            describe(target, load_receipt()) for target in targets
                        ],
                    }
                )
                return 0
            _print_json(uninstall(targets))
            return 0
    except (InstallError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
