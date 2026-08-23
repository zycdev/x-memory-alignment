from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "cross-agent-memory-distiller"
    / "scripts"
    / "distill_guard.py"
)
FIXTURES = ROOT / "tests" / "fixtures"

SPEC = importlib.util.spec_from_file_location("distill_guard", SCRIPT)
assert SPEC and SPEC.loader
distill_guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = distill_guard
SPEC.loader.exec_module(distill_guard)


class ChecklistValidationTests(unittest.TestCase):
    def load(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_accepts_english_visible_text(self) -> None:
        result = distill_guard.validate_checklist_text(
            self.load("checklist-valid-en.md")
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual((result.candidates, result.selected), (1, 1))

    def test_accepts_chinese_visible_text_and_project_mapping(self) -> None:
        result = distill_guard.validate_checklist_text(
            self.load("checklist-valid-zh.md")
        )
        self.assertTrue(result.ok, result.errors)

    def test_rejects_round_trip_echo(self) -> None:
        result = distill_guard.validate_checklist_text(
            self.load("checklist-invalid-echo.md")
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("round-trip echo" in error for error in result.errors))

    def test_rejects_unresolved_annotation_in_selected_text(self) -> None:
        result = distill_guard.validate_checklist_text(
            self.load("checklist-invalid-annotation.md")
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("unresolved annotation" in error for error in result.errors)
        )

    def test_unchecked_candidate_may_retain_user_annotation(self) -> None:
        text = self.load("checklist-invalid-annotation.md").replace(
            "- [x] Include", "- [ ] Include"
        )
        result = distill_guard.validate_checklist_text(text)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.selected, 0)

    def test_rejects_duplicate_candidate_ids(self) -> None:
        text = self.load("checklist-valid-en.md")
        candidate = text[text.index("<!-- camd:candidate") :]
        result = distill_guard.validate_checklist_text(text + "\n" + candidate)
        self.assertFalse(result.ok)
        self.assertTrue(any("duplicate candidate id" in error for error in result.errors))

    def test_accepts_zero_candidate_noop(self) -> None:
        text = (
            '<!-- camd:checklist {"schema":"camd/v1",'
            '"source_agent":"claude-code","target_agent":"codex"} -->\n'
            "# Nothing useful and new\n"
        )
        result = distill_guard.validate_checklist_text(text)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.candidates, 0)


def build_candidate(
    *,
    candidate_id: str = "CAMD-100",
    checkbox: str = "x",
    fingerprint: str = "sha256:" + "1" * 64,
    proposed: str = "Durable guidance.",
    before_proposed: str = "",
    scope_json: str = "",
) -> str:
    """Compose one candidate block for tests of the mechanical guard."""

    scope_fields = (
        '"source_scope":"project","target_scope":"project",' + scope_json
        if scope_json
        else '"source_scope":"global","target_scope":"global"'
    )
    return (
        '<!-- camd:candidate {"schema":"camd/v1","id":"' + candidate_id + '",'
        '"status":"novel","source_agent":"codex","target_agent":"claude-code",'
        '"origin_agent":"codex",' + scope_fields + ','
        '"normalization":"camd-norm/v1",'
        '"fingerprint":"' + fingerprint + '"} -->\n'
        f"- [{checkbox}] Include this candidate\n\n"
        f"{before_proposed}"
        "<!-- camd:proposed:start -->\n"
        f"{proposed}\n"
        "<!-- camd:proposed:end -->\n"
        "<!-- /camd:candidate -->\n"
    )


def build_checklist(*candidates: str) -> str:
    header = (
        '<!-- camd:checklist {"schema":"camd/v1","source_agent":"codex",'
        '"target_agent":"claude-code"} -->\n# Checklist\n\n'
    )
    return header + "\n".join(candidates)


