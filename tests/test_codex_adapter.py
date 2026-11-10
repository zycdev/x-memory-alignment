from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "x-memory-alignment" / "scripts"
ADAPTER_SCRIPT = SCRIPTS / "codex_target.py"

SPEC = importlib.util.spec_from_file_location("codex_target", ADAPTER_SCRIPT)
assert SPEC and SPEC.loader
codex_target = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codex_target
SPEC.loader.exec_module(codex_target)

import alignment_guard  # noqa: E402
import claude_target  # noqa: E402


FINGERPRINTS = {
    "global": "sha256:" + "a" * 64,
    "project": "sha256:" + "b" * 64,
    "extra": "sha256:" + "c" * 64,
    "echo": "sha256:" + "d" * 64,
}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def candidate_block(
    *,
    candidate_id: str,
    scope: str,
    fingerprint: str,
    proposed: str,
    checkbox: str = "x",
    origin_agent: str = "claude-code",
    project: dict[str, str] | None = None,
) -> str:
    metadata: dict[str, object] = {
        "schema": "camd/v1",
        "id": candidate_id,
        "status": "novel",
        "source_agent": "claude-code",
        "target_agent": "codex",
        "origin_agent": origin_agent,
        "source_scope": scope,
        "target_scope": scope,
        "normalization": alignment_guard.NORMALIZATION,
        "fingerprint": fingerprint,
    }
    if project:
        project = dict(project)
        project.setdefault("key", alignment_guard.project_key(project["target_root"]))
        metadata["project"] = project
    return (
        "<!-- camd:candidate "
        + json.dumps(metadata)
        + " -->\n"
        + f"- [{checkbox}] Include\n\n"
        + "<!-- camd:proposed:start -->\n"
        + proposed
        + "\n<!-- camd:proposed:end -->\n"
        + "<!-- /camd:candidate -->\n"
    )


def checklist_document(*blocks: str) -> str:
    return (
        '<!-- camd:checklist {"schema":"camd/v1",'
        '"source_agent":"claude-code","target_agent":"codex"} -->\n'
        "# Approval checklist\n\n"
        + "\n".join(blocks)
    )


class AdapterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tempdir.name)
        self.claude_home = self.tmp / "claude-home"
        self.codex_home = self.tmp / "codex-home"
        self.project_root = self.tmp / "workspace" / "example-project"
        self.project_root.mkdir(parents=True)
        self.build_claude_home()
        self.build_codex_home()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def build_claude_home(self) -> None:
        write(
            self.claude_home / "CLAUDE.md",
            "# User guidance\n\n- Use the user's language.\n",
        )
        memory_dir = (
            self.claude_home
            / "projects"
            / codex_target.project_slug(self.project_root)
            / "memory"
        )
        write(
            memory_dir / "MEMORY.md",
            "# Memory index\n\n- [Review rule](feedback_review.md)\n",
        )
        write(
            memory_dir / "feedback_review.md",
            "# Review rule\n\n- Review existing tools before building one.\n",
        )
        write(self.claude_home / "history.jsonl", "private transcript\n")
        write(self.claude_home / "settings.json", '{"secret":"value"}\n')

    def build_codex_home(self) -> None:
        write(
            self.codex_home / "memories" / "MEMORY.md",
            "# Existing Codex memory\n\n## Durable guidance\n",
        )
        write(
            codex_target.notes_dir(self.codex_home) / "legacy-note.md",
            "# Legacy note\n\n- Guidance without provenance.\n",
        )
        write(self.project_root / "AGENTS.md", "# Project instructions\n")

    def project_metadata(self) -> dict[str, str]:
        return {
            "source_root": self.project_root.resolve().as_posix(),
            "target_root": self.project_root.resolve().as_posix(),
            "git_identity": "example.com/owner/example-project",
            "mapping": "same-root",
        }

    def global_candidate(self, **overrides: object) -> str:
        values = {
            "candidate_id": "CAMD-C001",
            "scope": "global",
            "fingerprint": FINGERPRINTS["global"],
            "proposed": "Follow the user's language in the current interaction.",
        }
        values.update(overrides)
        return candidate_block(**values)  # type: ignore[arg-type]

    def project_candidate(self, **overrides: object) -> str:
        values = {
            "candidate_id": "CAMD-C002",
            "scope": "project",
            "fingerprint": FINGERPRINTS["project"],
            "proposed": "Record reproducible commands beside project findings.",
            "project": self.project_metadata(),
        }
        values.update(overrides)
        return candidate_block(**values)  # type: ignore[arg-type]

    def write_checklist(self, text: str, name: str = "checklist.md") -> Path:
        return write(self.tmp / name, text)

    def tree_digest(self, root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }


