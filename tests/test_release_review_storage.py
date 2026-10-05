"""Acceptance creation and publication preserve the authoritative decision."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_release_review import decision, seed


def standing_decision(store: PostgresRecordStore) -> ReleaseReviewDecisionRecord:
    seed(store)
    return decision(store).model_copy(
        update={
            "release_issue_url": "",
            "release_start": "promote",
            "acceptance_source": "director_standing",
        }
    )


class ReleaseReviewStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'state.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.decision = standing_decision(self.store)

    def test_create_conflict_returns_published_winner_and_preserves_ordering(self) -> None:
        self.store.create_release_review_decision_record_if_absent(self.decision)
        published = self.store.record_release_review_decision_publication(
            record_id=self.decision.record_id,
            release_issue_url="https://github.com/example/site/issues/99",
        )
        newer = self.decision.model_copy(
            update={"record_id": "newer-decision", "decided_at": "2026-09-23T02:00:00Z"}
        )
        self.store.write_release_review_decision_record(newer)
        contender = self.decision.model_copy(
            update={"decided_at": "2026-09-23T03:00:00Z", "actor_github_login": "renamed-client"}
        )
        self.assertEqual(
            self.store.create_release_review_decision_record_if_absent(contender), published
        )
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.decision.product),
            (newer, published),
        )

    def test_stale_publication_cannot_replace_authoritative_url_or_audit(self) -> None:
        self.store.create_release_review_decision_record_if_absent(self.decision)
        first = self.store.record_release_review_decision_publication(
            record_id=self.decision.record_id,
            release_issue_url="https://github.com/example/site/issues/99",
        )
        stale = self.store.record_release_review_decision_publication(
            record_id=self.decision.record_id,
            release_issue_url="https://github.com/example/site/issues/100",
        )
        self.assertEqual(stale, first)
        self.assertEqual(first.model_copy(update={"release_issue_url": ""}), self.decision)
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.decision.product), (first,)
        )

    def test_publication_is_existing_only_and_cannot_erase_url(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.store.record_release_review_decision_publication(
                record_id=self.decision.record_id,
                release_issue_url="https://github.com/example/site/issues/99",
            )
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.decision.product), ()
        )
        self.store.create_release_review_decision_record_if_absent(self.decision)
        published = self.store.record_release_review_decision_publication(
            record_id=self.decision.record_id,
            release_issue_url="https://github.com/example/site/issues/99",
        )
        with self.assertRaises(ValueError):
            self.store.record_release_review_decision_publication(
                record_id=self.decision.record_id, release_issue_url=""
            )
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.decision.product),
            (published,),
        )
