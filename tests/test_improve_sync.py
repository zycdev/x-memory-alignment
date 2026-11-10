from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "x-memory-alignment" / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


improve_log = load_module("improve_log", SCRIPTS / "improve_log.py")
improve_sync = load_module("improve_sync", SCRIPTS / "improve_sync.py")
LABELS_FILE = ROOT / ".github" / "labels.yml"


class FakeRunner:
    def __init__(
        self,
        *,
        remote: bool = True,
        authenticated: bool = True,
        permission: str = "ADMIN",
    ):
        self.remote = remote
        self.authenticated = authenticated
        self.permission = permission
        self.repo = "example/x-memory-alignment"
        self.labels: set[str] = set()
        self.issues: list[dict] = []
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.mutations: list[tuple[str, ...]] = []
        self.next_issue = 1
        self.issue_view_error = "not found"

    def which(self, name: str) -> str | None:
        return f"/fake/{name}" if name == "gh" else None

    def run(self, args, *, input_text=None):
        command = tuple(str(item) for item in args)
        self.calls.append((command, input_text))
        if command[:5] == ("git", "-C", command[2], "remote", "get-url"):
            if not self.remote:
                return improve_sync.CommandResult(2, stderr="no such remote")
            return improve_sync.CommandResult(
                0, f"https://github.com/{self.repo}.git\n"
            )
        if command[:3] == ("gh", "auth", "status"):
            return improve_sync.CommandResult(0 if self.authenticated else 1)
        if command[:3] == ("gh", "api", "user"):
            return improve_sync.CommandResult(
                0 if self.authenticated else 1,
                "example\n" if self.authenticated else "",
            )
        if command[:3] == ("gh", "repo", "view"):
            return improve_sync.CommandResult(
                0,
                json.dumps(
                    {
                        "nameWithOwner": self.repo,
                        "visibility": "PUBLIC",
                        "viewerPermission": self.permission,
                    }
                ),
            )
        if command[:3] == ("gh", "issue", "list"):
            return improve_sync.CommandResult(0, json.dumps(self.issues))
        if command[:3] == ("gh", "label", "list"):
            return improve_sync.CommandResult(
                0, json.dumps([{"name": name} for name in sorted(self.labels)])
            )
        if command[:3] == ("gh", "label", "create"):
            self.mutations.append(command)
            self.labels.add(command[3])
            return improve_sync.CommandResult(0)
        if command[:3] == ("gh", "issue", "create"):
            self.mutations.append(command)
            number = self.next_issue
            self.next_issue += 1
            title = command[command.index("--title") + 1]
            label_text = command[command.index("--label") + 1]
            url = f"https://github.com/{self.repo}/issues/{number}"
            self.issues.append(
                {
                    "number": number,
                    "state": "OPEN",
                    "title": title,
                    "body": input_text or "",
                    "url": url,
                    "labels": [
                        {"name": name} for name in label_text.split(",") if name
                    ],
                }
            )
            return improve_sync.CommandResult(0, url + "\n")
        if command[:3] == ("gh", "issue", "edit"):
            self.mutations.append(command)
            number = int(command[3])
            issue = self.issue(number)
            issue["title"] = command[command.index("--title") + 1]
            issue["body"] = input_text or ""
            for name in command[command.index("--add-label") + 1].split(","):
                if name and {"name": name} not in issue["labels"]:
                    issue["labels"].append({"name": name})
            if "--remove-label" in command:
                removed = set(
                    command[command.index("--remove-label") + 1].split(",")
                )
                issue["labels"] = [
                    item for item in issue["labels"] if item["name"] not in removed
                ]
            return improve_sync.CommandResult(0)
        if command[:3] == ("gh", "issue", "view"):
            number = int(command[3])
            try:
                return improve_sync.CommandResult(0, json.dumps(self.issue(number)))
            except KeyError:
                return improve_sync.CommandResult(1, stderr=self.issue_view_error)
        if command[:3] == ("gh", "issue", "close"):
            self.mutations.append(command)
            self.issue(int(command[3]))["state"] = "CLOSED"
            return improve_sync.CommandResult(0)
        if command[:4] == ("git", "-C", command[2], "rev-parse"):
            return improve_sync.CommandResult(0, "a" * 40 + "\n")
        raise AssertionError(f"unexpected command: {command}")

    def issue(self, number: int) -> dict:
        for issue in self.issues:
            if issue["number"] == number:
                return issue
        raise KeyError(number)


