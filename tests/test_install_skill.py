"""Tests for the skill installer.

Every test redirects HOME, both agent homes, and the state directory into a
temporary tree. No test may modify a real installation, and no test may reach the
network: the release mode uses only local Git operations against a temporary
repository.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_skill.py"

SPEC = importlib.util.spec_from_file_location("install_skill", SCRIPT)
assert SPEC and SPEC.loader
install_skill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = install_skill
SPEC.loader.exec_module(install_skill)

GIT = shutil.which("git")


class InstallerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="camd-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.claude_home = self.home / ".claude"
        self.codex_home = self.home / ".codex"
        (self.claude_home / "skills").mkdir(parents=True)
        (self.codex_home / "skills").mkdir(parents=True)
        self.state = self.tmp / "state"
        patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "CLAUDE_CONFIG_DIR": str(self.claude_home),
                "CODEX_HOME": str(self.codex_home),
                "CAMD_STATE_DIR": str(self.state),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def claude_destination(self) -> Path:
        return self.claude_home / "skills" / install_skill.SKILL_NAME

    @property
    def codex_destination(self) -> Path:
        return self.codex_home / "skills" / install_skill.SKILL_NAME

    def targets(self, *names: str):
        return install_skill.resolve_targets(list(names) or None)


class TargetResolutionTests(InstallerTestCase):
    def test_agent_homes_are_taken_from_the_environment(self) -> None:
        catalogue = install_skill.known_targets()
        self.assertEqual(
            catalogue["claude"].destination, self.claude_destination
        )
        self.assertEqual(catalogue["codex"].destination, self.codex_destination)

    def test_the_agents_convention_is_opportunistic(self) -> None:
        self.assertEqual(
            {target.name for target in self.targets()}, {"claude", "codex"}
        )
        (self.home / ".agents" / "skills").mkdir(parents=True)
        self.assertEqual(
            {target.name for target in self.targets()},
            {"claude", "codex", "agents"},
        )

    def test_unknown_target_is_refused(self) -> None:
        with self.assertRaises(install_skill.InstallError):
            self.targets("emacs")


class LinkWorkingTests(InstallerTestCase):
    def test_plan_changes_nothing(self) -> None:
        actions = install_skill.plan_actions(
            self.targets(),
            mode="link-working",
            ref="main",
            force_replace=False,
            receipt=install_skill.load_receipt(),
        )
        self.assertEqual({item["action"] for item in actions}, {"create"})
        self.assertFalse(self.claude_destination.exists())
        self.assertFalse(install_skill.receipt_path().exists())

    def test_install_links_both_agents_at_the_canonical_skill(self) -> None:
        result = install_skill.install(self.targets(), mode="link-working")
        self.assertEqual(result["mode"], "link-working")
        for destination in (self.claude_destination, self.codex_destination):
            self.assertTrue(destination.is_symlink())
            self.assertEqual(
                destination.resolve(), install_skill.CANONICAL_SKILL.resolve()
            )
            self.assertTrue((destination / "SKILL.md").is_file())
        receipt = json.loads(install_skill.receipt_path().read_text(encoding="utf-8"))
        self.assertEqual(set(receipt["entries"]), {"claude", "codex"})
        self.assertIsNone(receipt["entries"]["claude"]["ref"])
        self.assertEqual(
            stat.S_IMODE(install_skill.receipt_path().stat().st_mode), 0o600
        )
        self.assertEqual(
            stat.S_IMODE(install_skill.receipt_path().parent.stat().st_mode), 0o700
        )

    def test_install_is_idempotent(self) -> None:
        install_skill.install(self.targets(), mode="link-working")
        first = self.claude_destination.readlink()
        result = install_skill.install(self.targets(), mode="link-working")
        self.assertEqual({item["action"] for item in result["actions"]}, {"update"})
        self.assertEqual(self.claude_destination.readlink(), first)

    def test_verify_reports_a_usable_installation(self) -> None:
        install_skill.install(self.targets(), mode="link-working")
        results = install_skill.verify(self.targets())
        self.assertTrue(all(item["ok"] for item in results), results)
        self.assertTrue(all(item["verify_hint"] for item in results))

    def test_verify_detects_a_dangling_installation(self) -> None:
        install_skill.install(self.targets("claude"), mode="link-working")
        self.claude_destination.unlink()
        self.claude_destination.symlink_to(self.tmp / "absent")
        results = install_skill.verify(self.targets("claude"))
        self.assertFalse(results[0]["ok"])
        self.assertIn("symlink does not resolve", results[0]["problems"])

    def test_verify_detects_invalid_helper_syntax_without_importing_it(self) -> None:
        install_skill.install(self.targets("claude"), mode="copy")
        helper = self.claude_destination / "scripts" / "improve_log.py"
        helper.write_text("def broken(:\n", encoding="utf-8")
        results = install_skill.verify(self.targets("claude"))
        self.assertFalse(results[0]["ok"])
        self.assertTrue(
            any("invalid Python syntax" in item for item in results[0]["problems"])
        )


class ForeignDestinationTests(InstallerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.foreign = self.claude_destination
        self.foreign.mkdir(parents=True)
        (self.foreign / "SKILL.md").write_text("---\nname: someone-else\n---\n")

    def test_conflict_is_reported_and_nothing_changes(self) -> None:
        with self.assertRaises(install_skill.InstallError) as error:
            install_skill.install(self.targets("claude"), mode="link-working")
        self.assertIn("refusing to touch", str(error.exception))
        self.assertIn("name: someone-else", (self.foreign / "SKILL.md").read_text())
        self.assertFalse(install_skill.receipt_path().exists())

    def test_force_replace_moves_the_existing_entry_aside(self) -> None:
        result = install_skill.install(
            self.targets("claude"), mode="link-working", force_replace=True
        )
        action = result["actions"][0]
        self.assertEqual(action["action"], "replace")
        moved = Path(action["moved_to"])
        self.assertTrue(moved.is_dir())
        self.assertIn("name: someone-else", (moved / "SKILL.md").read_text())
        self.assertTrue(self.claude_destination.is_symlink())

    def test_uninstall_leaves_a_foreign_destination_untouched(self) -> None:
        results = install_skill.uninstall(self.targets("claude"))
        self.assertEqual(results[0]["action"], "skip")
        self.assertTrue(self.foreign.is_dir())
        self.assertFalse(
            install_skill.receipt_path().exists(),
            "a no-op uninstall must not create state",
        )

    def test_symlink_writer_locally_refuses_an_unmanaged_directory(self) -> None:
        with self.assertRaises(install_skill.InstallError):
            install_skill._install_symlink(
                self.foreign, install_skill.CANONICAL_SKILL
            )
        self.assertTrue(self.foreign.is_dir())

    def test_copy_ownership_requires_the_recorded_destination_to_match(self) -> None:
        (self.foreign / install_skill.COPY_MARKER).write_text("{}\n")
        receipt = {
            "schema": install_skill.SCHEMA,
            "entries": {
                "claude": {
                    "destination": (self.tmp / "somewhere-else").as_posix(),
                    "mode": "copy",
                }
            },
        }
        self.assertFalse(
            install_skill.describe(self.targets("claude")[0], receipt)["ours"]
        )


class AdoptionTests(InstallerTestCase):
    def test_a_correct_hand_made_link_is_adopted_not_replaced(self) -> None:
        self.claude_destination.symlink_to(
            install_skill.CANONICAL_SKILL, target_is_directory=True
        )
        before = self.claude_destination.readlink()
        result = install_skill.install(self.targets("claude"), mode="link-working")
        action = result["actions"][0]
        self.assertEqual(action["action"], "adopt")
        self.assertEqual(self.claude_destination.readlink(), before)
        receipt = json.loads(install_skill.receipt_path().read_text(encoding="utf-8"))
        self.assertTrue(receipt["entries"]["claude"]["adopted"])
        self.assertEqual(receipt["entries"]["claude"]["origin"], "adopted")
        install_skill.install(self.targets("claude"), mode="link-working")
        receipt = json.loads(install_skill.receipt_path().read_text(encoding="utf-8"))
        self.assertEqual(receipt["entries"]["claude"]["origin"], "adopted")
        siblings = list(self.claude_destination.parent.iterdir())
        self.assertEqual(len(siblings), 1, "adoption must not create a backup")

    def test_uninstall_reports_that_an_adopted_link_is_being_removed(self) -> None:
        self.claude_destination.symlink_to(
            install_skill.CANONICAL_SKILL, target_is_directory=True
        )
        install_skill.install(self.targets("claude"), mode="link-working")
        result = install_skill.uninstall(self.targets("claude"))[0]
        self.assertEqual(result["action"], "removed")
        self.assertEqual(result["recorded_origin"], "adopted")


class CopyModeTests(InstallerTestCase):
    def test_copy_is_independent_of_the_repository(self) -> None:
        install_skill.install(self.targets("codex"), mode="copy")
        destination = self.codex_destination
        self.assertFalse(destination.is_symlink())
        self.assertTrue((destination / "SKILL.md").is_file())
        self.assertTrue((destination / "scripts" / "improve_log.py").is_file())
        marker = json.loads(
            (destination / install_skill.COPY_MARKER).read_text(encoding="utf-8")
        )
        self.assertEqual(marker["mode"], "copy")
        self.assertFalse((destination / "scripts" / "__pycache__").exists())

    def test_uninstall_removes_a_copy_it_created(self) -> None:
        install_skill.install(self.targets("codex"), mode="copy")
        results = install_skill.uninstall(self.targets("codex"))
        self.assertEqual(results[0]["action"], "removed")
        self.assertFalse(self.codex_destination.exists())


@unittest.skipIf(GIT is None, "git is unavailable")
class ReleaseModeTests(InstallerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        skill = self.repo / "skills" / install_skill.SKILL_NAME
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {install_skill.SKILL_NAME}\ndescription: test\n---\n",
            encoding="utf-8",
        )
        for script in install_skill.HELPER_SCRIPTS:
            (skill / "scripts" / script).write_text("# test\n", encoding="utf-8")
        self.git_environment = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        for command in (
            ["init", "-q", "-b", "main", str(self.repo)],
            ["-C", str(self.repo), "add", "-A"],
            ["-C", str(self.repo), "commit", "-q", "-m", "initial"],
        ):
            subprocess.run(
                ["git", *command], check=True, env=self.git_environment
            )
        patcher = mock.patch.multiple(
            install_skill,
            REPO_ROOT=self.repo,
            CANONICAL_SKILL=skill,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_release_mode_links_at_a_detached_worktree(self) -> None:
        result = install_skill.install(
            self.targets("claude"), mode="link-release", ref="main"
        )
        destination = self.claude_destination
        worktree_skill = (
            install_skill.release_worktree() / "skills" / install_skill.SKILL_NAME
        )
        self.assertEqual(destination.resolve(), worktree_skill.resolve())
        self.assertNotEqual(
            destination.resolve(), install_skill.CANONICAL_SKILL.resolve()
        )
        receipt = json.loads(install_skill.receipt_path().read_text(encoding="utf-8"))
        self.assertEqual(receipt["entries"]["claude"]["ref"], "main")
        self.assertEqual(result["actions"][0]["action"], "create")

    def test_unknown_ref_is_refused_without_fetching(self) -> None:
        with self.assertRaises(install_skill.InstallError) as error:
            install_skill.install(
                self.targets("claude"), mode="link-release", ref="v9.9.9"
            )
        self.assertIn("never contacts the network", str(error.exception))
        self.assertFalse(self.claude_destination.exists())

    def test_verify_detects_release_worktree_head_drift(self) -> None:
        install_skill.install(
            self.targets("claude"), mode="link-release", ref="main"
        )
        receipt = json.loads(install_skill.receipt_path().read_text(encoding="utf-8"))
        recorded = receipt["entries"]["claude"]["commit"]

        drift_marker = self.repo / "drift.txt"
        drift_marker.write_text("later commit\n", encoding="utf-8")
        for command in (
            ["-C", str(self.repo), "add", "drift.txt"],
            ["-C", str(self.repo), "commit", "-q", "-m", "later"],
        ):
            subprocess.run(
                ["git", *command], check=True, env=self.git_environment
            )
        later = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
            env=self.git_environment,
        ).stdout.strip()
        self.assertNotEqual(later, recorded)
        subprocess.run(
            [
                "git",
                "-C",
                str(install_skill.release_worktree()),
                "checkout",
                "-q",
                "--detach",
                later,
            ],
            check=True,
            env=self.git_environment,
        )

        result = install_skill.verify(self.targets("claude"))[0]
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                "differs from recorded commit" in problem
                for problem in result["problems"]
            ),
            result,
        )


    def test_reinstalling_one_target_reports_shared_worktree_drift(self) -> None:
        # v1 keeps one shared release worktree so both agents run the same
        # version by default. The cost is that reinstalling either target moves
        # the worktree for the other, which must be diagnosable rather than
        # mysterious: the untouched target is the one that reports drift.
        install_skill.install(
            self.targets("claude"), mode="link-release", ref="main"
        )
        marker = self.repo / "later.txt"
        marker.write_text("later commit\n", encoding="utf-8")
        for command in (
            ["-C", str(self.repo), "add", "later.txt"],
            ["-C", str(self.repo), "commit", "-q", "-m", "later"],
        ):
            subprocess.run(["git", *command], check=True, env=self.git_environment)
        later = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
            env=self.git_environment,
        ).stdout.strip()
        install_skill.install(
            self.targets("codex"), mode="link-release", ref=later
        )
        codex_result = install_skill.verify(self.targets("codex"))[0]
        self.assertTrue(codex_result["ok"], codex_result)

        claude_result = install_skill.verify(self.targets("claude"))[0]
        self.assertFalse(claude_result["ok"])
        problem = " ".join(claude_result["problems"])
        self.assertIn("differs from recorded commit", problem)
        self.assertIn("shared", problem)


class CommandLineTests(InstallerTestCase):
    def run_installer(self, *args: str, expect_success: bool = True):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            check=False,
            capture_output=True,
            text=True,
            env=dict(os.environ),
        )
        if expect_success:
            self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def test_install_without_yes_is_a_dry_run(self) -> None:
        payload = json.loads(
            self.run_installer("install", "--mode", "link-working").stdout
        )
        self.assertTrue(payload["dry_run"])
        self.assertFalse(self.claude_destination.exists())

    def test_status_install_verify_uninstall(self) -> None:
        status = json.loads(self.run_installer("status").stdout)
        self.assertEqual(
            {item["state"] for item in status["targets"]}, {"absent"}
        )
        self.run_installer("install", "--mode", "link-working", "--yes")
        self.run_installer("verify")
        removed = json.loads(self.run_installer("uninstall", "--yes").stdout)
        self.assertEqual({item["action"] for item in removed}, {"removed"})
        self.assertFalse(self.claude_destination.exists())

    def test_verify_exits_non_zero_when_nothing_is_installed(self) -> None:
        completed = self.run_installer("verify", expect_success=False)
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
