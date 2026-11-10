"""Structural repository invariants.

Global installation is the sole discovery entrypoint for this skill. A
same-name skill under any project-level dot convention would reintroduce a
second discovery path and make agent precedence relevant again.

These invariants must hold on the development branch and on the published
branch alike. The published tree carries no agent coordination files, so the
checks that concern them state what must be true when they are present rather
than requiring their presence.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "x-memory-alignment"


def tracked_symlinks() -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s"],
        check=True,
        capture_output=True,
        text=True,
    )
    links = []
    for line in completed.stdout.splitlines():
        mode, _, remainder = line.partition(" ")
        if mode != "120000":
            continue
        links.append(ROOT / remainder.split("\t", 1)[1])
    return links


class TrackedSymlinkTests(unittest.TestCase):
    def test_every_tracked_symlink_resolves(self) -> None:
        for link in tracked_symlinks():
            with self.subTest(link=link.relative_to(ROOT).as_posix()):
                self.assertTrue(link.is_symlink())
                self.assertTrue(
                    link.exists(),
                    f"dangling symlink: {link.relative_to(ROOT)} -> {link.readlink()}",
                )

    def test_every_tracked_symlink_stays_inside_the_repository(self) -> None:
        for link in tracked_symlinks():
            with self.subTest(link=link.relative_to(ROOT).as_posix()):
                self.assertFalse(link.readlink().is_absolute())
                self.assertTrue(link.resolve().is_relative_to(ROOT))

    def test_project_skill_shadow_paths_are_absent(self) -> None:
        shadow_paths = []
        for convention in ROOT.iterdir():
            if not convention.name.startswith(".") or not convention.is_dir():
                continue
            candidate = convention / "skills" / SKILL_NAME
            if candidate.exists() or candidate.is_symlink():
                shadow_paths.append(candidate.relative_to(ROOT).as_posix())
        self.assertEqual(shadow_paths, [])

    def test_shared_instructions_link_is_relative_when_present(self) -> None:
        claude_md = ROOT / "CLAUDE.md"
        if not claude_md.is_symlink():
            self.skipTest("no shared instruction link in this tree")
        self.assertEqual(claude_md.readlink(), Path("AGENTS.md"))
        self.assertEqual(claude_md.resolve(), (ROOT / "AGENTS.md").resolve())


if __name__ == "__main__":
    unittest.main()