class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)
        self.state = self.tmp / "state"
        self.repo_root = self.tmp / "repo"
        self.repo_root.mkdir()
        self.old_state = os.environ.get("CAMD_STATE_DIR")
        os.environ["CAMD_STATE_DIR"] = str(self.state)

    def tearDown(self) -> None:
        if self.old_state is None:
            os.environ.pop("CAMD_STATE_DIR", None)
        else:
            os.environ["CAMD_STATE_DIR"] = self.old_state
        self.temp.cleanup()

    def report(self, *, body: str = "A reviewed improvement.", triaged: bool = True):
        created = improve_log.add_report(
            kind="bug",
            severity="major",
            component="claude-adapter",
            title="Adapter behavior needs correction",
            body=body,
            initiated_by="user-request",
            agent="codex",
        )
        if triaged:
            improve_log.set_status(created["id"], "triaged")
        return improve_log.find_report(created["id"])

    def plan(self, runner: FakeRunner, report_id: str):
        return improve_sync.build_push_plan(
            repo_root=self.repo_root,
            expected_repo=runner.repo,
            identifiers=[report_id],
            cycle=1,
            runner=runner,
        )


class AuthorizationTests(SyncTestCase):
    def test_both_maintenance_signals_are_required(self) -> None:
        with self.assertRaises(improve_sync.SyncError):
            improve_sync.require_maintenance(False, {})
        with self.assertRaises(improve_sync.SyncError):
            improve_sync.require_maintenance(True, {})
        improve_sync.require_maintenance(
            True, {improve_sync.MAINTENANCE_ENV: improve_sync.MAINTENANCE_VALUE}
        )

    def test_missing_remote_reports_unavailable_without_local_change(self) -> None:
        report = self.report()
        before = report.path.read_bytes()
        runner = FakeRunner(remote=False)
        plan = self.plan(runner, report.id)
        self.assertFalse(plan["preflight"]["available"])
        self.assertIn("origin", plan["preflight"]["reason"])
        self.assertEqual(report.path.read_bytes(), before)
        self.assertEqual(runner.mutations, [])

    def test_duplicate_report_selection_is_refused(self) -> None:
        report = self.report()
        runner = FakeRunner()
        with self.assertRaisesRegex(improve_sync.SyncError, "more than once"):
            improve_sync.build_push_plan(
                repo_root=self.repo_root,
                expected_repo=runner.repo,
                identifiers=[report.id, report.id],
                cycle=1,
                runner=runner,
            )

    def test_failed_authentication_stops_before_issue_queries(self) -> None:
        report = self.report()
        runner = FakeRunner(authenticated=False)
        plan = self.plan(runner, report.id)
        self.assertFalse(plan["preflight"]["available"])
        self.assertFalse(any(call[0][:3] == ("gh", "issue", "list") for call in runner.calls))
        self.assertEqual(runner.mutations, [])

    def test_read_only_permission_stops_before_issue_queries(self) -> None:
        report = self.report()
        runner = FakeRunner(permission="READ")
        plan = self.plan(runner, report.id)
        self.assertFalse(plan["preflight"]["available"])
        self.assertIn("write permission", plan["preflight"]["reason"])
        self.assertFalse(
            any(call[0][:3] == ("gh", "issue", "list") for call in runner.calls)
        )
        self.assertEqual(runner.mutations, [])


