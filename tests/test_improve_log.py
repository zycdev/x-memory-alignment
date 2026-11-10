"""Tests for the offline improvement queue.

Every test redirects the state directory into a temporary tree. No test may read
or write the user's real queue, and several assert that the tool cannot reach the
network or a version control binary at all.
"""

from __future__ import annotations

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
SCRIPT = (
    ROOT / "skills" / "x-memory-alignment" / "scripts" / "improve_log.py"
)

SPEC = importlib.util.spec_from_file_location("improve_log", SCRIPT)
assert SPEC and SPEC.loader
improve_log = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = improve_log
SPEC.loader.exec_module(improve_log)


class QueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="camd-loop-"))
        self.state = self.tmp / "state"
        self._previous = os.environ.get("CAMD_STATE_DIR")
        os.environ["CAMD_STATE_DIR"] = str(self.state)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._previous is None:
            os.environ.pop("CAMD_STATE_DIR", None)
        else:
            os.environ["CAMD_STATE_DIR"] = self._previous
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, **overrides: object) -> dict:
        payload = {
            "kind": "bug",
            "severity": "major",
            "component": "guard",
            "title": "A validated checklist was refused",
            "body": "Run validate-checklist on a valid file and it exits non-zero.",
            "initiated_by": "user-request",
            "agent": "claude-code",
        }
        payload.update(overrides)
        return improve_log.add_report(**payload)  # type: ignore[arg-type]


class CaptureTests(QueueTestCase):
    def test_default_storage_preserves_existing_reports(self) -> None:
        for use_xdg in (False, True):
            with self.subTest(use_xdg=use_xdg), mock.patch.dict(os.environ):
                home = self.tmp / ("xdg-home" if use_xdg else "default-home")
                os.environ["HOME"] = str(home)
                if use_xdg:
                    base = self.tmp / "xdg-state"
                    os.environ["XDG_STATE_HOME"] = str(base)
                else:
                    base = home / ".local" / "state"
                    os.environ.pop("XDG_STATE_HOME", None)
                default_state = base / "x-memory-alignment"
                os.environ["CAMD_STATE_DIR"] = str(default_state)
                report = self.add()
                os.environ.pop("CAMD_STATE_DIR")
                reports = improve_log.load_reports()
                self.assertEqual(len(reports), 1)
                self.assertEqual(reports[0].id, report["id"])

    def test_report_is_created_privately(self) -> None:
        result = self.add()
        path = Path(result["path"])
        self.assertEqual(result["action"], "created")
        self.assertTrue(improve_log.ID_RE.fullmatch(result["id"]))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_reports_live_outside_the_repository_and_memory(self) -> None:
        path = Path(self.add()["path"]).resolve()
        self.assertFalse(path.is_relative_to(ROOT))
        self.assertIn("state", path.parts)

    def test_capture_requires_a_declared_initiator(self) -> None:
        with self.assertRaises(improve_log.ReportError) as error:
            self.add(initiated_by="agent-decided")
        self.assertIn("how this report was requested", str(error.exception))

    def test_identical_meaning_becomes_an_occurrence(self) -> None:
        first = self.add()
        Path(first["path"]).chmod(0o644)
        second = self.add(
            body="Run   validate-checklist on a **valid** file and it exits non-zero.",
            agent="codex",
        )
        self.assertEqual(second["action"], "occurrence-added")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["occurrences"], 2)
        self.assertEqual(len(improve_log.load_reports()), 1)
        body = Path(first["path"]).read_text(encoding="utf-8")
        self.assertIn("## Occurrences", body)
        self.assertIn("codex", body)
        self.assertEqual(stat.S_IMODE(Path(first["path"]).stat().st_mode), 0o600)

    def test_different_meaning_becomes_a_new_report(self) -> None:
        self.add()
        other = self.add(title="Installer overwrote an unrelated skill")
        self.assertEqual(other["action"], "created")
        self.assertEqual(len(improve_log.load_reports()), 2)

    def test_resolved_report_does_not_absorb_a_new_occurrence(self) -> None:
        first = self.add()
        improve_log.set_status(first["id"], "resolved", note="fixed in a test")
        again = self.add()
        self.assertEqual(again["action"], "created")
        self.assertNotEqual(again["id"], first["id"])

    def test_surprise_is_a_supported_kind(self) -> None:
        result = self.add(kind="surprise", title="Refusal was right but unexpected")
        report = improve_log.find_report(result["id"])
        self.assertEqual(report.metadata["kind"], "surprise")

    def test_metadata_validates(self) -> None:
        result = self.add()
        report = improve_log.find_report(result["id"])
        self.assertEqual(improve_log.validate_metadata(report.metadata, report.id), [])
        self.assertEqual(
            report.metadata["normalization"], improve_log.alignment_guard.NORMALIZATION
        )

    def test_corrupt_neighbour_does_not_disable_capture_or_status(self) -> None:
        improve_log.reports_dir().mkdir(parents=True)
        corrupt = improve_log.reports_dir() / "CAMD-R-20260822-BROKEN.md"
        corrupt.write_text("not a report\n", encoding="utf-8")

        created = self.add(title="A healthy report beside a corrupt file")
        self.assertEqual(created["action"], "created")
        self.assertTrue(created["warnings"])
        overview = improve_log.status_overview()
        self.assertEqual(overview["total"], 1)
        self.assertTrue(overview["warnings"])
        packet = improve_log.render_packet(improve_log.load_reports())
        self.assertIn(created["id"], packet)

        reports, errors = improve_log.scan_reports()
        for report in reports:
            errors.extend(improve_log.validate_metadata(report.metadata, report.id))
        self.assertTrue(errors, "validate must still report the corrupt file")


    def test_a_failed_write_leaves_no_partial_report(self) -> None:
        # The queue writer delegates to the shared private-write helper, so a
        # failure removes the partial file. A partial report would otherwise
        # survive as an unreadable entry that only `validate` could explain.
        path = improve_log.reports_dir()
        self.add()
        target = path / "CAMD-R-20260823-PARTAA.md"
        with self.assertRaises(TypeError):
            improve_log._write_new_private_file(target, None)  # type: ignore[arg-type]
        self.assertFalse(target.exists())

    def test_the_writer_refuses_to_follow_a_symlink(self) -> None:
        self.add()
        victim = self.tmp / "victim.md"
        victim.write_text("keep\n", encoding="utf-8")
        link = improve_log.reports_dir() / "CAMD-R-20260823-LINKAA.md"
        link.symlink_to(victim)
        with self.assertRaises(OSError):
            improve_log._write_new_private_file(link, "attacker\n")
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep\n")


