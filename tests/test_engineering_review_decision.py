from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.contracts.engineering_review_decision import (
    EngineeringReviewDecisionRecord,
    EngineeringReviewDecisionRequest,
)
from control_plane.contracts.engineering_review_run import EngineeringReviewRunRecord
from control_plane.contracts.engineering_review_run import (
    EngineeringReviewAuthorityRecord,
    EngineeringReviewPullRequestTarget,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.engineering_review_decision_projection import (
    project_engineering_review_decision,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.engineering_review_decision_service import (
    evaluate_engineering_review_decision,
)
from control_plane.engineering_review_service import create_engineering_review_runs
from tests.test_change_impact import (
    HEAD_SHA,
    REPOSITORY,
    TREE_SHA,
    _policy,
    _repository_evidence,
)
from tests.test_engineering_review_run import _pending_record
from tests.test_engineering_review_run import _authority


class _Provider:
    def __init__(self, *paths: str) -> None:
        self.evidence = _repository_evidence(*paths)

    def resolve(self, _target: ChangeImpactTargetReference):  # type: ignore[no-untyped-def]
        return self.evidence


def _shared_authority() -> EngineeringReviewAuthorityRecord:
    base_authority = _authority()
    return EngineeringReviewAuthorityRecord.model_validate(
        {
            **base_authority.model_dump(mode="json", exclude={"authority_id", "authority_digest"}),
            "repository": REPOSITORY,
        }
    )


class _Store:
    def __init__(self, runs: tuple[EngineeringReviewRunRecord, ...] = ()) -> None:
        self.runs = tuple(runs)
        self.records: dict[str, EngineeringReviewDecisionRecord] = {}
        self.work_request = EveryCodeWorkRequestRecord(
            request_id="work-request-20",
            lifecycle_id="lifecycle-20",
            source="manual",
            state="done",
            repository=REPOSITORY,
            issue_number=20,
            issue_url=f"https://github.com/{REPOSITORY}/issues/20",
            trigger_label="every-code",
            queued_at="2026-08-06T00:00:00Z",
            updated_at="2026-08-06T00:00:00Z",
            claimed_at="2026-08-06T00:00:00Z",
            claimed_by_host="controlled-worker.example.test",
            started_at="2026-08-06T00:00:00Z",
            finished_at="2026-08-06T00:00:00Z",
            result_pr_url=f"https://github.com/{REPOSITORY}/pull/20",
        )
        self.authority = _shared_authority()

    def read_every_code_work_request_record(self, request_id: str):  # type: ignore[no-untyped-def]
        if request_id != self.work_request.request_id:
            raise FileNotFoundError(request_id)
        return self.work_request

    def list_change_impact_policy_records(self, **_filters):  # type: ignore[no-untyped-def]
        return (_policy(),)

    def list_change_impact_stored_evidence(self, **_filters):  # type: ignore[no-untyped-def]
        return ()

    def list_engineering_review_run_records(self, **_filters):  # type: ignore[no-untyped-def]
        return self.runs

    def list_engineering_review_authority_records(self, **_filters):  # type: ignore[no-untyped-def]
        return (self.authority,)

    def create_engineering_review_run_records_if_absent(
        self,
        records: tuple[EngineeringReviewRunRecord, ...],
        **_expected: object,
    ) -> tuple[tuple[EngineeringReviewRunRecord, ...], bool]:
        self.runs = tuple(records)
        return self.runs, True

    def write_engineering_review_decision_record_if_absent(self, record):  # type: ignore[no-untyped-def]
        existing = self.records.get(record.decision_id)
        if existing is not None:
            return existing, False
        self.records[record.decision_id] = record
        return record, True

    def read_engineering_review_decision_record(self, decision_id: str):  # type: ignore[no-untyped-def]
        try:
            return self.records[decision_id]
        except KeyError as error:
            raise FileNotFoundError(decision_id) from error

    def list_engineering_review_decision_records(self, **_filters):  # type: ignore[no-untyped-def]
        return tuple(reversed(tuple(self.records.values())))


def _completed_run(
    *, slot: int, family: str, decision: str = "approved"
) -> EngineeringReviewRunRecord:
    pending, _credential = _pending_record()
    authority = _shared_authority()
    return pending.model_copy(
        update={
            "run_id": f"review-run-{slot}-{family}",
            "review_slot": slot,
            "state": "completed",
            "repository": REPOSITORY,
            "pr_number": 20,
            "head_sha": HEAD_SHA,
            "tree_sha": TREE_SHA,
            "work_request_id": "work-request-20",
            "work_request_lifecycle_id": "lifecycle-20",
            "authority_id": authority.authority_id,
            "authority_digest": authority.authority_digest,
            "policy_revision": authority.policy_revision,
            "model_family": family,
            "decision": decision,
            "finished_at": "2026-08-06T00:10:00Z",
        }
    )


def _request() -> EngineeringReviewDecisionRequest:
    return EngineeringReviewDecisionRequest(
        target=ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=20),
        work_request_id="work-request-20",
    )


class EngineeringReviewDecisionTests(unittest.TestCase):
    def test_server_resolved_target_must_match_work_request_pr(self) -> None:
        store = _Store((_completed_run(slot=1, family="openai"),))
        provider = _Provider(".github/workflows/ci.yml")
        provider.evidence = provider.evidence.model_copy(
            update={
                "target": provider.evidence.target.model_copy(
                    update={"repository": "other/repository"}
                )
            }
        )

        with self.assertRaisesRegex(ValueError, "Server-resolved"):
            evaluate_engineering_review_decision(
                store=store,
                request=_request(),
                repository_evidence_provider=provider,
            )

    def test_work_request_result_pr_repository_must_match_target(self) -> None:
        store = _Store((_completed_run(slot=1, family="openai"),))
        store.work_request = store.work_request.model_copy(
            update={"result_pr_url": "https://github.com/other/repository/pull/20"}
        )

        with self.assertRaisesRegex(ValueError, "pull request"):
            evaluate_engineering_review_decision(
                store=store,
                request=_request(),
                repository_evidence_provider=_Provider(".github/workflows/ci.yml"),
            )

    def test_routine_classification_schedules_only_one_review_run(self) -> None:
        store = _Store()
        target = EngineeringReviewPullRequestTarget(
            repository=REPOSITORY,
            pr_number=20,
            pr_url=f"https://github.com/{REPOSITORY}/pull/20",
            head_sha=HEAD_SHA,
            tree_sha=TREE_SHA,
        )
        with patch(
            "control_plane.engineering_review_service.control_plane_secrets._encrypt_secret_value",
            return_value=("ciphertext", "key-id"),
        ):
            result = create_engineering_review_runs(
                store=store,
                work_request_id=store.work_request.request_id,
                target_resolver=lambda _work_request: target,
                repository_evidence_provider=_Provider(".github/workflows/ci.yml"),
                created_at="2026-08-06T00:05:00Z",
            )

        self.assertEqual(len(result.runs), 1)
        self.assertEqual(result.runs[0].review_slot, 1)

    def test_routine_change_requires_one_exact_approved_run(self) -> None:
        store = _Store((_completed_run(slot=1, family="openai"),))

        record, created = evaluate_engineering_review_decision(
            store=store,
            request=_request(),
            repository_evidence_provider=_Provider(".github/workflows/ci.yml"),
            evaluated_at="2026-08-06T00:20:00Z",
        )

        self.assertTrue(created)
        self.assertEqual(record.status, "approved")
        self.assertEqual(record.required_review_count, 1)
        self.assertEqual(record.qualifying_run_ids, ("review-run-1-openai",))

    def test_sensitive_change_requires_two_model_families(self) -> None:
        store = _Store((_completed_run(slot=1, family="openai"),))

        record, _created = evaluate_engineering_review_decision(
            store=store,
            request=_request(),
            repository_evidence_provider=_Provider("control_plane/billing/policy.py"),
        )
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.required_review_count, 2)

        store.runs = (
            _completed_run(slot=1, family="openai"),
            _completed_run(slot=2, family="anthropic"),
        )
        approved, _created = evaluate_engineering_review_decision(
            store=store,
            request=_request(),
            repository_evidence_provider=_Provider("control_plane/billing/policy.py"),
        )
        self.assertEqual(approved.status, "approved")
        self.assertEqual(approved.qualifying_model_families, ("anthropic", "openai"))

    def test_blocking_result_wins_and_stale_run_is_ignored(self) -> None:
        stale = _completed_run(slot=1, family="openai").model_copy(update={"head_sha": "c" * 40})
        blocked = _completed_run(slot=2, family="anthropic", decision="blocked")
        store = _Store((stale, blocked))

        record, _created = evaluate_engineering_review_decision(
            store=store,
            request=_request(),
            repository_evidence_provider=_Provider(".github/workflows/ci.yml"),
        )

        self.assertEqual(record.status, "blocked")
        self.assertEqual(record.qualifying_run_ids, ())

    def test_identical_evidence_replays_despite_new_evaluation_time(self) -> None:
        store = _Store((_completed_run(slot=1, family="openai"),))
        first, first_created = evaluate_engineering_review_decision(
            store=store,
            request=_request(),
            repository_evidence_provider=_Provider(".github/workflows/ci.yml"),
            evaluated_at="2026-08-06T00:20:00Z",
        )
        second, second_created = evaluate_engineering_review_decision(
            store=store,
            request=_request(),
            repository_evidence_provider=_Provider(".github/workflows/ci.yml"),
            evaluated_at="2026-08-06T00:21:00Z",
        )
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.decision_id, second.decision_id)

    def test_projection_is_idempotent_and_advisory_named(self) -> None:
        store = _Store((_completed_run(slot=1, family="openai"),))
        record, _created = evaluate_engineering_review_decision(
            store=store,
            request=_request(),
            repository_evidence_provider=_Provider(".github/workflows/ci.yml"),
        )
        calls: list[dict[str, Any]] = []

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            if kwargs.get("method") == "POST":
                body = kwargs["body"]
                return {
                    "id": 91,
                    "name": body["name"],
                    "head_sha": body["head_sha"],
                    "status": body["status"],
                    "conclusion": body["conclusion"],
                    "external_id": body["external_id"],
                    "details_url": body["details_url"],
                    "output": body["output"],
                    "app": {"id": 42},
                }
            return {"check_runs": []}

        result = project_engineering_review_decision(
            record=record,
            installation_token=GitHubAppInstallationToken(
                token="token",
                app_id=42,
                installation_id=43,
                repository_id=int(record.target.repository_id),
                repository=record.target.repository,
                expires_at="2026-08-07T15:00:00Z",
            ),
            api_request=api_request,
        )
        self.assertEqual(result.status, "projected")
        self.assertEqual(calls[-1]["body"]["name"], "launchplane/engineering-review")
        self.assertEqual(calls[-1]["body"]["conclusion"], "neutral")


if __name__ == "__main__":
    unittest.main()