class PushTests(SyncTestCase):
    def test_unfiltered_plan_cannot_be_applied(self) -> None:
        self.report()
        runner = FakeRunner()
        plan = improve_sync.build_push_plan(
            repo_root=self.repo_root,
            expected_repo=runner.repo,
            identifiers=None,
            cycle=1,
            runner=runner,
        )
        self.assertFalse(plan["explicit_selection"])
        with self.assertRaisesRegex(improve_sync.SyncError, "explicit --report"):
            improve_sync.apply_push_plan(plan, runner)
        self.assertEqual(runner.mutations, [])

    def test_label_creation_specs_match_the_public_source_of_truth(self) -> None:
        text = LABELS_FILE.read_text(encoding="utf-8")
        entries = re.findall(
            r"(?m)^- name: (\S+)\n  color: ([0-9a-f]{6})\n  description: (.+)$",
            text,
        )
        for name, color, description in entries:
            with self.subTest(label=name):
                self.assertEqual(improve_sync.label_style(name), (color, description))

    def test_redaction_flags_block_the_whole_push_before_mutation(self) -> None:
        report = self.report(body="See /home/example/private/file for evidence.")
        runner = FakeRunner()
        plan = self.plan(runner, report.id)
        self.assertEqual(plan["actions"][0]["action"], "blocked")
        with self.assertRaises(improve_sync.SyncError):
            improve_sync.apply_push_plan(plan, runner)
        self.assertEqual(runner.mutations, [])

    def test_push_creates_labels_and_one_marker_then_converges(self) -> None:
        report = self.report()
        runner = FakeRunner()
        first = self.plan(runner, report.id)
        self.assertEqual(first["actions"][0]["action"], "create")
        outcome = improve_sync.apply_push_plan(first, runner)
        self.assertEqual(outcome["results"][0]["issue"], 1)
        self.assertEqual(runner.issue(1)["body"].count("camd-report-id"), 1)
        linked = improve_log.find_report(report.id)
        self.assertEqual(linked.status, "published")
        self.assertEqual(linked.metadata["github"]["issue"], 1)

        mutation_count = len(runner.mutations)
        second = self.plan(runner, report.id)
        self.assertEqual(second["actions"][0]["action"], "noop")
        improve_sync.apply_push_plan(second, runner)
        self.assertEqual(len(runner.mutations), mutation_count)

    def test_remote_marker_is_linked_without_creating_a_duplicate(self) -> None:
        report = self.report()
        runner = FakeRunner()
        runner.labels.update(improve_sync.labels_for(report, 1))
        runner.issues.append(
            {
                "number": 7,
                "state": "OPEN",
                "title": report.title,
                "body": improve_sync.render_issue_body(report),
                "url": f"https://github.com/{runner.repo}/issues/7",
                "labels": [{"name": name} for name in improve_sync.labels_for(report, 1)],
            }
        )
        plan = self.plan(runner, report.id)
        self.assertEqual(plan["actions"][0]["action"], "link")
        improve_sync.apply_push_plan(plan, runner)
        self.assertFalse(any(command[:3] == ("gh", "issue", "create") for command in runner.mutations))
        self.assertEqual(improve_log.find_report(report.id).metadata["github"]["issue"], 7)

    def test_maintainer_body_edit_blocks_instead_of_being_overwritten(self) -> None:
        report = self.report()
        runner = FakeRunner()
        improve_sync.apply_push_plan(self.plan(runner, report.id), runner)
        runner.issue(1)["body"] += "\nMaintainer investigation note.\n"

        plan = self.plan(runner, report.id)
        self.assertEqual(plan["actions"][0]["action"], "blocked")
        self.assertIn("diverged", plan["actions"][0]["reasons"][0])
        mutation_count = len(runner.mutations)
        with self.assertRaises(improve_sync.SyncError):
            improve_sync.apply_push_plan(plan, runner)
        self.assertEqual(len(runner.mutations), mutation_count)
        self.assertIn("Maintainer investigation note", runner.issue(1)["body"])

    def test_local_occurrence_updates_when_remote_body_is_unchanged(self) -> None:
        report = self.report()
        runner = FakeRunner()
        improve_sync.apply_push_plan(self.plan(runner, report.id), runner)
        improve_log.add_report(
            kind="bug",
            severity="major",
            component="claude-adapter",
            title=report.title,
            body="A reviewed improvement.",
            initiated_by="user-request",
            agent="claude-code",
        )

        current = improve_log.find_report(report.id)
        plan = self.plan(runner, current.id)
        self.assertEqual(plan["actions"][0]["action"], "update")
        improve_sync.apply_push_plan(plan, runner)
        self.assertIn("Occurrences: `2`", runner.issue(1)["body"])

    def test_owned_labels_are_reconciled_without_removing_human_labels(self) -> None:
        report = self.report()
        runner = FakeRunner()
        improve_sync.apply_push_plan(self.plan(runner, report.id), runner)
        runner.issue(1)["labels"].append({"name": "help wanted"})
        current = improve_log.find_report(report.id)
        metadata = dict(current.metadata)
        metadata["component"] = "guard"
        improve_log._rewrite_private_file(
            current.path, improve_log.render_report(metadata, current.body)
        )

        plan = self.plan(runner, report.id)
        action = plan["actions"][0]
        self.assertEqual(action["action"], "update")
        self.assertIn("component/claude-adapter", action["remove_labels"])
        improve_sync.apply_push_plan(plan, runner)
        labels = {item["name"] for item in runner.issue(1)["labels"]}
        self.assertIn("component/guard", labels)
        self.assertNotIn("component/claude-adapter", labels)
        self.assertIn("help wanted", labels)

    def test_local_occurrence_after_plan_stops_before_remote_mutation(self) -> None:
        report = self.report()
        runner = FakeRunner()
        plan = self.plan(runner, report.id)
        improve_log.add_report(
            kind="bug",
            severity="major",
            component="claude-adapter",
            title=report.title,
            body="A reviewed improvement.",
            initiated_by="user-request",
            agent="claude-code",
        )
        with self.assertRaisesRegex(improve_sync.SyncError, "changed after planning"):
            improve_sync.apply_push_plan(plan, runner)
        current = improve_log.find_report(report.id)
        self.assertEqual(current.metadata["occurrences"], 2)
        self.assertNotIn("github", current.metadata)
        self.assertEqual(runner.issues, [])
        self.assertEqual(runner.mutations, [])


