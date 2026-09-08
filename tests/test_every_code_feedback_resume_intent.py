from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import unittest
from typing import Any
from control_plane.contracts.every_code_feedback_resume import (
    EveryCodeFeedbackPullRequestOpenObservation,
)
from control_plane.every_code_feedback_resume_intent import EveryCodeFeedbackIntentMintResult

from pydantic import ValidationError

from control_plane.contracts.every_code_feedback_resume import (
    EveryCodeFeedbackResumeIntentRecord,
    parse_every_code_feedback_timestamp,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.every_code_feedback_open_observation import _verified_open_observation
from control_plane.every_code_feedback_resume_intent import decide_every_code_feedback_resume_intent
from tests.test_every_code_feedback_resume_storage import _acceptance, _intent, T0, T1


def request_fixture() -> EveryCodeWorkRequestRecord:
    acceptance = _acceptance()
    return EveryCodeWorkRequestRecord(
        request_id=acceptance.request_id,
        lifecycle_id="lifecycle-1",
        source="manual",
        state="done",
        repository=acceptance.revision.repository,
        issue_number=acceptance.issue_number,
        issue_url=acceptance.issue_url,
        trigger_label="feedback-test",
        queued_at=T0,
        updated_at=T1,
        claimed_at=T0,
        started_at=T0,
        finished_at=T1,
        claimed_by_host="worker-1",
        fencing_token=2,
        result_pr_url=acceptance.retained_pull_request_url,
    )


def observation_fixture(
    observed_at: str = T1,
) -> EveryCodeFeedbackPullRequestOpenObservation | None:
    return _verified_open_observation(
        repository={"id": 34, "owner": {"id": 12}},
        pull_request={
            "number": 1,
            "node_id": "PR_node",
            "state": "open",
            "merged": False,
            "base": {"repo": {"id": 34, "owner": {"id": 12}}},
        },
        observed_at=observed_at,
    )


def minted_fixture() -> EveryCodeFeedbackResumeIntentRecord:
    acceptance = _acceptance()
    return EveryCodeFeedbackResumeIntentRecord.model_validate(
        {
            **_intent(acceptance).model_dump(),
            "schema_version": 2,
            "intent_digest": "",
            "issued_at": T1,
            "issuance_policy": acceptance.policy,
            "open_observation": observation_fixture(),
        }
    )


class FeedbackResumeIntentTests(unittest.TestCase):
    def decision(self, **changes: Any) -> EveryCodeFeedbackIntentMintResult:
        acceptance = _acceptance()
        values: dict[str, Any] = dict(
            acceptance=acceptance,
            request=request_fixture(),
            current_acceptance=acceptance,
            closure_present=False,
            open_observation=observation_fixture(),
            current_policy_provenance=acceptance.policy,
            database_now=T1,
            existing_intent=None,
        )
        values.update(changes)
        return decide_every_code_feedback_resume_intent(**values)

    def test_terminal_mint_and_fail_closed_boundaries(self) -> None:
        self.assertEqual(self.decision().status, "mint")
        cases: list[tuple[dict[str, Any], str]] = [
            ({"open_observation": None}, "pull_request_state_unknown"),
            ({"closure_present": True}, "pull_request_closed"),
            ({"current_policy_provenance": None}, "authority_denied"),
            ({"database_now": _acceptance().eligible_until}, "acceptance_expired"),
            (
                {"request": request_fixture().model_copy(update={"state": "running"})},
                "request_not_terminal",
            ),
            (
                {"request": request_fixture().model_copy(update={"repository": "other/repo"})},
                "binding_mismatch",
            ),
            ({"current_acceptance": _acceptance(acceptance_id="new")}, "acceptance_superseded"),
        ]
        for changes, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.decision(**changes).status, expected)
        blocked = request_fixture().model_copy(
            update={"state": "blocked", "error_message": "stopped"}
        )
        self.assertEqual(self.decision(request=blocked).status, "mint")

    def test_freshness_inclusive_edges_and_skew(self) -> None:
        now = parse_every_code_feedback_timestamp(T1)
        for age, expected in [
            (30, "mint"),
            (30.000001, "pull_request_observation_stale"),
            (-5, "mint"),
            (-5.000001, "pull_request_observation_stale"),
        ]:
            observed = (
                (now - timedelta(seconds=age))
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            with self.subTest(age=age):
                self.assertEqual(
                    self.decision(open_observation=observation_fixture(observed)).status, expected
                )
        with self.assertRaises(ValidationError):
            observation_fixture("2026-09-07T12:01:00")

    def test_replay_is_exact_without_claiming_current_open(self) -> None:
        intent = minted_fixture()
        result = self.decision(existing_intent=intent, open_observation=None)
        self.assertEqual(result.status, "replay")
        self.assertIs(result.record, intent)
        self.assertEqual(
            self.decision(existing_intent=intent, closure_present=True).status,
            "pull_request_closed",
        )
        self.assertEqual(
            self.decision(existing_intent=intent, current_policy_provenance=None).status,
            "authority_denied",
        )
        changed = request_fixture().model_copy(
            update={"state": "blocked", "error_message": "stopped"}
        )
        self.assertEqual(
            self.decision(existing_intent=intent, request=changed).status,
            "terminal_snapshot_mismatch",
        )
        self.assertEqual(
            self.decision(existing_intent=_intent(_acceptance())).status, "legacy_unverified"
        )

    def test_v1_digest_survives_and_v2_requires_complete_proof(self) -> None:
        legacy = _intent(_acceptance())
        payload = legacy.model_dump(exclude={"issuance_policy", "open_observation"})
        self.assertEqual(
            EveryCodeFeedbackResumeIntentRecord.model_validate(payload).intent_digest,
            legacy.intent_digest,
        )
        for field in ("issuance_policy", "open_observation"):
            invalid = minted_fixture().model_dump()
            invalid[field] = None
            with self.assertRaises(ValidationError):
                EveryCodeFeedbackResumeIntentRecord.model_validate(invalid)
        minted = minted_fixture()
        changed = minted.model_dump()
        changed["issuance_policy"]["policy_revision"] += 1
        changed["intent_digest"] = ""
        self.assertNotEqual(
            EveryCodeFeedbackResumeIntentRecord.model_validate(changed).intent_digest,
            minted.intent_digest,
        )

    def test_no_route_consumes_open_observations_or_fixture_writer(self) -> None:
        root = Path(__file__).resolve().parents[1] / "control_plane"
        for path in (root / "http_routes").glob("*.py"):
            source = path.read_text()
            self.assertNotIn("EveryCodeFeedbackPullRequestOpenObservation", source, path.name)
            self.assertNotIn(
                "_write_every_code_feedback_resume_intent_fixture_record", source, path.name
            )

    def test_canonical_factory_rejects_closed_or_repository_mismatch(self) -> None:
        repo = {"id": 34, "owner": {"id": 12}}
        pr = {
            "number": 1,
            "node_id": "PR_node",
            "state": "closed",
            "merged": False,
            "base": {"repo": {"id": 34, "owner": {"id": 12}}},
        }
        self.assertIsNone(
            _verified_open_observation(repository=repo, pull_request=pr, observed_at=T1)
        )
        pr["state"] = "open"
        self.assertIsNone(
            _verified_open_observation(
                repository={"id": 35, "owner": {"id": 12}}, pull_request=pr, observed_at=T1
            )
        )
