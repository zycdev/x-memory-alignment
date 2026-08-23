"""Static checks for the public GitHub surface.

The label vocabulary and the issue forms exist to stay reconcilable with the
maintainers' private improvement queue. Nothing enforces that by itself, so
these tests compare the published surface with the vocabularies defined in
`improve_log.py` and fail when the two drift apart.

No test here contacts GitHub. They read files only.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

try:  # PyYAML is convenient but must not become a hard dependency of the suite.
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
GITHUB = ROOT / ".github"
LABELS = GITHUB / "labels.yml"
TEMPLATES = GITHUB / "ISSUE_TEMPLATE"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
MARKER = "camd-report-id"

SPEC = importlib.util.spec_from_file_location(
    "improve_log",
    ROOT / "skills" / "cross-agent-memory-distiller" / "scripts" / "improve_log.py",
)
assert SPEC and SPEC.loader
improve_log = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = improve_log
SPEC.loader.exec_module(improve_log)

LABEL_NAME_RE = re.compile(r"(?m)^- name: (\S+)$")
COLOR_RE = re.compile(r"(?m)^  color: ([0-9a-f]{6})$")


def flat(text: str) -> str:
    """Collapse wrapping so prose assertions do not depend on line breaks."""

    return re.sub(r"\s+", " ", text).strip().lower()


def label_names() -> list[str]:
    return LABEL_NAME_RE.findall(LABELS.read_text(encoding="utf-8"))


def issue_forms() -> list[Path]:
    return sorted(path for path in TEMPLATES.glob("*.yml") if path.name != "config.yml")


class LabelVocabularyTests(unittest.TestCase):
    def test_every_kind_component_and_agent_has_a_label(self) -> None:
        names = set(label_names())
        for family, values in (
            ("kind", improve_log.KINDS),
            ("component", improve_log.COMPONENTS),
            ("agent", improve_log.AGENTS),
        ):
            for value in values:
                with self.subTest(label=f"{family}/{value}"):
                    self.assertIn(f"{family}/{value}", names)

    def test_no_label_exists_outside_the_four_families(self) -> None:
        allowed = {
            f"kind/{value}" for value in improve_log.KINDS
        } | {
            f"component/{value}" for value in improve_log.COMPONENTS
        } | {
            f"agent/{value}" for value in improve_log.AGENTS
        }
        for name in label_names():
            with self.subTest(label=name):
                self.assertTrue(
                    name in allowed or re.fullmatch(r"cycle/\d+", name),
                    f"unexpected label outside the documented families: {name}",
                )

    def test_label_names_are_unique(self) -> None:
        names = label_names()
        self.assertEqual(len(names), len(set(names)))

    def test_a_cycle_label_documents_the_pattern(self) -> None:
        self.assertTrue(any(re.fullmatch(r"cycle/\d+", name) for name in label_names()))

    def test_dispositions_are_not_labels(self) -> None:
        # Triage dispositions live in the private queue. Two records that can
        # disagree is exactly what the loop design avoids.
        names = " ".join(label_names())
        for disposition in ("fix-now", "fix-later", "wontfix", "needs-info", "duplicate"):
            with self.subTest(disposition=disposition):
                self.assertNotIn(disposition, names)

    def test_colors_are_six_lowercase_hex_digits(self) -> None:
        text = LABELS.read_text(encoding="utf-8")
        self.assertEqual(len(COLOR_RE.findall(text)), len(label_names()))

    @unittest.skipIf(yaml is None, "PyYAML is unavailable")
    def test_labels_parse_and_carry_short_descriptions(self) -> None:
        entries = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
        self.assertIsInstance(entries, list)
        for entry in entries:
            with self.subTest(label=entry.get("name")):
                self.assertEqual(set(entry) , {"name", "color", "description"})
                description = entry["description"]
                self.assertTrue(description.strip())
                # GitHub truncates label descriptions beyond 100 characters.
                self.assertLessEqual(len(description), 100)


class IssueTemplateTests(unittest.TestCase):
    def test_both_forms_exist_with_a_config(self) -> None:
        self.assertTrue((TEMPLATES / "config.yml").is_file())
        self.assertEqual(len(issue_forms()), 2)

    def test_every_form_explains_the_origin_marker(self) -> None:
        for path in issue_forms():
            with self.subTest(form=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(MARKER, text)
                # Either phrasing is fine; the instruction must be present.
                self.assertIn("do not add", flat(text))

    def test_every_form_warns_against_private_material(self) -> None:
        for path in issue_forms():
            with self.subTest(form=path.name):
                text = flat(path.read_text(encoding="utf-8"))
                for phrase in ("credential", "absolute path", "memory"):
                    self.assertIn(phrase, text)

    def test_component_and_agent_options_match_the_queue(self) -> None:
        for path in issue_forms():
            text = path.read_text(encoding="utf-8")
            for value in improve_log.COMPONENTS:
                with self.subTest(form=path.name, component=value):
                    self.assertRegex(text, rf"(?m)^\s+- {re.escape(value)}$")
            for value in improve_log.AGENTS:
                with self.subTest(form=path.name, agent=value):
                    self.assertRegex(text, rf"(?m)^\s+- {re.escape(value)}$")

    def test_the_second_form_offers_the_non_defect_kinds(self) -> None:
        text = (TEMPLATES / "02-friction-idea.yml").read_text(encoding="utf-8")
        for value in improve_log.KINDS:
            if value == "bug":
                continue
            with self.subTest(kind=value):
                self.assertRegex(text, rf"(?m)^\s+- {re.escape(value)}$")

    def test_surprise_is_offered_as_its_own_kind(self) -> None:
        text = (TEMPLATES / "02-friction-idea.yml").read_text(encoding="utf-8")
        self.assertIn("surprise", text)

    @unittest.skipIf(yaml is None, "PyYAML is unavailable")
    def test_forms_parse_and_declare_the_required_fields(self) -> None:
        for path in issue_forms():
            with self.subTest(form=path.name):
                form = yaml.safe_load(path.read_text(encoding="utf-8"))
                for field in ("name", "description", "body"):
                    self.assertIn(field, form)
                self.assertTrue(form["body"])
                labels = form.get("labels", [])
                self.assertIsInstance(labels, list)
                for label in labels:
                    self.assertIn(label, label_names())

    @unittest.skipIf(yaml is None, "PyYAML is unavailable")
    def test_config_parses_and_links_the_contribution_guide(self) -> None:
        config = yaml.safe_load((TEMPLATES / "config.yml").read_text(encoding="utf-8"))
        links = config.get("contact_links", [])
        self.assertTrue(any("CONTRIBUTING.md" in link.get("url", "") for link in links))


class ContributingGuideTests(unittest.TestCase):
    def test_guide_exists_and_covers_the_required_topics(self) -> None:
        text = flat(CONTRIBUTING.read_text(encoding="utf-8"))
        for phrase in (
            "private, offline queue",
            "publication is a separate, explicit, batched act",
            "never adopted, rewritten, or closed",
            "marker matching cannot detect semantic duplicates",
            "maintainers close issues",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_guide_documents_the_marker_and_forbids_adding_one(self) -> None:
        text = CONTRIBUTING.read_text(encoding="utf-8")
        self.assertIn(MARKER, text)
        self.assertIn("not add such a marker", flat(text))

    def test_guide_states_the_semantic_deduplication_limit(self) -> None:
        text = flat(CONTRIBUTING.read_text(encoding="utf-8"))
        self.assertIn("semantic duplicates", text)
        self.assertIn("may coexist", text)

    def test_guide_names_the_validation_commands(self) -> None:
        text = CONTRIBUTING.read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests", text)
        self.assertIn("ruff check", text)


class PrivacyTests(unittest.TestCase):
    """The published surface must not leak the machine it was written on."""

    PRIVATE = re.compile(r"/home/[a-z0-9_-]+/|/Users/[a-z0-9_-]+/|/data/[a-z0-9_-]+/")

    def test_no_private_absolute_paths_are_published(self) -> None:
        files = [CONTRIBUTING, *GITHUB.rglob("*")]
        for path in files:
            if not path.is_file():
                continue
            with self.subTest(file=path.relative_to(ROOT).as_posix()):
                self.assertIsNone(self.PRIVATE.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
