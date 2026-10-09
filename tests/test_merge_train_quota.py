from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from io import BytesIO
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.merge_train_policy import MergeTrainSchedulerPolicy
from control_plane.merge_train_admission import (
    build_merge_train_controller_status_read_model,
    evaluate_merge_train_admission_from_store,
)
from control_plane.merge_train_scheduler import (
    MergeTrainScheduledTargetResult,
    run_merge_train_scheduler_pass,
)
from control_plane.merge_train_controller_run_once import (
    _controller_exception_reconciliation_detail,
)
from control_plane.merge_train_github import MergeTrainGitHubError, UrllibMergeTrainGitHubTransport
from tests.test_merge_train_admission import _RunHistoryStore, _run_record
from tests.test_merge_train_github_failures import _failed_request
from tests.test_merge_train_scheduler import _policy_record


_FAILED_AT = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _failure(repository: str, detail: str) -> MergeTrainControllerStateRecord:
    return MergeTrainControllerStateRecord(
        controller_key=build_merge_train_controller_key(repository=repository, base_branch="main"),
        repository=repository,
        base_branch="main",
        policy_key=f"{repository}:main",
        policy_sha256="policy-sha",
        status="reconcile_required",
        updated_at=_stamp(_FAILED_AT),
        active_action="land_batch",
        active_phase="merge_batch_entries",
        reconciliation_status="required",
        reconciliation_detail=detail,
    )


class _QuotaStore(_RunHistoryStore):
    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]:
        return tuple(
            state
            for state in self.controller_state_records
            if state.repository == repository and state.base_branch == base_branch
        )[:limit]