class SourceDiscoveryTests(AdapterTestCase):
    def test_inventory_is_index_first_and_explicit_project_only(self) -> None:
        inventory = codex_target.inventory_source(
            self.claude_home, [self.project_root]
        )
        roles = {item["path"]: item["role"] for item in inventory["files"]}
        self.assertEqual(roles["CLAUDE.md"], "candidate")
        project_prefix = f"projects/{codex_target.project_slug(self.project_root)}/memory"
        self.assertEqual(roles[f"{project_prefix}/MEMORY.md"], "registry")
        self.assertEqual(roles[f"{project_prefix}/feedback_review.md"], "candidate")
        self.assertNotIn("history.jsonl", roles)
        self.assertNotIn("settings.json", roles)

    def test_show_source_refuses_forbidden_and_traversal_paths(self) -> None:
        for path in ("history.jsonl", "../outside.md"):
            with self.subTest(path=path):
                with self.assertRaises(codex_target.AdapterError):
                    codex_target.read_source(
                        self.claude_home, path, [self.project_root]
                    )

    def test_source_manifest_detects_drift(self) -> None:
        manifest = codex_target.source_manifest(
            self.claude_home, [self.project_root]
        )
        write(self.claude_home / "CLAUDE.md", "# Changed\n")
        problems = codex_target.compare_source_manifest(
            self.claude_home, manifest, [self.project_root]
        )
        self.assertTrue(any("changed" in item for item in problems))


class AlignmentAndTargetInventoryTests(AdapterTestCase):
    def test_same_root_alignment_finds_claude_memory(self) -> None:
        alignment = codex_target.align_project(
            self.claude_home, self.project_root
        )
        self.assertEqual(alignment["mapping"], "same-root")
        self.assertTrue(alignment["memory_dir_exists"])

    def test_target_inventory_marks_unprovenanced_legacy_notes(self) -> None:
        inventory = codex_target.inventory_target(
            self.codex_home, [self.project_root]
        )
        self.assertEqual(inventory["unprovenanced_notes"], 1)
        self.assertTrue(inventory["historical_origin_review_required"])
        self.assertNotIn("Guidance without provenance", json.dumps(inventory))
        self.assertEqual(
            inventory["project_contexts"][0]["instruction_files"], ["AGENTS.md"]
        )