class RedactionTests(QueueTestCase):
    def test_identifiable_material_is_flagged(self) -> None:
        home = os.environ.get("HOME", "/home/example")
        body = (
            f"The path {home}/private/notes.md was shown.\n"
            "Contact someone@example.com or host build.example.com.\n"
            "Digest sha256:" + "a" * 64 + "\n"
            '<!-- camd:provenance {"schema":"camd/v1"} -->\n'
        )
        findings = improve_log.scan_redaction(body)
        rules = {item["rule"] for item in findings}
        self.assertIn("absolute-home-path", rules)
        self.assertIn("email-address", rules)
        self.assertIn("host-name", rules)
        self.assertIn("long-digest", rules)
        self.assertIn("alignment-metadata", rules)

    def test_findings_never_quote_the_material(self) -> None:
        body = "See /home/someone/secret-directory/file.md for details."
        serialized = json.dumps(improve_log.scan_redaction(body))
        self.assertNotIn("secret-directory", serialized)
        self.assertIn("absolute-home-path", serialized)

    def test_ordinary_prose_is_not_flagged(self) -> None:
        body = (
            "The refusal message should name the command to rerun, because the "
            "user has already confirmed the mapping in the checklist."
        )
        self.assertEqual(improve_log.scan_redaction(body), [])

    def test_private_evidence_is_flagged_and_excluded_from_the_public_view(self) -> None:
        result = self.add(evidence="local-only detail")
        report = improve_log.find_report(result["id"])
        rules = {item["rule"] for item in improve_log.scan_redaction(report.body)}
        self.assertIn("private-evidence-block", rules)
        self.assertIn("local-only detail", report.body)
        self.assertNotIn("local-only detail", report.public_view())


class PacketTests(QueueTestCase):
    def test_packet_is_read_only_and_self_validating(self) -> None:
        self.add()
        self.add(title="Installer overwrote an unrelated skill", severity="blocking")
        packet = improve_log.render_packet(improve_log.load_reports())
        self.assertIn("camd:triage", packet)
        self.assertNotIn("- [ ]", packet)
        self.assertNotIn("- [x]", packet)
        self.assertNotIn("camd:candidate", packet)
        self.assertEqual(improve_log.validate_packet(packet), [])

    def test_packet_excludes_private_evidence(self) -> None:
        self.add(evidence="local-only detail")
        packet = improve_log.render_packet(improve_log.load_reports())
        self.assertNotIn("local-only detail", packet)
        self.assertEqual(improve_log.validate_packet(packet), [])

    def test_packet_detects_drift_from_the_queue(self) -> None:
        result = self.add()
        packet = improve_log.render_packet(improve_log.load_reports())
        improve_log.set_status(result["id"], "resolved")
        problems = improve_log.validate_packet(packet)
        self.assertTrue(any("status differs" in item for item in problems))

    def test_packet_rejects_an_unknown_report(self) -> None:
        self.add()
        packet = improve_log.render_packet(improve_log.load_reports()).replace(
            improve_log.load_reports()[0].id, "CAMD-R-20260101-ZZZZZZ"
        )
        problems = improve_log.validate_packet(packet)
        self.assertTrue(any("no matching report" in item for item in problems))


