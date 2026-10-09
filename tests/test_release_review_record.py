from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from collections.abc import Iterator
import select
import subprocess
import sys
import unittest
from unittest.mock import patch

from control_plane.release_review_record import (
    publish_release_decision,
    release_decision_issue_body,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_release_review import decision, profile, seed


def publication_lock_recovers_after_worker_exit(
    store: FilesystemRecordStore | PostgresRecordStore, root: Path
) -> None:
    worker = """
import sys
from pathlib import Path
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
store = (PostgresRecordStore(database_url=sys.argv[1]) if sys.argv[1]
         else FilesystemRecordStore(Path(sys.argv[2])))
with store.release_review_publication_lock(record_id="interrupted-publication"):
    print("locked", flush=True)
    sys.stdin.read()
"""
    database_url = store.database_url if isinstance(store, PostgresRecordStore) else ""
    # The first killed worker leaves no finally/rollback opportunity. The next
    # process must acquire the same lock without resetting a durable lease.
    for _ in range(2):
        process = subprocess.Popen(
            [sys.executable, "-c", worker, database_url, str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 10)
            if not readable or process.stdout.readline().strip() != "locked":
                raise AssertionError("Worker could not acquire release publication lock")
        finally:
            process.kill()
            process.communicate(timeout=10)


class FakeReleaseIssues:
    """Issue API with a controllable first lookup and a lost POST response."""

    def __init__(self) -> None:
        self.issues: list[dict[str, object]] = []
        self.lookup_started = Event()
        self.continue_lookup = Event()
        self.continue_lookup.set()
        self.lose_response = False
        self._lock = Lock()

    def request(
        self, *, path: str, token: str, method: str = "GET", body: dict[str, str] | None = None
    ) -> object:
        if method == "GET":
            with self._lock:
                snapshot = list(self.issues)
            self.lookup_started.set()
            if not self.continue_lookup.wait(timeout=10):
                raise TimeoutError("Test did not release issue lookup")
            return snapshot
        assert body is not None
        with self._lock:
            issue = {"number": 99 + len(self.issues), "body": body["body"]}
            self.issues.append(issue)
        if self.lose_response:
            self.lose_response = False
            raise ValueError("Issue created; response lost")
        return issue


def concurrent_publication(
    stores: tuple[FilesystemRecordStore | PostgresRecordStore, ...], root: Path
) -> tuple[tuple[str, ...], FakeReleaseIssues]:
    seed(stores[0])
    saved = decision(stores[0]).model_copy(update={"release_issue_url": ""})
    stores[0].create_release_review_decision_record_if_absent(saved)
    github = FakeReleaseIssues()
    github.continue_lookup.clear()
    contender_started = Event()
    contender_acquired = Event()
    original_lock = stores[1].release_review_publication_lock

    @contextmanager
    def contender_lock(*, record_id: str) -> Iterator[None]:
        contender_started.set()
        with original_lock(record_id=record_id):
            contender_acquired.set()
            yield

    with (
        patch("control_plane.release_review_record.github_api_request", side_effect=github.request),
        patch(
            "control_plane.release_review_record.resolve_launchplane_github_token",
            return_value="test-token",
        ),
        patch.object(stores[1], "release_review_publication_lock", contender_lock),
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        first = workers.submit(
            publish_release_decision,
            store=stores[0],
            control_plane_root=root,
            profile=profile(),
            decision=saved,
        )
        try:
            if not github.lookup_started.wait(timeout=10):
                raise TimeoutError("First publisher did not reach lookup")
            second = workers.submit(
                publish_release_decision,
                store=stores[1],
                control_plane_root=root,
                profile=profile(),
                decision=saved,
            )
            if not contender_started.wait(timeout=10):
                raise TimeoutError("Second publisher did not attempt serialization")
            if contender_acquired.wait(timeout=0.5):
                raise AssertionError("Contender acquired publication lock during the first lookup")
        finally:
            github.continue_lookup.set()
        urls = (first.result(timeout=10), second.result(timeout=10))
    return urls, github


class ReleaseReviewRecordTests(unittest.TestCase):
    store: FilesystemRecordStore | PostgresRecordStore

    def test_publication_lock_recovers_after_worker_exit(self) -> None:
        publication_lock_recovers_after_worker_exit(self.store, self.root)

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = store = FilesystemRecordStore(self.root)
        seed(store)
        self.decision = decision(store).model_copy(update={"release_issue_url": ""})
        store.create_release_review_decision_record_if_absent(self.decision)
        token = patch(
            "control_plane.release_review_record.resolve_launchplane_github_token",
            return_value="test-token",
        )
        token.start()
        self.addCleanup(token.stop)

    def test_concurrent_publishers_return_one_stored_issue(self) -> None:
        urls, github = concurrent_publication(
            (self.store, FilesystemRecordStore(self.root)), self.root
        )
        self.assertEqual(len(github.issues), 1)
        self.assertEqual(urls[0], urls[1])
        stored = self.store.list_release_review_decision_records(product=self.decision.product)[0]
        self.assertEqual(stored.release_issue_url, urls[0])
        self.assertEqual(stored.model_copy(update={"release_issue_url": ""}), self.decision)

    def test_lost_response_or_storage_acknowledgement_recovers_one_issue(self) -> None:
        for failure in ("response", "storage"):
            with self.subTest(failure=failure):
                saved = self.decision.model_copy(update={"record_id": f"lost-{failure}"})
                self.store.create_release_review_decision_record_if_absent(saved)
                github = FakeReleaseIssues()
                github.lose_response = failure == "response"
                with patch(
                    "control_plane.release_review_record.github_api_request",
                    side_effect=github.request,
                ):
                    if failure == "storage":
                        with patch.object(
                            self.store,
                            "record_release_review_decision_publication",
                            side_effect=ValueError("Storage acknowledgement lost"),
                        ):
                            with self.assertRaises(ValueError):
                                publish_release_decision(
                                    store=self.store,
                                    control_plane_root=self.root,
                                    profile=profile(),
                                    decision=saved,
                                )
                    else:
                        with self.assertRaises(ValueError):
                            publish_release_decision(
                                store=self.store,
                                control_plane_root=self.root,
                                profile=profile(),
                                decision=saved,
                            )
                    url = publish_release_decision(
                        store=self.store,
                        control_plane_root=self.root,
                        profile=profile(),
                        decision=saved,
                    )
                self.assertEqual(len(github.issues), 1)
                self.assertEqual(url, "https://github.com/example/site/issues/99")

    def test_missing_or_changed_saved_decision_has_no_external_effect(self) -> None:
        cases = (
            self.decision.model_copy(update={"record_id": "missing"}),
            self.decision.model_copy(update={"decision": "changes_requested", "reason": "Changed"}),
        )
        with patch("control_plane.release_review_record.github_api_request") as api:
            for saved in cases:
                with self.subTest(record=saved), self.assertRaises((FileNotFoundError, ValueError)):
                    publish_release_decision(
                        store=self.store,
                        control_plane_root=self.root,
                        profile=profile(),
                        decision=saved,
                    )
            api.assert_not_called()

    def test_stale_snapshot_reuses_stored_url_without_source_control_access(self) -> None:
        self.store.record_release_review_decision_publication(
            record_id=self.decision.record_id,
            release_issue_url="https://github.com/example/site/issues/99",
        )
        with (
            patch("control_plane.release_review_record.github_api_request") as api,
            patch("control_plane.release_review_record.resolve_launchplane_github_token") as token,
        ):
            url = publish_release_decision(
                store=self.store,
                control_plane_root=self.root,
                profile=profile(),
                decision=self.decision,
            )
        self.assertEqual(url, "https://github.com/example/site/issues/99")
        api.assert_not_called()
        token.assert_not_called()

    def test_publishes_full_checklist_and_decision_to_tenant_repository(self) -> None:
        with patch(
            "control_plane.release_review_record.github_api_request",
            side_effect=[[], {"number": 99}, None],
        ) as api:
            url = publish_release_decision(
                store=self.store,
                control_plane_root=self.root,
                profile=profile(),
                decision=self.decision,
            )
        self.assertEqual(url, "https://github.com/example/site/issues/99")
        write = next(
            call.kwargs for call in api.call_args_list if call.kwargs.get("method") == "POST"
        )
        self.assertEqual(write["method"], "POST")
        self.assertEqual(write["path"], "/repos/example/site/issues")
        body = write["body"]["body"]
        self.assertIn("Check the repair prices.", body)
        self.assertIn(self.decision.checklist_digest, body)
        self.assertIn(self.decision.checklist.production.source_commit, body)
        self.assertIn(self.decision.checklist.candidate.source_commit, body)
        self.assertIn("site-owner", body)
        self.assertIn("## Client checklist", body)
        self.assertNotIn("Owner", body)

    def test_recovers_successful_issue_write_without_a_duplicate(self) -> None:
        with patch(
            "control_plane.release_review_record.github_api_request",
            return_value=[{"number": 99, "body": release_decision_issue_body(self.decision)}],
        ) as api:
            url = publish_release_decision(
                store=self.store,
                control_plane_root=self.root,
                profile=profile(),
                decision=self.decision,
            )
        self.assertEqual(url, "https://github.com/example/site/issues/99")
        self.assertEqual(
            [call.kwargs.get("method", "GET") for call in api.call_args_list], ["GET", "DELETE"]
        )
        self.assertNotIn("method", api.call_args_list[0].kwargs)

    def test_recovers_a_record_written_with_older_wording(self) -> None:
        older_body = (
            release_decision_issue_body(self.decision)
            .replace("## Client checklist", "## Owner checklist")
            .replace("\n", "\r\n")
        )
        with patch(
            "control_plane.release_review_record.github_api_request",
            return_value=[{"number": 99, "body": older_body}],
        ) as api:
            url = publish_release_decision(
                store=self.store,
                control_plane_root=self.root,
                profile=profile(),
                decision=self.decision,
            )
        self.assertEqual(url, "https://github.com/example/site/issues/99")
        self.assertEqual(
            [call.kwargs.get("method", "GET") for call in api.call_args_list], ["GET", "DELETE"]
        )

    def test_only_the_same_record_marker_on_the_first_line_recovers(self) -> None:
        body = release_decision_issue_body(self.decision)
        other = release_decision_issue_body(
            self.decision.model_copy(update={"record_id": "release-review-other"})
        )
        issues = [
            {"number": 97, "body": other},
            {"number": 98, "body": f"Quoting the record:\n\n{body}"},
            {"number": 99, "body": body, "pull_request": {}},
            {"number": 100, "body": None},
        ]
        with patch(
            "control_plane.release_review_record.github_api_request",
            side_effect=[issues, {"number": 101}],
        ) as api:
            url = publish_release_decision(
                store=self.store,
                control_plane_root=self.root,
                profile=profile(),
                decision=self.decision,
            )
        self.assertEqual(url, "https://github.com/example/site/issues/101")
        self.assertTrue(any(call.kwargs.get("method") == "POST" for call in api.call_args_list))

    def test_incomplete_lookup_or_create_never_claims_a_release_record(self) -> None:
        cases: tuple[list[object], ...] = (
            [{}],
            [[], {}],
            [[{"number": "bad", "body": release_decision_issue_body(self.decision)}]],
        )
        for responses in cases:
            with (
                self.subTest(responses=responses),
                patch(
                    "control_plane.release_review_record.github_api_request", side_effect=responses
                ),
                self.assertRaises(ValueError),
            ):
                publish_release_decision(
                    store=self.store,
                    control_plane_root=self.root,
                    profile=profile(),
                    decision=self.decision,
                )


class SqliteReleaseReviewRecordTests(ReleaseReviewRecordTests):
    store: PostgresRecordStore

    def setUp(self) -> None:
        super().setUp()
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        seed(self.store)
        self.store.create_release_review_decision_record_if_absent(self.decision)

    def test_concurrent_publishers_return_one_stored_issue(self) -> None:
        contender = PostgresRecordStore(database_url=self.store.database_url)
        self.addCleanup(contender.close)
        urls, github = concurrent_publication((self.store, contender), self.root)
        self.assertEqual(len(github.issues), 1)
        self.assertEqual(urls[0], urls[1])
        stored = self.store.list_release_review_decision_records(product=self.decision.product)[0]
        self.assertEqual(stored.release_issue_url, urls[0])
