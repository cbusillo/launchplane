import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.discussion_topic import DiscussionTopicInput
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import sqlite_database_url


class DiscussionTopicTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "topics.db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.topic = DiscussionTopicInput(
            title="Decide: Contact links", issue_url="https://git.example.test/team/site/issues/7"
        )

    def test_add_close_and_retry_preserve_history_and_product_scope(self) -> None:
        first = self.store.add_discussion_topic("site-a", "contact", self.topic)
        self.assertTrue(first.added_at)
        self.assertIsNone(first.closed_at)
        self.assertEqual(self.store.add_discussion_topic("site-a", "contact", self.topic), first)
        self.store.add_discussion_topic("site-b", "contact", self.topic)
        closed = self.store.close_discussion_topic("site-a", "contact")
        self.assertEqual(closed.added_at, first.added_at)
        self.assertIsNotNone(closed.closed_at)
        self.assertEqual(self.store.close_discussion_topic("site-a", "contact"), closed)
        self.assertEqual(self.store.add_discussion_topic("site-a", "contact", self.topic), closed)
        self.assertEqual(self.store.list_discussion_topics("site-a"), ())
        self.assertEqual(
            self.store.list_discussion_topics("site-a", include_closed=True), (closed,)
        )
        self.assertEqual(len(self.store.list_discussion_topics("site-b")), 1)
        with self.assertRaises(FileNotFoundError):
            self.store.close_discussion_topic("site-c", "contact")

    def test_conflicting_add_cannot_overwrite_or_reopen(self) -> None:
        first = self.store.add_discussion_topic("site-a", "contact", self.topic)
        other = self.topic.model_copy(update={"title": "Decide: Different question"})
        with self.assertRaises(ValueError):
            self.store.add_discussion_topic("site-a", "contact", other)
        self.assertEqual(self.store.list_discussion_topics("site-a"), (first,))

    def test_added_order_crosses_whole_second_without_reordering_topics(self) -> None:
        first_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch("control_plane.storage.postgres.datetime") as clock:
            clock.now.side_effect = [first_time, first_time + timedelta(microseconds=1)]
            first = self.store.add_discussion_topic("site-a", "first", self.topic)
            second = self.store.add_discussion_topic("site-a", "second", self.topic)
        self.assertEqual(self.store.list_discussion_topics("site-a"), (first, second))

    def test_concurrent_retries_create_one_record_and_one_close_time(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            added = list(
                pool.map(
                    lambda _: self.store.add_discussion_topic("site-a", "contact", self.topic),
                    range(8),
                )
            )
            closed = list(
                pool.map(lambda _: self.store.close_discussion_topic("site-a", "contact"), range(8))
            )
        self.assertTrue(all(record == added[0] for record in added))
        self.assertTrue(all(record == closed[0] for record in closed))

    def test_rejects_private_context_fields_unsafe_links_and_non_neutral_titles(self) -> None:
        for changes in (
            {"private_reason": "private context"},
            {"title": ""},
            {"title": "Contact links"},
            {"title": "Decide: Contact\nlinks"},
            {"issue_url": "javascript:alert(1)"},
            {"issue_url": "https://user:password@example.test/issue/7"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                DiscussionTopicInput.model_validate({**self.topic.model_dump(), **changes})