class ChecklistParsingTests(unittest.TestCase):
    """Regressions for checklist parsing: markup inside proposed text,
    checkbox counting, scaffold detection, and duplicate fingerprints."""

    def test_proposed_text_may_contain_task_list_markup(self) -> None:
        text = build_checklist(
            build_candidate(
                proposed=(
                    "When reporting a review, emit one line per finding:\n"
                    "- [ ] finding\n"
                    "- [ ] resolution"
                )
            )
        )
        result = distill_guard.validate_checklist_text(text)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.selected, 1)

    def test_rejects_checkbox_after_proposed_region(self) -> None:
        block = build_candidate()
        misordered = block.replace("- [x] Include this candidate\n\n", "")
        misordered = misordered.replace(
            "<!-- camd:proposed:end -->\n",
            "<!-- camd:proposed:end -->\n- [x] Include this candidate\n",
        )
        result = distill_guard.validate_checklist_text(build_checklist(misordered))
        self.assertFalse(result.ok)
        self.assertTrue(
            any("checkbox must precede" in error for error in result.errors)
        )

    def test_rejects_marker_shaped_scaffold(self) -> None:
        for proposed in (
            "TODO: decide the retention window.",
            "TBD",
            "Keep the PLACEHOLDER section until the owner replies.",
            "Keep this rule. 【Narrow the scope first.】",
        ):
            with self.subTest(proposed=proposed):
                result = distill_guard.validate_checklist_text(
                    build_checklist(build_candidate(proposed=proposed))
                )
                self.assertFalse(result.ok)
                self.assertTrue(
                    any("unresolved annotation" in error for error in result.errors)
                )

    def test_warns_but_accepts_incidental_scaffold_word(self) -> None:
        result = distill_guard.validate_checklist_text(
            build_checklist(
                build_candidate(
                    proposed=(
                        "Never leave a TODO comment in committed code; open an "
                        "issue instead, and replace every placeholder value "
                        "before review."
                    )
                )
            )
        )
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(any("scaffold word" in warning for warning in result.warnings))

    def test_rejects_duplicate_fingerprint_among_selected_candidates(self) -> None:
        text = build_checklist(
            build_candidate(candidate_id="CAMD-100"),
            build_candidate(candidate_id="CAMD-101"),
        )
        result = distill_guard.validate_checklist_text(text)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("duplicate fingerprint" in error for error in result.errors)
        )

    def test_warns_on_duplicate_fingerprint_when_only_one_is_selected(self) -> None:
        text = build_checklist(
            build_candidate(candidate_id="CAMD-100"),
            build_candidate(candidate_id="CAMD-101", checkbox=" "),
        )
        result = distill_guard.validate_checklist_text(text)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(
            any("duplicate fingerprint" in warning for warning in result.warnings)
        )

    def test_rejects_two_selected_duplicates_after_an_unselected_one(self) -> None:
        text = build_checklist(
            build_candidate(candidate_id="CAMD-100", checkbox=" "),
            build_candidate(candidate_id="CAMD-101"),
            build_candidate(candidate_id="CAMD-102"),
        )
        result = distill_guard.validate_checklist_text(text)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("duplicate fingerprint" in error for error in result.errors)
        )

    def test_parse_checklist_exposes_candidates(self) -> None:
        text = build_checklist(
            build_candidate(candidate_id="CAMD-100", proposed="Keep this."),
            build_candidate(
                candidate_id="CAMD-101",
                checkbox=" ",
                fingerprint="sha256:" + "2" * 64,
                proposed="Skip this.",
            ),
        )
        document = distill_guard.parse_checklist(text)
        self.assertTrue(document.ok, document.result.errors)
        self.assertEqual(len(document.candidates), 2)
        approved = document.approved()
        self.assertEqual([candidate.id for candidate in approved], ["CAMD-100"])
        self.assertEqual(approved[0].proposed_text, "Keep this.")
        self.assertEqual(approved[0].scope, "global")
        self.assertEqual(approved[0].status, "novel")
        self.assertEqual(document.header["target_agent"], "claude-code")

    def test_strict_mode_fails_on_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "checklist.md"
            path.write_text(
                build_checklist(
                    build_candidate(proposed="Remove every TODO comment before review.")
                ),
                encoding="utf-8",
            )
            lenient = subprocess.run(
                [sys.executable, str(SCRIPT), "validate-checklist", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            strict = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "validate-checklist",
                    str(path),
                    "--strict",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(lenient.returncode, 0, lenient.stderr)
        self.assertIn("WARNING", lenient.stderr)
        self.assertNotEqual(strict.returncode, 0)


class ProvenanceTests(unittest.TestCase):
    def test_audits_valid_output(self) -> None:
        text = (FIXTURES / "output-valid.md").read_text(encoding="utf-8")
        result = distill_guard.validate_provenance_text(
            text, expected_target="codex"
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.candidates, 1)

    def test_accepts_project_record_without_private_roots(self) -> None:
        text = (
            '<!-- camd:provenance {"schema":"camd/v1","records":[{"id":"CAMD-P1",'
            '"source_agent":"codex","target_agent":"claude-code",'
            '"origin_agent":"codex","source_scope":"project",'
            '"target_scope":"project","fingerprint":"sha256:' + "3" * 64 + '",'
            '"project":{"git_identity":"example.com/owner/repo",'
            '"mapping":"same-root"}}]} -->\n'
        )
        result = distill_guard.validate_provenance_text(
            text, expected_target="claude-code"
        )
        self.assertTrue(result.ok, result.errors)

    def test_rejects_project_record_without_identity(self) -> None:
        text = (
            '<!-- camd:provenance {"schema":"camd/v1","records":[{"id":"CAMD-P2",'
            '"source_agent":"codex","target_agent":"claude-code",'
            '"origin_agent":"codex","source_scope":"project",'
            '"target_scope":"project","fingerprint":"sha256:' + "4" * 64 + '",'
            '"project":{"mapping":"same-root"}}]} -->\n'
        )
        result = distill_guard.validate_provenance_text(text)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("project.git_identity" in error for error in result.errors)
        )

    def test_rejects_wrong_expected_target(self) -> None:
        text = (FIXTURES / "output-valid.md").read_text(encoding="utf-8")
        result = distill_guard.validate_provenance_text(
            text, expected_target="claude-code"
        )
        self.assertFalse(result.ok)


class SharedHelperTests(unittest.TestCase):
    """Helpers both adapters share, so their behaviour is asserted once."""

    def test_private_review_artifact_is_exclusive_and_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.json"
            distill_guard.write_private_exclusive(path, "first\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                distill_guard.write_private_exclusive(path, "replacement\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "first\n")

    def test_mapping_vocabulary_is_constrained(self) -> None:
        key = distill_guard.project_key("/a")
        project = (
            '"project":{"source_root":"/a","target_root":"/a",'
            '"git_identity":"example.com/owner/repo","mapping":"%s",'
            f'"key":"{key}"}}'
        )
        for mapping in distill_guard.MAPPING_VALUES:
            with self.subTest(mapping=mapping):
                text = build_checklist(
                    build_candidate(scope_json=project % mapping)
                )
                self.assertTrue(distill_guard.validate_checklist_text(text).ok)
        result = distill_guard.validate_checklist_text(
            build_checklist(build_candidate(scope_json=project % "same_root"))
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("project.mapping" in error for error in result.errors))

    def test_normalization_ignores_formatting_noise_only(self) -> None:
        self.assertEqual(distill_guard.NORMALIZATION, "camd-norm/v1")
        plain = "Compare maintained alternatives before building a new tool."
        decorated = "- **Compare** maintained   alternatives\n  before building a new tool.\n"
        self.assertEqual(
            distill_guard.fingerprint_text(plain),
            distill_guard.fingerprint_text(decorated),
        )
        self.assertNotEqual(
            distill_guard.fingerprint_text(plain),
            distill_guard.fingerprint_text("Build the tool immediately."),
        )

    def test_project_key_separates_roots_and_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            first = Path(tmpdir) / "one"
            second = Path(tmpdir) / "two"
            first.mkdir()
            second.mkdir()
            self.assertEqual(
                distill_guard.project_key(first), distill_guard.project_key(first)
            )
            self.assertNotEqual(
                distill_guard.project_key(first), distill_guard.project_key(second)
            )
            self.assertNotIn(str(first), distill_guard.project_key(first))

    def test_manifest_comparison_reports_every_difference(self) -> None:
        recorded = distill_guard.build_manifest(
            "example-home", {"kept.md": "sha256:a", "gone.md": "sha256:b"}
        )
        problems = distill_guard.compare_manifest(
            recorded,
            {"kept.md": "sha256:c", "new.md": "sha256:d"},
            root_class="example-home",
        )
        self.assertEqual(
            sorted(problems),
            [
                "source file appeared: new.md",
                "source file changed: kept.md",
                "source file disappeared: gone.md",
            ],
        )

    def test_manifest_comparison_rejects_a_malformed_manifest(self) -> None:
        problems = distill_guard.compare_manifest(
            {"files": []}, {}, root_class="example-home"
        )
        self.assertIn("manifest: schema must be camd/v1", problems)
        self.assertIn("manifest: root_class must be example-home", problems)
        self.assertIn("manifest: files must be a JSON object", problems)

    def test_checklist_requires_current_normalization(self) -> None:
        missing = build_candidate().replace(
            '"normalization":"camd-norm/v1",', ""
        )
        result = distill_guard.validate_checklist_text(build_checklist(missing))
        self.assertFalse(result.ok)
        self.assertTrue(any("normalization" in error for error in result.errors))

    def test_project_key_must_match_target_root(self) -> None:
        project = (
            '"project":{"source_root":"/a","target_root":"/a",'
            '"git_identity":"none","mapping":"same-root",'
            '"key":"0000000000000000"}'
        )
        result = distill_guard.validate_checklist_text(
            build_checklist(build_candidate(scope_json=project))
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("project.key" in error for error in result.errors))

    def test_fingerprint_identity_keeps_versions_separate(self) -> None:
        record = {"fingerprint": "sha256:" + "1" * 64}
        legacy = distill_guard.fingerprint_identity(record)
        current = distill_guard.fingerprint_identity(
            {**record, "normalization": "camd-norm/v1"}
        )
        future = distill_guard.fingerprint_identity(
            {**record, "normalization": "camd-norm/v2"}
        )
        self.assertEqual(legacy, current)
        self.assertNotEqual(current, future)


class CommandLineTests(unittest.TestCase):
    def test_fingerprint_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source.md"
            path.write_bytes(b"durable guidance\n")
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "fingerprint", "--file", str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertRegex(completed.stdout.strip(), r"^sha256:[0-9a-f]{64}$")

    def test_cli_returns_nonzero_for_invalid_checklist(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "validate-checklist",
                str(FIXTURES / "checklist-invalid-echo.md"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("round-trip echo", completed.stderr)


if __name__ == "__main__":
    unittest.main()