class StatusTests(QueueTestCase):
    def test_quiet_queue_recommends_nothing(self) -> None:
        self.add(severity="minor")
        overview = improve_log.status_overview()
        self.assertFalse(overview["maintenance_recommended"])
        self.assertEqual(overview["open"], 1)

    def test_a_blocking_report_recommends_maintenance(self) -> None:
        self.add(severity="blocking")
        overview = improve_log.status_overview()
        self.assertTrue(overview["maintenance_recommended"])
        self.assertTrue(
            any("blocking" in reason for reason in overview["maintenance_reasons"])
        )

    def test_threshold_of_open_reports_recommends_maintenance(self) -> None:
        for index in range(improve_log.OPEN_REPORT_THRESHOLD):
            self.add(title=f"Distinct report number {index}", severity="minor")
        overview = improve_log.status_overview()
        self.assertTrue(overview["maintenance_recommended"])

    def test_publication_is_left_to_the_explicit_sync_preflight(self) -> None:
        overview = improve_log.status_overview()
        self.assertIsNone(overview["publication"]["available"])
        self.assertIn("improve_sync.py", overview["publication"]["reason"])


class IsolationTests(QueueTestCase):
    """The tool must be unable to reach the network or version control."""

    FORBIDDEN_IMPORTS = (
        "urllib",
        "http",
        "socket",
        "requests",
        "ssl",
        "ftplib",
        "smtplib",
    )

    def test_source_imports_no_network_library(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for name in self.FORBIDDEN_IMPORTS:
            with self.subTest(module=name):
                self.assertNotIn(f"import {name}", source)

    def test_source_invokes_no_version_control_or_forge_binary(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for binary in ('"git"', "'git'", '"gh"', "'gh'", "subprocess"):
            with self.subTest(binary=binary):
                self.assertNotIn(binary, source)

    def test_runs_with_git_and_gh_removed_from_path(self) -> None:
        poisoned = self.tmp / "poisoned-bin"
        poisoned.mkdir()
        for name in ("git", "gh"):
            script = poisoned / name
            script.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
            script.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": str(poisoned),
            "CAMD_STATE_DIR": str(self.state),
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "add",
                "--kind",
                "friction",
                "--title",
                "Captured without git or gh on PATH",
                "--body",
                "The queue must work in a hostile environment.",
                "--initiated-by",
                "user-request",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["action"], "created")


class CommandLineTests(QueueTestCase):
    def run_tool(self, *args: str, expect_success: bool = True):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "CAMD_STATE_DIR": str(self.state)},
        )
        if expect_success:
            self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def test_end_to_end_capture_list_draft_validate(self) -> None:
        created = json.loads(
            self.run_tool(
                "add",
                "--kind", "surprise",
                "--severity", "minor",
                "--component", "loop",
                "--title", "Refusal was correct but unexpected",
                "--body", "The adapter refused an unconfirmed project root.",
                "--initiated-by", "accepted-suggestion",
                "--agent", "claude-code",
            ).stdout
        )
        listing = json.loads(self.run_tool("list", "--open").stdout)
        self.assertEqual([item["id"] for item in listing], [created["id"]])
        self.assertEqual(listing[0]["initiated_by"], "accepted-suggestion")

        packet = self.tmp / "packet.md"
        self.run_tool("draft", "--out", str(packet))
        self.assertEqual(stat.S_IMODE(packet.stat().st_mode), 0o600)
        self.run_tool("validate", "--packet", str(packet))
        self.run_tool("validate")

    def test_public_view_lists_flags_without_the_evidence(self) -> None:
        evidence = self.tmp / "evidence.txt"
        evidence.write_text("local-only detail\n", encoding="utf-8")
        created = json.loads(
            self.run_tool(
                "add",
                "--kind", "bug",
                "--title", "Something failed",
                "--body", "Contact someone@example.com for the trace.",
                "--evidence-file", str(evidence),
                "--initiated-by", "user-request",
            ).stdout
        )
        public = json.loads(
            self.run_tool("show", created["id"], "--public").stdout
        )
        self.assertNotIn("local-only detail", public["public_view"])
        self.assertTrue(
            any(item["rule"] == "email-address" for item in public["redaction_flags"])
        )

    def test_unprompted_capture_is_refused_at_the_command_line(self) -> None:
        completed = self.run_tool(
            "add",
            "--kind", "bug",
            "--title", "Filed without asking",
            "--body", "This must not be possible.",
            expect_success=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--initiated-by", completed.stderr)


if __name__ == "__main__":
    unittest.main()
