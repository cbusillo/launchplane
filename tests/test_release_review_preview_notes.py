"""October 8 preview-era notes, retained from GitHub PR body edit history."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import cast

from control_plane.contracts.preview_record import PreviewRecord
from control_plane.release_review import ReleaseReviewStore, build_release_review, checklist_digest
from control_plane.release_review_github import read_release_changes, release_test_notes
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.odoo_preview_apply_http import _odoo_preview_anchor_repo
from tests.test_release_review import BASE, HEAD, github_read, profile, seed


NOTES = json.loads(
    (Path(__file__).parent / "fixtures/release_review_preview_notes.json").read_text()
)


class PreviewNotesTests(unittest.TestCase):
    def test_october_8_notes_compile_without_mutating_pr_bodies(self) -> None:
        pulls = []
        for number, notes in NOTES.items():
            pull = github_read("pulls")
            assert isinstance(pull, list)
            pulls.append(
                {**pull[0], "number": int(number), "body": "## Owner test notes\n" + notes}
            )
        original = [pull["body"] for pull in pulls]
        items, _ = read_release_changes(
            repository="example/site",
            production_commit=BASE,
            candidate_commit=HEAD,
            read=lambda path: github_read(path) if "/compare/" in path else pulls,
        )
        self.assertEqual([pull["body"] for pull in pulls], original)
        by_number = {item.pull_request_number: item for item in items}
        for number in (96, 116):
            item = by_number[number]
            with self.subTest(number=number):
                self.assertTrue(item.preview_era_notes)
                self.assertNotIn("cm-website-preview", item.owner_test_notes)
                self.assertNotIn("/ui/owner-review", item.owner_test_notes)
                self.assertEqual(item.owner_test_notes.count("Check this on the testing site."), 2)
                self.assertIn("Check this on the testing site.", item.owner_test_notes.splitlines())
        self.assertIn('Check that "How long we keep it"', by_number[96].owner_test_notes)
        self.assertIn("Backup retention is still being checked", by_number[96].owner_test_notes)
        self.assertIn("Check that the main buttons", by_number[116].owner_test_notes)
        self.assertEqual(by_number[121].owner_test_notes, NOTES["121"])
        self.assertFalse(by_number[121].preview_era_notes)

    def test_link_forms_and_product_hosts_leave_other_links_alone(self) -> None:
        for link in (
            "https://pr-8.site-preview.shinycomputers.com/contactus",
            "[Contact](https://pr-8.site-preview.shinycomputers.com/contactus)",
            "<https://pr-8.site-preview.shinycomputers.com>",
            "https://pr-8.rehearsals.example.net/page",
            "/ui/owner-review?pull_request=8&repository=example%2Fsite",
            "https://control.example.net/ui/owner-review?repository=example%2Fsite&pull_request=8",
        ):
            with self.subTest(link=link):
                self.assertEqual(
                    release_test_notes(link, preview_hosts=("pr-8.rehearsals.example.net",)),
                    ("Check this on the testing site.", True),
                )
        notes = (
            "On the preview, check Contact.\n"
            "[Testing](https://testing.example.net/contactus)\n"
            "[Guide](https://github.com/example/site/blob/main/README.md)\n"
            "https://control.example.net/ui/owner-review?product=example-site"
            "\n[Design](https://preview.design.example.net/mockup)"
        )
        self.assertEqual(release_test_notes(notes), (notes, False))


class RecordedPreviewNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = FilesystemRecordStore(Path(directory.name))
        seed(self.store)

    def test_retained_preview_host_is_used_and_display_changes_digest(self) -> None:
        self.store.write_preview_record(
            PreviewRecord(
                preview_id="old-preview",
                context="example-site",
                anchor_repo=_odoo_preview_anchor_repo(profile().repository),
                anchor_pr_number=42,
                anchor_pr_url="https://github.com/example/site/pull/42",
                preview_label="historical",
                canonical_url="https://review-42.example.net",
                state="destroyed",
                created_at="2026-10-01T00:00:00Z",
                updated_at="2026-10-08T00:00:00Z",
                eligible_at="2026-10-01T00:00:00Z",
            )
        )

        def read(path: str) -> object:
            result = github_read(path)
            if isinstance(result, list):
                result[0]["body"] = (
                    "## Client test notes\nOpen https://review-42.example.net/contactus"
                )
            return result

        review = build_release_review(
            store=cast(ReleaseReviewStore, self.store), profile=profile(), read=read
        )
        assert review.checklist is not None
        item = review.checklist.items[0]
        self.assertTrue(item.preview_era_notes)
        self.assertNotIn("review-42.example.net", item.owner_test_notes)
        original = review.checklist.model_copy(
            update={
                "items": (
                    item.model_copy(
                        update={
                            "owner_test_notes": "Open https://review-42.example.net/contactus",
                            "preview_era_notes": False,
                        }
                    ),
                )
            }
        )
        self.assertNotEqual(checklist_digest(original), review.checklist_digest)
        annotation = review.checklist.model_copy(
            update={
                "items": (
                    item.model_copy(
                        update={
                            "already_reviewed": True,
                        }
                    ),
                )
            }
        )
        self.assertEqual(checklist_digest(annotation), review.checklist_digest)

    def test_unaffected_item_keeps_historical_serialization_and_digest(self) -> None:
        review = build_release_review(
            store=cast(ReleaseReviewStore, self.store), profile=profile(), read=github_read
        )
        assert review.checklist is not None
        historical = review.checklist.model_dump(mode="json")
        self.assertNotIn("preview_era_notes", historical["items"][0])
        for item in historical["items"]:
            item.pop("already_reviewed")

        expected = hashlib.sha256(
            json.dumps(historical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(review.checklist_digest, expected)