class PullAndCloseTests(SyncTestCase):
    def test_pull_cannot_use_a_remote_marker_to_bypass_local_triage(self) -> None:
        report = self.report(triaged=False)
        runner = FakeRunner()
        runner.issues.append(
            {
                "number": 4,
                "state": "OPEN",
                "title": report.title,
                "body": improve_sync.render_issue_body(report),
                "url": f"https://github.com/{runner.repo}/issues/4",
                "labels": [],
            }
        )
        plan = improve_sync.build_pull_plan(
            repo_root=self.repo_root, expected_repo=runner.repo, runner=runner
        )
        self.assertEqual(plan["actions"][0]["action"], "blocked")
        improve_sync.apply_pull_plan(plan)
        self.assertEqual(improve_log.find_report(report.id).status, "new")

    def test_pull_links_a_reviewed_report_without_remote_mutation(self) -> None:
        report = self.report()
        runner = FakeRunner()
        runner.issues.append(
            {
                "number": 5,
                "state": "OPEN",
                "title": report.title,
                "body": improve_sync.render_issue_body(report),
                "url": f"https://github.com/{runner.repo}/issues/5",
                "labels": [],
            }
        )
        plan = improve_sync.build_pull_plan(
            repo_root=self.repo_root, expected_repo=runner.repo, runner=runner
        )
        self.assertEqual(plan["actions"][0]["action"], "link")
        improve_sync.apply_pull_plan(plan)
        linked = improve_log.find_report(report.id)
        self.assertEqual(linked.status, "published")
        self.assertEqual(linked.metadata["github"]["issue"], 5)
        self.assertEqual(runner.mutations, [])

    def test_pull_does_not_treat_a_transient_view_failure_as_deletion(self) -> None:
        report = self.report()
        runner = FakeRunner()
        improve_sync.apply_push_plan(self.plan(runner, report.id), runner)
        runner.issues.clear()
        runner.issue_view_error = "network timed out"
        with self.assertRaisesRegex(improve_sync.SyncError, "cannot determine"):
            improve_sync.build_pull_plan(
                repo_root=self.repo_root, expected_repo=runner.repo, runner=runner
            )
        linked = improve_log.find_report(report.id)
        self.assertFalse(linked.metadata["github"]["missing"])

    def test_pull_marks_a_confirmed_missing_issue_for_user_review(self) -> None:
        report = self.report()
        runner = FakeRunner()
        improve_sync.apply_push_plan(self.plan(runner, report.id), runner)
        runner.issues.clear()
        plan = improve_sync.build_pull_plan(
            repo_root=self.repo_root, expected_repo=runner.repo, runner=runner
        )
        self.assertEqual(plan["actions"][0]["action"], "missing")
        self.assertTrue(plan["actions"][0]["ask_user"])
        improve_sync.apply_pull_plan(plan)
        linked = improve_log.find_report(report.id)
        self.assertTrue(linked.metadata["github"]["missing"])
        self.assertEqual(linked.status, "published")

    def test_pull_reconciles_a_linked_issue_omitted_from_the_snapshot(self) -> None:
        report = self.report()
        runner = FakeRunner()
        improve_sync.apply_push_plan(self.plan(runner, report.id), runner)
        linked = improve_log.find_report(report.id)
        issue = runner.issue(1)
        issue["state"] = "CLOSED"

        original_run = runner.run

        def omit_from_list(args, *, input_text=None):
            command = tuple(str(item) for item in args)
            if command[:3] == ("gh", "issue", "list"):
                runner.calls.append((command, input_text))
                return improve_sync.CommandResult(0, "[]")
            return original_run(args, input_text=input_text)

        runner.run = omit_from_list
        plan = improve_sync.build_pull_plan(
            repo_root=self.repo_root, expected_repo=runner.repo, runner=runner
        )
        action = next(item for item in plan["actions"] if item["id"] == linked.id)
        self.assertEqual(action["action"], "link")
        self.assertEqual(action["state"], "closed")
        improve_sync.apply_pull_plan(plan)
        self.assertEqual(
            improve_log.find_report(report.id).metadata["github"]["state"], "closed"
        )

    def test_close_requires_local_resolution_and_records_remote_state(self) -> None:
        report = self.report()
        runner = FakeRunner()
        improve_sync.apply_push_plan(self.plan(runner, report.id), runner)
        improve_log.set_status(report.id, "resolved", "Fixed with a regression test.")
        plan = improve_sync.build_close_plan(
            repo_root=self.repo_root,
            expected_repo=runner.repo,
            identifiers=[report.id],
            commit="a" * 40,
            test_evidence="All regression tests pass.",
            runner=runner,
        )
        self.assertEqual(plan["actions"][0]["action"], "close")
        self.assertEqual(plan["actions"][0]["commit"], "a" * 12)
        improve_sync.apply_close_plan(plan, runner)
        self.assertEqual(runner.issue(1)["state"], "CLOSED")
        closed = improve_log.find_report(report.id)
        self.assertEqual(closed.status, "resolved")
        self.assertEqual(closed.metadata["github"]["state"], "closed")


if __name__ == "__main__":
    unittest.main()