class MergeTrainQuotaTests(TestCase):
    def test_graphql_body_quota_preserves_the_response_deadline_for_admission(self) -> None:
        reset = _FAILED_AT + timedelta(minutes=30)
        headers = Message()
        headers["X-RateLimit-Reset"] = str(int(reset.timestamp()))
        headers["X-RateLimit-Remaining"] = "0"

        @contextmanager
        def response(*args: object, **kwargs: object) -> Iterator[BytesIO]:
            with BytesIO(
                b'{"data":null,"errors":[{"type":"RATE_LIMITED","message":"secret-provider-message"}]}'
            ) as body:
                body.headers = headers  # type: ignore[attr-defined]
                yield body

        transport = UrllibMergeTrainGitHubTransport(token="secret-token")
        with patch("control_plane.merge_train_github.urlopen", side_effect=response):
            with self.assertRaises(MergeTrainGitHubError) as caught:
                transport.request(method="POST", path="/graphql", body={"query": "private-query"})
        detail = _controller_exception_reconciliation_detail(caught.exception)
        self.assertNotIn("secret", detail)
        self.assertNotIn("private", detail)
        state = _failure("cbusillo/alpha", detail)
        store = _QuotaStore(None, controller_state_records=(state,))
        decision = evaluate_merge_train_admission_from_store(
            store=store,
            repository=state.repository,
            base_branch=state.base_branch,
            requested_at=_stamp(_FAILED_AT + timedelta(minutes=5)),
        )
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.next_allowed_at, _stamp(reset))

    def test_secondary_limit_with_primary_quota_left_recovers_after_retry_after(self) -> None:
        reset_at = int((_FAILED_AT + timedelta(minutes=59)).timestamp())
        for remaining, minutes in (("100", 1), ("0", 59)):
            error = _failed_request(
                429,
                {
                    "X-RateLimit-Remaining": remaining,
                    "X-RateLimit-Reset": str(reset_at),
                    "Retry-After": "60",
                },
            )
            state = _failure("cbusillo/alpha", _controller_exception_reconciliation_detail(error))
            store = _QuotaStore(None, controller_state_records=(state,))
            decision = evaluate_merge_train_admission_from_store(
                store=store,
                repository=state.repository,
                base_branch=state.base_branch,
                requested_at=_stamp(_FAILED_AT + timedelta(seconds=1)),
            )
            self.assertEqual(
                decision.next_allowed_at, _stamp(_FAILED_AT + timedelta(minutes=minutes))
            )
            recovered = evaluate_merge_train_admission_from_store(
                store=store,
                repository=state.repository,
                base_branch=state.base_branch,
                requested_at=decision.next_allowed_at,
            )
            self.assertTrue(recovered.admitted)

    def test_recorded_deadlines_override_absent_dry_run_and_old_mutation_history(self) -> None:
        repository = "cbusillo/sellyouroutboard"
        reset = int((_FAILED_AT + timedelta(minutes=12)).timestamp())
        cases = (
            (f"reset_at:{reset}", 12),
            ("retry_after_seconds:600", 10),
            (f"reset_at:{reset}; retry_after_seconds:600", 12),
            (f"reset_at:{reset}; retry_after_seconds:900", 15),
        )
        histories = (
            None,
            _run_record(recorded_at=_stamp(_FAILED_AT - timedelta(hours=1))),
            _run_record(recorded_at=_stamp(_FAILED_AT - timedelta(hours=1)), mutation="wait"),
        )
        for metadata, minutes in cases:
            for history in histories:
                with self.subTest(metadata=metadata, history=history):
                    store = _QuotaStore(
                        history,
                        controller_state_records=(
                            _failure(repository, f"retryable:github_rate_limited; {metadata}"),
                        ),
                    )
                    for seconds in (1, 60, 299):
                        decision = evaluate_merge_train_admission_from_store(
                            store=store,
                            repository=repository,
                            base_branch="main",
                            requested_at=_stamp(_FAILED_AT + timedelta(seconds=seconds)),
                        )
                        self.assertFalse(decision.admitted)
                        self.assertEqual(
                            decision.next_allowed_at,
                            _stamp(_FAILED_AT + timedelta(minutes=minutes)),
                        )
                    read_model = build_merge_train_controller_status_read_model(
                        store=store,
                        repository=repository,
                        base_branch="main",
                        generated_at=_stamp(_FAILED_AT + timedelta(seconds=1)),
                    )
                    self.assertEqual(read_model.admission.reason_code, "github_rate_limit_pending")
                    recovered = evaluate_merge_train_admission_from_store(
                        store=store,
                        repository=repository,
                        base_branch="main",
                        requested_at=_stamp(_FAILED_AT + timedelta(minutes=minutes)),
                    )
                    self.assertTrue(recovered.admitted)
                    self.assertEqual(store.controller_state_records[0].status, "reconcile_required")

    def test_quota_cannot_shorten_a_longer_level1_backoff(self) -> None:
        repository = "cbusillo/sellyouroutboard"
        store = _QuotaStore(
            _run_record(recorded_at=_stamp(_FAILED_AT), mutation="block"),
            controller_state_records=(
                _failure(repository, "retryable:github_rate_limited; retry_after_seconds:60"),
            ),
        )
        decision = evaluate_merge_train_admission_from_store(
            store=store,
            repository=repository,
            base_branch="main",
            requested_at=_stamp(_FAILED_AT + timedelta(seconds=1)),
        )
        self.assertEqual(decision.next_allowed_at, _stamp(_FAILED_AT + timedelta(seconds=300)))
        self.assertEqual(decision.reason_code, "backoff_pending")

    def test_real_refusals_and_uncertain_effects_do_not_become_quota_deferrals(self) -> None:
        for detail in (
            "operator_required:github_request_rejected; reset_at:1791588000",
            "operator_required:pull_request_merge_blocked",
            "retryable:github_request_failed",
            "operator_required:outcome_reconcile_required",
        ):
            with self.subTest(detail=detail):
                state = _failure("cbusillo/alpha", detail)
                decision = evaluate_merge_train_admission_from_store(
                    store=_QuotaStore(None, controller_state_records=(state,)),
                    repository=state.repository,
                    base_branch=state.base_branch,
                    requested_at=_stamp(_FAILED_AT + timedelta(seconds=1)),
                )
                self.assertTrue(decision.admitted)
                self.assertEqual(state.reconciliation_detail, detail)

    def test_unusable_quota_metadata_has_a_fixed_one_minute_wait(self) -> None:
        for metadata in ("", "; reset_at:secret", "; retry_after_seconds:999999999999"):
            state = _failure("cbusillo/alpha", "retryable:github_rate_limited" + metadata)
            store = _QuotaStore(None, controller_state_records=(state,))
            for seconds in (1, 59, 60):
                decision = evaluate_merge_train_admission_from_store(
                    store=store,
                    repository=state.repository,
                    base_branch=state.base_branch,
                    requested_at=_stamp(_FAILED_AT + timedelta(seconds=seconds)),
                )
                self.assertEqual(decision.admitted, seconds == 60)

    def test_scheduler_skips_provider_calls_on_repeated_wakes_then_recovers(self) -> None:
        policy_record = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
        )
        state = _failure("cbusillo/alpha", "retryable:github_rate_limited; retry_after_seconds:600")
        store = _QuotaStore(None, controller_state_records=(state,))
        with (
            patch(
                "control_plane.merge_train_scheduler.resolve_merge_train_policy_record",
                return_value=policy_record,
            ),
            patch(
                "control_plane.merge_train_scheduler.resolve_merge_train_github_token",
                return_value="test-token",
            ) as token,
            patch(
                "control_plane.merge_train_scheduler._run_controller",
                return_value=MergeTrainScheduledTargetResult(
                    repository=state.repository,
                    base_branch="main",
                    runner_mode="controller",
                    mutate=True,
                    status="ran",
                ),
            ) as controller,
        ):
            for seconds in (1, 2, 300, 599):
                results = run_merge_train_scheduler_pass(
                    record_store=store,
                    control_plane_root=Path("/unused"),
                    now=lambda: _stamp(_FAILED_AT + timedelta(seconds=seconds)),
                )
                self.assertEqual(results[0].status, "deferred")
            token.assert_not_called()
            controller.assert_not_called()
            results = run_merge_train_scheduler_pass(
                record_store=store,
                control_plane_root=Path("/unused"),
                now=lambda: _stamp(_FAILED_AT + timedelta(seconds=600)),
            )
            self.assertEqual(results[0].status, "ran")
            token.assert_called_once()
            controller.assert_called_once()

    def test_shared_app_account_quota_defers_peers_but_not_other_accounts_or_apps(self) -> None:
        policy_record = _policy_record(
            *(
                (repo, MergeTrainSchedulerPolicy(enabled=True, mutate=True))
                for repo in ("cbusillo/alpha", "cbusillo/beta", "other/gamma", "cbusillo/delta")
            ),
        )
        policies = list(policy_record.policy.policies)
        for policy in policies:
            if policy.repository == "cbusillo/delta":
                assert policy.github_token.github_app is not None
                policy.github_token.github_app.app_id = 84
            if policy.repository == "cbusillo/beta":
                assert policy.github_token.github_app is not None
                policy.github_token.github_app.repository_id = 456
        policy_record = type(policy_record).model_validate(
            policy_record.model_dump(exclude={"policy_sha256"})
        )
        state = _failure(
            "cbusillo/alpha", "retryable:github_rate_limited; retry_after_seconds:600"
        ).model_copy(update={"policy_sha256": policy_record.policy_sha256})
        store = _QuotaStore(None, controller_state_records=(state,))
        for repository, admitted in (
            ("cbusillo/beta", False),
            ("other/gamma", True),
            ("cbusillo/delta", True),
        ):
            with self.subTest(repository=repository):
                model = build_merge_train_controller_status_read_model(
                    store=store,
                    repository=repository,
                    base_branch="main",
                    generated_at=_stamp(_FAILED_AT + timedelta(seconds=1)),
                    policy_record=policy_record,
                )
                self.assertEqual(model.admission.admitted, admitted)
        store.controller_state_records = (state.model_copy(update={"policy_sha256": "old"}),)
        decision = evaluate_merge_train_admission_from_store(
            store=store,
            repository="cbusillo/beta",
            base_branch="main",
            requested_at=_stamp(_FAILED_AT + timedelta(seconds=1)),
            policy_record=policy_record,
        )
        self.assertTrue(decision.admitted)
