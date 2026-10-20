"""Pending request withdrawal never supplies a Client decision."""

from collections.abc import Iterator
from typing import cast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.product_review_withdrawal import (
    ReviewWithdrawalRequest,
    ReviewWithdrawalService,
    ReviewWithdrawalRefused,
)
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_product_review import _profile, _decision, _REPOSITORY, _PULL_REQUEST
from tests.support.stores import sqlite_database_url

HEAD = _decision(record_id="fixture-head", decided_at="2026-10-20T10:00:00Z").head_sha
REVIEW_LABEL = _profile().owner.review_label


class SourceControl:
    def __init__(self) -> None:
        self.head = HEAD
        self.labels = {REVIEW_LABEL, "keep-me"}
        self.comments: dict[str, str] = {}
        self.deletes = 0

    def change(self) -> tuple[str, frozenset[str]]:
        return self.head, frozenset(self.labels)

    def history(self, marker: str, body: str = "") -> str:
        if body:
            self.comments[marker] = body
        return (
            f"https://github.com/{_REPOSITORY}/pull/42#issuecomment-100"
            if marker in self.comments
            else ""
        )

    def remove_label(self, label: str) -> None:
        self.deletes += 1
        self.labels.remove(label)


class ReviewWithdrawalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary.name) / "state.db")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        self.source = SourceControl()
        self.service = ReviewWithdrawalService(
            self.store, self.source, self.profile, actor="director-agent:test"
        )
        self.request = ReviewWithdrawalRequest(
            product=self.profile.product,
            repository=_REPOSITORY,
            pull_request=_PULL_REQUEST,
            expected_head_sha=HEAD,
            reason="Director rerouted the change as engineering.",
            director_decision_url="https://github.com/example/direction/issues/1#issuecomment-20",
        )

    def apply(self) -> dict[str, object]:
        if not hasattr(self, "reviewed_request"):
            plan = self.service.run(self.request, key="", trace="plan")
            self.reviewed_request = self.request.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": plan["plan_sha256"]}
            )
        return self.service.run(self.reviewed_request, key="withdraw-1", trace="apply")

    def test_director_decision_withdraws_pending_request_and_readback(self) -> None:
        planned = self.service.run(self.request, key="", trace="plan")
        self.assertIn(REVIEW_LABEL, self.source.labels)
        self.assertFalse(self.source.comments)
        result = self.apply()
        self.assertTrue(result["applied"])
        self.assertNotIn(REVIEW_LABEL, self.source.labels)
        self.assertIn("keep-me", self.source.labels)
        history = next(iter(self.source.comments.values()))
        self.assertIn(self.request.reason, history)
        self.assertIn(self.request.director_decision_url, history)
        self.assertIn(HEAD, history)
        self.assertEqual(
            self.store.list_product_review_decision_records(
                repository=_REPOSITORY, pull_request_number=42
            ),
            (),
        )
        read = self.service.run(
            self.request.model_copy(
                update={"mode": "readback", "reviewed_plan_sha256": planned["plan_sha256"]}
            ),
            key="withdraw-1",
            trace="read",
        )
        self.assertTrue(read["readback_matches"])

    def test_withdrawal_refuses_after_either_client_decision_on_current_head(self) -> None:
        for outcome in ("accepted", "changes_requested"):
            with self.subTest(outcome=outcome):
                record = _decision(record_id="decision", decided_at="2026-10-20T10:00:00Z")
                record = ProductReviewDecisionRecord.model_validate(
                    record.model_dump() | {"decision": outcome, "reason": "Client decided"}
                )
                self.store.write_product_review_decision_record(record)
                with self.assertRaises(ReviewWithdrawalRefused):
                    self.apply()
                self.assertEqual(self.source.deletes, 0)
                self.assertFalse(self.source.comments)

    def test_later_review_request_and_decision_work_normally(self) -> None:
        self.apply()
        self.source.labels.add(REVIEW_LABEL)
        # Replaying the original operation must never withdraw a later request.
        replay = self.apply()
        self.assertTrue(replay["applied"])
        self.assertIn(REVIEW_LABEL, self.source.labels)
        self.assertEqual(self.source.deletes, 1)
        from control_plane.client_test_notes import change_client_review
        from control_plane.product_review import (
            record_product_review_decision,
            resolve_serving_preview,
        )
        from tests.test_product_review import _human, _OWNER_GITHUB_ID
        from tests.http_app_test_support import (
            _preview_read_record,
            _preview_generation_read_record,
        )

        intent = change_client_review(
            body="## Client test notes\nOpen the preview and inspect the changed layout.",
            labels=frozenset(self.source.labels),
            review_label=REVIEW_LABEL,
        )
        self.assertTrue(intent.review_required)
        self.store.write_preview_record(_preview_read_record(anchor_pr_number=42))
        self.store.write_preview_generation_record(
            _preview_generation_read_record(anchor_pr_number=42)
        )
        recorded = record_product_review_decision(
            store=self.store,
            profile=self.profile,
            pull_request_number=42,
            preview=resolve_serving_preview(
                store=self.store, profile=self.profile, pull_request_number=42
            ),
            decision="accepted",
            reason="Later review accepted",
            identity=_human(login="site-owner", github_id=_OWNER_GITHUB_ID),
        )
        self.assertEqual(recorded.head_sha, HEAD)
        with self.assertRaises(ReviewWithdrawalRefused):
            self.service.run(self.request, key="", trace="new-plan")

    def test_changed_head_and_missing_decision_citation_refuse(self) -> None:
        self.source.head = "b" * 40
        with self.assertRaises(ReviewWithdrawalRefused):
            self.apply()
        with self.assertRaises(ValueError):
            ReviewWithdrawalRequest.model_validate(
                self.request.model_dump() | {"director_decision_url": ""}
            )

    def test_decision_saved_after_dry_run_blocks_apply(self) -> None:
        plan = self.service.run(self.request, key="", trace="plan")
        self.store.write_product_review_decision_record(
            _decision(record_id="between", decided_at="2026-10-20T12:00:00Z")
        )
        with self.assertRaises(ReviewWithdrawalRefused):
            self.service.run(
                self.request.model_copy(
                    update={"mode": "apply", "reviewed_plan_sha256": plan["plan_sha256"]}
                ),
                key="late",
                trace="apply",
            )
        self.assertEqual(self.source.deletes, 0)

    def test_uncertain_delete_recovers_history_without_deleting_a_later_request(self) -> None:
        from unittest.mock import patch

        remove = self.source.remove_label

        def lost_response(label: str) -> None:
            remove(label)
            raise TimeoutError("lost provider response")

        with patch.object(self.source, "remove_label", side_effect=lost_response):
            with self.assertRaises(ReviewWithdrawalRefused):
                self.apply()
        self.assertEqual(self.source.deletes, 1)
        self.assertFalse(self.source.comments)
        self.assertTrue(self.apply()["applied"])
        self.assertEqual(self.source.deletes, 1)
        self.assertEqual(len(self.source.comments), 1)


class ReviewWithdrawalHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_director_agent_uses_standing_permission_and_scoped_agents_refuse(self) -> None:
        from contextlib import contextmanager
        from unittest.mock import patch
        from fastapi import FastAPI
        from control_plane.http_app import _launchplane_http_error, LaunchplaneErrorResponse
        from control_plane.http_routes.support import ReadRouteDependencies, ApiRouteRegistrar
        from control_plane.http_routes.product_review_withdrawal import (
            register_product_review_withdrawal_route,
        )
        from control_plane.service_auth import LocalOperatorIdentity, LaunchplaneAuthzPolicy
        from tests.support.http import request

        with TemporaryDirectory() as temporary:
            store = PostgresRecordStore(
                database_url=sqlite_database_url(Path(temporary) / "state.db")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            profile = _profile()
            store.write_product_profile_record(profile)
            source = SourceControl()
            identity = LocalOperatorIdentity("director", "existing-bootstrap", director_agent=True)
            app = FastAPI()
            policy = LaunchplaneAuthzPolicy(schema_version=3)
            register_product_review_withdrawal_route(
                cast(ApiRouteRegistrar, app),
                dependencies=ReadRouteDependencies(
                    read_identity=lambda: identity,
                    get_record_store=lambda: store,
                    next_trace_id=lambda: "trace-test",
                    authorization_allows=policy.allows,
                    http_error=_launchplane_http_error,
                    error_response_model=LaunchplaneErrorResponse,
                ),
                control_plane_root=Path(temporary),
            )

            @contextmanager
            def existing_credential(**kwargs: object) -> Iterator[str]:
                yield "test-token"

            payload = {
                "product": profile.product,
                "repository": profile.repository,
                "pull_request": 42,
                "expected_head_sha": HEAD,
                "reason": "Recorded engineering routing decision",
                "director_decision_url": "https://github.com/example/direction/issues/1#issuecomment-20",
            }
            with (
                patch(
                    "control_plane.http_routes.product_review_withdrawal.launchplane_github_token",
                    existing_credential,
                ),
                patch(
                    "control_plane.http_routes.product_review_withdrawal.GitHubReviewWithdrawalSource",
                    return_value=source,
                ),
                patch(
                    "control_plane.http_routes.product_review_withdrawal.resolve_delivery_github_app_id",
                    return_value=100,
                ),
            ):
                planned = await request(
                    app, "POST", "/v1/product-review/withdrawal", payload=payload
                )
                self.assertEqual(planned.status_code, 202, planned.text)
                apply = payload | {
                    "mode": "apply",
                    "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"],
                }
                result = await request(
                    app,
                    "POST",
                    "/v1/product-review/withdrawal",
                    payload=apply,
                    headers={"Idempotency-Key": "test-1"},
                )
                self.assertEqual(result.status_code, 202, result.text)
                self.assertTrue(result.json()["result"]["applied"])
                # A generic agent with no Director marker cannot attest a Director decision.
                identity = LocalOperatorIdentity("other-agent", "other-token")
                denied = await request(
                    app, "POST", "/v1/product-review/withdrawal", payload=payload
                )
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(denied.json()["detail"]["code"], "authorization_denied")


class ReviewWithdrawalGitHubTests(unittest.TestCase):
    def test_delivery_app_history_recovery_ignores_forged_markers(self) -> None:
        from unittest.mock import Mock
        from control_plane.product_review_withdrawal_github import GitHubReviewWithdrawalSource

        marker = "<!-- launchplane:review-withdrawal:test -->"
        body = marker + "\nSaved routing decision"
        forged = {"id": 11, "body": body, "user": {"id": 21}, "performed_via_github_app": {"id": 1}}
        genuine = {"id": 12, "body": body, "performed_via_github_app": {"id": 2}}
        api = Mock(side_effect=[[forged, genuine]])
        source = GitHubReviewWithdrawalSource(_REPOSITORY, 42, "test-token", api, app_id=2)
        self.assertTrue(source.history(marker).endswith("#issuecomment-12"))
        self.assertEqual(api.call_count, 1)