class ApplyTests(AdapterTestCase):
    def test_global_and_project_candidates_share_one_note(self) -> None:
        checklist = self.write_checklist(
            checklist_document(self.global_candidate(), self.project_candidate())
        )
        outcome = codex_target.apply_checklist(
            checklist,
            self.codex_home,
            project_roots=[self.project_root],
            applied_at="2026-08-21T12:00:00Z",
        )
        self.assertEqual(len(outcome.applied), 2)
        self.assertEqual(len(outcome.files), 1)
        note = Path(outcome.files[0])
        text = note.read_text(encoding="utf-8")
        self.assertIn("## Global guidance", text)
        self.assertIn("## Project: example.com/owner/example-project", text)
        self.assertNotIn(self.project_root.as_posix(), text)
        self.assertTrue(
            alignment_guard.validate_provenance_text(text, expected_target="codex").ok
        )

    def test_unchecked_candidate_is_never_applied(self) -> None:
        checklist = self.write_checklist(
            checklist_document(
                self.global_candidate(checkbox=" "), self.project_candidate()
            )
        )
        outcome = codex_target.apply_checklist(
            checklist, self.codex_home, project_roots=[self.project_root]
        )
        self.assertEqual([item["id"] for item in outcome.applied], ["CAMD-C002"])

    def test_zero_candidate_noop_needs_no_notes_directory(self) -> None:
        empty_codex = self.tmp / "zero-candidate-codex"
        empty_codex.mkdir()
        checklist = self.write_checklist(checklist_document())
        outcome = codex_target.apply_checklist(checklist, empty_codex)
        self.assertEqual(outcome.applied, [])
        self.assertEqual(outcome.files, [])
        self.assertEqual(list(empty_codex.iterdir()), [])

    def test_apply_is_idempotent_by_provenance_fingerprint(self) -> None:
        checklist = self.write_checklist(checklist_document(self.global_candidate()))
        first = codex_target.apply_checklist(checklist, self.codex_home)
        second = codex_target.apply_checklist(checklist, self.codex_home)
        self.assertEqual(len(first.applied), 1)
        self.assertEqual(second.applied, [])
        self.assertEqual(second.files, [])
        self.assertEqual(second.skipped[0]["reason"], "already-applied")

    def test_future_notes_always_embed_provenance(self) -> None:
        checklist = self.write_checklist(checklist_document(self.global_candidate()))
        outcome = codex_target.apply_checklist(checklist, self.codex_home)
        text = Path(outcome.files[0]).read_text(encoding="utf-8")
        self.assertIn("camd:provenance", text)
        records = codex_target._provenance_records(text)
        self.assertEqual(records[0]["origin_agent"], "claude-code")
        self.assertEqual(records[0]["normalization"], alignment_guard.NORMALIZATION)

    def test_new_note_is_private_and_exclusive_create_never_overwrites(self) -> None:
        checklist = self.write_checklist(checklist_document(self.global_candidate()))
        outcome = codex_target.apply_checklist(checklist, self.codex_home)
        note = Path(outcome.files[0])
        self.assertEqual(note.stat().st_mode & 0o777, 0o600)
        original = note.read_text(encoding="utf-8")
        with self.assertRaises(FileExistsError):
            codex_target._exclusive_create(note, "replacement")
        self.assertEqual(note.read_text(encoding="utf-8"), original)

    def test_concurrent_same_name_note_is_not_removed_on_exclusive_failure(self) -> None:
        checklist = self.write_checklist(checklist_document(self.global_candidate()))
        protected = write(codex_target.notes_dir(self.codex_home) / "race.md", "peer")
        with mock.patch.object(codex_target, "_choose_note_path", return_value=protected):
            with self.assertRaises(FileExistsError):
                codex_target.apply_checklist(checklist, self.codex_home)
        self.assertEqual(protected.read_text(encoding="utf-8"), "peer")

    def test_projects_without_remotes_have_distinct_visible_headings(self) -> None:
        other_root = self.tmp / "workspace" / "second-project"
        other_root.mkdir(parents=True)
        first = dict(self.project_metadata(), git_identity="none")
        second = {
            "source_root": other_root.as_posix(),
            "target_root": other_root.as_posix(),
            "git_identity": "none",
            "mapping": "same-root",
        }
        checklist = self.write_checklist(
            checklist_document(
                self.project_candidate(project=first),
                self.project_candidate(
                    candidate_id="CAMD-C003",
                    fingerprint=FINGERPRINTS["extra"],
                    proposed="Keep this guidance with the second project.",
                    project=second,
                ),
            )
        )
        outcome = codex_target.apply_checklist(
            checklist,
            self.codex_home,
            project_roots=[self.project_root, other_root],
        )
        text = Path(outcome.files[0]).read_text(encoding="utf-8")
        self.assertIn(
            f"## Project: none [key: {alignment_guard.project_key(self.project_root)}]",
            text,
        )
        self.assertIn(
            f"## Project: none [key: {alignment_guard.project_key(other_root)}]",
            text,
        )

    def test_round_trip_echo_is_refused(self) -> None:
        checklist = self.write_checklist(
            checklist_document(self.global_candidate(origin_agent="codex"))
        )
        before = self.tree_digest(self.codex_home)
        with self.assertRaises(codex_target.AdapterError):
            codex_target.apply_checklist(checklist, self.codex_home)
        self.assertEqual(before, self.tree_digest(self.codex_home))

    def test_opposite_direction_is_refused(self) -> None:
        text = checklist_document()
        text = text.replace(
            '"source_agent":"claude-code","target_agent":"codex"',
            '"source_agent":"codex","target_agent":"claude-code"',
            1,
        )
        checklist = self.write_checklist(text)
        with self.assertRaises(codex_target.AdapterError) as error:
            codex_target.apply_checklist(checklist, self.codex_home)
        self.assertIn("applies only", str(error.exception))

    def test_project_candidate_requires_explicit_confirmed_root(self) -> None:
        checklist = self.write_checklist(checklist_document(self.project_candidate()))
        with self.assertRaises(codex_target.AdapterError) as error:
            codex_target.apply_checklist(checklist, self.codex_home)
        self.assertIn("confirmed project roots", str(error.exception))

    def test_warning_requires_explicit_acknowledgement(self) -> None:
        checklist = self.write_checklist(
            checklist_document(
                self.global_candidate(
                    proposed="Never leave a TODO comment in committed code."
                )
            )
        )
        with self.assertRaises(codex_target.AdapterError):
            codex_target.apply_checklist(checklist, self.codex_home)
        outcome = codex_target.apply_checklist(
            checklist, self.codex_home, acknowledge_warnings=True
        )
        self.assertEqual(len(outcome.applied), 1)

    def test_dry_run_changes_nothing(self) -> None:
        checklist = self.write_checklist(checklist_document(self.global_candidate()))
        before = self.tree_digest(self.codex_home)
        outcome = codex_target.apply_checklist(
            checklist, self.codex_home, dry_run=True
        )
        self.assertEqual(len(outcome.applied), 1)
        self.assertEqual(before, self.tree_digest(self.codex_home))

    def test_generated_registry_and_source_are_unchanged(self) -> None:
        source_before = self.tree_digest(self.claude_home)
        registry = self.codex_home / "memories" / "MEMORY.md"
        registry_before = registry.read_bytes()
        checklist = self.write_checklist(checklist_document(self.global_candidate()))
        codex_target.apply_checklist(checklist, self.codex_home)
        self.assertEqual(source_before, self.tree_digest(self.claude_home))
        self.assertEqual(registry_before, registry.read_bytes())

    def test_verification_failure_removes_new_note(self) -> None:
        checklist = self.write_checklist(checklist_document(self.global_candidate()))
        original = set(codex_target._note_files(self.codex_home))
        with mock.patch.object(
            codex_target, "verify_note", return_value=["synthetic failure"]
        ):
            with self.assertRaises(codex_target.AdapterError):
                codex_target.apply_checklist(checklist, self.codex_home)
        self.assertEqual(set(codex_target._note_files(self.codex_home)), original)

    def test_missing_notes_directory_requires_explicit_creation(self) -> None:
        empty_codex = self.tmp / "empty-codex"
        empty_codex.mkdir()
        checklist = self.write_checklist(checklist_document(self.global_candidate()))
        with self.assertRaises(codex_target.AdapterError):
            codex_target.apply_checklist(checklist, empty_codex)
        outcome = codex_target.apply_checklist(
            checklist, empty_codex, create_notes_dir=True
        )
        self.assertEqual(len(outcome.applied), 1)

    def test_created_notes_tree_rolls_back_on_verification_failure(self) -> None:
        empty_codex = self.tmp / "rollback-codex"
        empty_codex.mkdir()
        checklist = self.write_checklist(checklist_document(self.global_candidate()))
        with mock.patch.object(
            codex_target, "verify_note", return_value=["synthetic failure"]
        ):
            with self.assertRaises(codex_target.AdapterError):
                codex_target.apply_checklist(
                    checklist, empty_codex, create_notes_dir=True
                )
        self.assertFalse((empty_codex / "memories").exists())


