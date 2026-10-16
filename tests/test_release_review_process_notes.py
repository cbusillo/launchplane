"""Client checklist checks survive removal of agent release-process conditions."""

import unittest

from control_plane.release_review_github import read_release_changes, release_test_notes
from tests.test_release_review import BASE, HEAD, github_read


class ReleaseProcessNotesTests(unittest.TestCase):
    def test_internal_handoff_condition_is_removed_without_losing_the_check(self) -> None:
        notes = (
            "Check sustained stability, then accept the exact proposed release through "
            "Launchplane after its completed cleanup and memory evidence is recorded."
        )
        displayed, changed = release_test_notes(notes)
        self.assertTrue(changed)
        self.assertIn("Check sustained stability", displayed)
        self.assertNotIn("cleanup", displayed)
        self.assertNotIn("memory evidence", displayed)
        self.assertNotIn("accept the exact proposed release", displayed)

    def test_unrelated_product_instructions_and_safety_limits_are_preserved(self) -> None:
        for notes in (
            "Check that the Accept button saves the order. Then check the order history.",
            "Check the cleanup status on the testing site's jobs page.",
            "Backup retention is still being checked and is not stated in the notice yet.",
            "Check the page. Record what you find in the Launchplane release review.",
            "Nothing for the Client to test: Internal cleanup and memory evidence only.",
            "Check the available memory in Launchplane's service diagnostics.",
        ):
            with self.subTest(notes=notes):
                self.assertEqual(release_test_notes(notes), (notes, False))

    def test_compiled_item_marks_process_sanitation_and_keeps_source_body(self) -> None:
        notes = (
            "Open the testing site and check the contact form.\n"
            "Then accept the release through Launchplane after cleanup evidence is recorded."
        )
        pulls = github_read("pulls")
        assert isinstance(pulls, list)
        pulls[0]["body"] = "## Client test notes\n" + notes
        original = pulls[0]["body"]
        items, _ = read_release_changes(
            repository="example/site",
            production_commit=BASE,
            candidate_commit=HEAD,
            read=lambda path: github_read(path) if "/compare/" in path else pulls,
        )
        self.assertEqual(pulls[0]["body"], original)
        self.assertTrue(items[0].preview_era_notes)
        self.assertIn("check the contact form", items[0].owner_test_notes)
        self.assertNotIn("cleanup evidence", items[0].owner_test_notes)

    def test_process_only_notes_do_not_become_a_complete_test_instruction(self) -> None:
        displayed, changed = release_test_notes(
            "Accept the release through Launchplane after cleanup and memory evidence is recorded."
        )
        self.assertTrue(changed)
        self.assertEqual(displayed, "")
