from __future__ import annotations

from tempfile import TemporaryDirectory
from datetime import datetime, timedelta, timezone
import unittest

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.every_code_feedback_resume import (
    every_code_feedback_eligible_until,
)
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_every_code_feedback_resume_intent import observation_fixture, request_fixture
from tests.test_every_code_feedback_resume_storage import _acceptance


class EveryCodeFeedbackResumeIntentSqliteTests(unittest.TestCase):
    def test_sqlite_mint_and_replay_portability(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{temporary_directory}/records.db"
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            now = (
                (datetime.now(timezone.utc) - timedelta(seconds=60))
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            original = _acceptance()
            revision = original.revision.model_dump()
            revision.update(provider_updated_at=now, revision_digest="")
            acceptance = type(original).model_validate(
                {
                    **original.model_dump(),
                    "revision": revision,
                    "received_at": now,
                    "created_at": now,
                    "eligible_until": every_code_feedback_eligible_until(
                        first_received_at=now, provider_updated_at=now
                    ),
                    "acceptance_digest": "",
                }
            )
            request = request_fixture()
            store.write_every_code_work_request_record(request)
            store.write_every_code_feedback_acceptance_record(acceptance)
            store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="policy-sqlite-mint",
                    source="test",
                    updated_at=now,
                    policy=LaunchplaneAuthzPolicy(
                        schema_version=2,
                        github_humans=(
                            GitHubHumanPolicyRule(
                                managed_set_id="feedback-test",
                                managed_rule_id="human",
                                github_ids=(90,),
                                products=("launchplane",),
                                contexts=("launchplane",),
                                actions=("every_code_feedback_resume.request",),
                                instances=("github-repository:34",),
                            ),
                        ),
                    ),
                )
            )

            first = store.mint_every_code_feedback_resume_intent(
                acceptance_id=acceptance.acceptance_id,
                open_observation=observation_fixture(
                    datetime.now(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                ),
            )
            replay = store.mint_every_code_feedback_resume_intent(
                acceptance_id=acceptance.acceptance_id,
                open_observation=None,
            )

            self.assertEqual((first.status, replay.status), ("mint", "replay"))
            self.assertEqual(replay.record, first.record)
            self.assertEqual(
                store.list_every_code_feedback_resume_intent_records(request_id=request.request_id),
                (first.record,),
            )


if __name__ == "__main__":
    unittest.main()