class FingerprintTests(unittest.TestCase):
    def test_normalization_matches_claude_adapter_candidate(self) -> None:
        plain = "Compare maintained alternatives before building a new tool."
        decorated = "- **Compare** maintained   alternatives\n  before building a new tool.\n"
        self.assertEqual(
            codex_target.fingerprint_text(plain),
            codex_target.fingerprint_text(decorated),
        )
        self.assertEqual(
            codex_target.fingerprint_text(decorated),
            claude_target.fingerprint_text(decorated),
        )


class CommandLineTests(AdapterTestCase):
    def run_adapter(
        self, *args: str, expect_success: bool = True
    ) -> subprocess.CompletedProcess:
        isolated_home = self.tmp / "isolated-home"
        isolated_home.mkdir(exist_ok=True)
        completed = subprocess.run(
            [sys.executable, str(ADAPTER_SCRIPT), *args],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(isolated_home)},
        )
        if expect_success:
            self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def test_end_to_end_inventory_apply_verify(self) -> None:
        inventory = json.loads(
            self.run_adapter(
                "inventory",
                "--claude-home", str(self.claude_home),
                "--codex-home", str(self.codex_home),
                "--project-root", str(self.project_root),
            ).stdout
        )
        self.assertEqual(inventory["source"]["counts"]["candidate"], 2)
        self.assertTrue(inventory["target"]["historical_origin_review_required"])

        manifest = self.tmp / "source-manifest.json"
        self.run_adapter(
            "snapshot-source",
            "--claude-home", str(self.claude_home),
            "--project-root", str(self.project_root),
            "--out", str(manifest),
        )
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        completed = self.run_adapter(
            "snapshot-source",
            "--claude-home", str(self.claude_home),
            "--project-root", str(self.project_root),
            "--out", str(manifest),
            expect_success=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("snapshot destination already exists", completed.stderr)
        self.assertIn("new --out path", completed.stderr)
        checklist = self.write_checklist(
            checklist_document(self.global_candidate(), self.project_candidate())
        )
        outcome = json.loads(
            self.run_adapter(
                "apply",
                "--checklist", str(checklist),
                "--codex-home", str(self.codex_home),
                "--project-root", str(self.project_root),
                "--timestamp", "2026-08-21T12:00:00Z",
            ).stdout
        )
        self.assertEqual(len(outcome["applied"]), 2)
        self.run_adapter(
            "verify",
            "--codex-home", str(self.codex_home),
            "--note", outcome["files"][0],
            "--claude-home", str(self.claude_home),
            "--project-root", str(self.project_root),
            "--source-manifest", str(manifest),
        )

    def test_show_source_refuses_history(self) -> None:
        completed = self.run_adapter(
            "show-source",
            "--claude-home", str(self.claude_home),
            "--path", "history.jsonl",
            expect_success=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("private transcript", completed.stdout)


if __name__ == "__main__":
    unittest.main()
