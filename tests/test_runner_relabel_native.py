from contextlib import nullcontext
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from datetime import timedelta
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import (
    parse_launchplane_mutation_timestamp,
    format_launchplane_mutation_timestamp,
)
from control_plane.contracts.runner_relabel import (
    RunnerRelabelPolicy,
    RunnerRelabelReviewRequest,
    RunnerRelabelRunner,
)
from control_plane.provider_operations import (
    run_durable_provider_operation,
    build_provider_operation_key,
)
from control_plane.runner_relabel import (
    ROUTE,
    RunnerRelabelAdapter,
    RunnerRelabelInactive,
    actor_scope,
    create_review,
    load_policy,
    runner_transport,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_runner_lane_relabel import labels


class RunnerTransport:
    def __init__(self) -> None:
        self.custom: tuple[str, ...] = ("general",)
        self.automatic: tuple[str, ...] = ("self-hosted", "linux")
        self.puts = 0
        self.put_error: int | None = None
        self.read_error_after_put = False
        self.unknown = False
        self.busy = False
        self.runner_name = "general-1"
        self.runner_id = 7
        self.put_paths: list[str] = []

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        from control_plane.merge_train_github import MergeTrainGitHubError

        if method == "GET" and self.puts and self.read_error_after_put:
            raise MergeTrainGitHubError("fixture post-read failure", status_code=422)
        if method == "PUT":
            self.puts += 1
            self.put_paths.append(path)
            if self.put_error is not None:
                raise MergeTrainGitHubError("fixture rejected request", status_code=self.put_error)
            assert body is not None
            values = body["labels"]
            assert isinstance(values, list) and all(isinstance(value, str) for value in values)
            self.custom = tuple(values)
            if self.unknown:
                raise TimeoutError("sensitive provider response deliberately not retained")
        if "/actions/runners?" in path:
            return {
                "runners": [
                    {
                        "id": self.runner_id,
                        "name": self.runner_name,
                        "status": "online",
                        "busy": self.busy,
                        "labels": [{"name": label} for label in (*self.automatic, *self.custom)],
                    }
                ]
            }
        return labels(*self.custom, automatic=self.automatic)


class RunnerRelabelNativeTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=self.database_url
            or f"sqlite+pysqlite:///{Path(directory) / 'runner.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        if not self.database_url:
            self.store.ensure_schema()
        self.policy = RunnerRelabelPolicy(
            policy_id="fixture",
            product="fixture-product",
            context="fixture-context",
            repository="example/control",
            repository_id=101,
            host_name="build-1",
            runners=(
                RunnerRelabelRunner(
                    runner_id=7, runner_name="general-1", registration_root="/srv/runners/general-1"
                ),
            ),
            protected_runner_names=("operations-1",),
            protected_labels=("operations",),
            enabled=True,
        )
        self.saved = self.store.compare_and_write_runner_relabel_policy(
            "fixture",
            {
                "policy": self.policy.model_dump(mode="json"),
                "actor": {"type": "github_human", "github_id": 51},
                "reason": "fixture reviewed binding",
                "expected_policy_sha256": "",
            },
            expected_sha256="",
        )
        self.actor = {"type": "github_human", "github_id": 51}
        self.transport = RunnerTransport()
        self.factory = self.enterContext(
            patch(
                "control_plane.runner_relabel.runner_transport",
                side_effect=lambda *_args, **_kwargs: nullcontext(self.transport),
            )
        )

    def review(self, **changes: Any) -> dict[str, Any]:
        return create_review(
            self.store,
            "fixture",
            RunnerRelabelReviewRequest.model_validate(
                {
                    "runner_id": 7,
                    "desired_custom_labels": ["candidate", "general"],
                    "reason": "fixture candidate routing",
                    **changes,
                }
            ),
            self.actor,
        )

    def run_change(
        self,
        review: dict[str, Any],
        *,
        key: str = "original",
        observe: bool = False,
        evidence_only: bool = False,
        actor: dict[str, object] | None = None,
        authorize_effect: Callable[[], None] = lambda: None,
    ) -> Any:
        principal = actor or self.actor
        intent: dict[str, object] = {"review": review, "actor": principal, "operation_key": key}
        return run_durable_provider_operation(
            store=self.store,
            scope=actor_scope(principal),
            route_path=ROUTE.format(policy_id="fixture"),
            idempotency_key=key,
            request_fingerprint=canonical_json_sha256(intent),
            lease_owner="fixture-request",
            response_trace_id="fixture-trace",
            adapter=RunnerRelabelAdapter(
                self.store, review, "fixture-trace", key, evidence_only, authorize_effect
            ),
            original_intent=intent,
            allow_mutation=not observe,
        )

    def test_review_apply_replay_and_empty_inverse(self) -> None:
        self.transport.custom = ()
        review = self.review()
        self.assertEqual(self.transport.puts, 0)
        result = self.run_change(review)
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.run_change(review).status, "replayed")
        self.assertEqual(self.transport.puts, 1)
        self.store.write_runner_relabel_document(
            "review-operation",
            review["review_id"],
            {"key": "original", "actor": self.actor, "review_sha256": review["review_sha256"]},
        )
        inverse = self.review(desired_custom_labels=[], inverse_of_review_id=review["review_id"])
        self.assertEqual(self.run_change(inverse, key="inverse").status, "completed")
        self.assertEqual(self.transport.custom, ())

    def test_lost_put_response_retains_cross_actor_fence_and_observes_without_put(self) -> None:
        review = self.review()
        self.transport.unknown = True
        result = self.run_change(review)
        self.assertEqual(result.status, "reconcile_required")
        other = self.run_change(
            review, key="other", actor={"type": "github_human", "github_id": 52}
        )
        self.assertEqual(other.status, "target_busy")
        self.transport.unknown = False
        self.assertEqual(self.run_change(review, observe=True).status, "adopted")
        self.assertEqual(self.transport.puts, 1)

    def test_absent_readback_never_retries_the_effect(self) -> None:
        review = self.review()
        self.transport.unknown = True
        self.run_change(review)
        self.transport.custom = ("general",)
        self.assertEqual(self.run_change(review, observe=True).status, "reconcile_required")
        self.assertEqual(self.transport.puts, 1)

    def test_only_exact_put_validation_rejection_settles_without_retry(self) -> None:
        review = self.review()
        self.transport.put_error = 422
        result = self.run_change(review)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.response_status_code, 409)
        self.assertEqual(self.transport.puts, 1)
        assert result.record is not None
        audit = self.store.read_runner_relabel_document(
            "terminal-audit",
            build_provider_operation_key(
                scope=result.record.scope,
                route_path=result.record.route_path,
                idempotency_key=result.record.idempotency_key,
                request_fingerprint=result.record.request_fingerprint,
                reconciliation_key=result.record.reconciliation_key,
            ),
        )
        assert audit is not None
        self.assertEqual(audit["message"], result.response_payload["result"]["status"])
        self.transport.put_error = None
        self.assertEqual(self.run_change(self.review(), key="fresh").status, "completed")
        self.assertEqual(self.transport.puts, 2)

    def test_put_server_failure_retains_original_target_fence(self) -> None:
        review = self.review()
        self.transport.put_error = 503
        self.assertEqual(self.run_change(review).status, "reconcile_required")
        self.transport.put_error = None
        self.assertEqual(self.run_change(review, observe=True).status, "reconcile_required")
        self.assertEqual(self.run_change(self.review(), key="fresh").status, "target_busy")
        self.assertEqual(self.transport.puts, 1)

    def test_post_put_validation_failure_is_not_terminal_put_rejection(self) -> None:
        review = self.review()
        self.transport.read_error_after_put = True
        self.assertEqual(self.run_change(review).status, "reconcile_required")
        self.assertEqual(
            self.run_change(review, observe=True, evidence_only=True).status, "reconcile_required"
        )
        self.assertEqual(self.transport.puts, 1)

    def test_policy_and_runner_drift_refuse_before_put(self) -> None:
        for drift in ("busy", "name", "automatic", "policy"):
            with self.subTest(drift=drift):
                review = self.review()
                if drift == "policy":
                    current = load_policy(self.store, "fixture")
                    self.store.compare_and_write_runner_relabel_policy(
                        "fixture",
                        {
                            "policy": self.policy.model_dump(mode="json"),
                            "actor": self.actor,
                            "reason": "new review",
                            "expected_policy_sha256": current["policy_sha256"],
                        },
                        expected_sha256=current["policy_sha256"],
                    )
                elif drift == "busy":
                    self.transport.busy = True
                elif drift == "name":
                    self.transport.runner_name = "foreign"
                else:
                    self.transport.automatic = ("self-hosted", "windows")
                result = self.run_change(review, key=drift)
                self.assertEqual(result.response_status_code, 409)
                if drift == "policy":
                    assert result.record is not None
                    audit = self.store.read_runner_relabel_document(
                        "terminal-audit",
                        build_provider_operation_key(
                            scope=result.record.scope,
                            route_path=result.record.route_path,
                            idempotency_key=result.record.idempotency_key,
                            request_fingerprint=result.record.request_fingerprint,
                            reconciliation_key=result.record.reconciliation_key,
                        ),
                    )
                    assert audit is not None
                    self.assertEqual(audit["message"], result.response_payload["result"]["status"])
                self.assertEqual(self.transport.puts, 0)
                self.transport.busy = False
                self.transport.runner_name = "general-1"
                self.transport.automatic = ("self-hosted", "linux")

    def test_roster_rejects_empty_duplicate_and_unapproved_targets(self) -> None:
        roster = self.policy.model_dump(mode="json")
        for field, value in (
            ("runner_id", 8),
            ("runner_name", "general-2"),
            ("registration_root", "/srv/runners/general-2"),
        ):
            duplicate = {**roster["runners"][0], field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                RunnerRelabelPolicy.model_validate(
                    {**roster, "runners": [*roster["runners"], duplicate]}
                )
        with self.assertRaises(ValueError):
            RunnerRelabelPolicy.model_validate({**roster, "runners": []})
        self.factory.reset_mock()
        with self.assertRaises(ValueError):
            self.review(runner_id=99)
        self.factory.assert_not_called()

    def test_review_selects_one_roster_entry_and_freezes_its_target(self) -> None:
        roster = self.policy.model_dump(mode="json")
        runner: dict[str, Any] = {
            "runner_id": 8,
            "runner_name": "general-2",
            "registration_root": "/srv/runners/general-2",
        }
        roster["runners"].append(runner)
        self.store.compare_and_write_runner_relabel_policy(
            "fixture",
            {
                "policy": roster,
                "actor": self.actor,
                "reason": "reviewed host roster",
                "expected_policy_sha256": self.saved["policy_sha256"],
            },
            expected_sha256=self.saved["policy_sha256"],
        )
        self.transport.runner_id = runner["runner_id"]
        self.transport.runner_name = runner["runner_name"]
        review = self.review(runner_id=runner["runner_id"])
        self.assertEqual(review["runner"], runner)
        self.assertEqual(self.run_change(review).status, "completed")
        self.assertEqual(
            self.transport.put_paths,
            [f"/repos/{self.policy.repository}/actions/runners/{runner['runner_id']}/labels"],
        )

    def test_predispatch_refusal_settles_and_fresh_review_can_proceed(self) -> None:
        review = self.review()
        self.transport.busy = True
        refused = self.run_change(review)
        self.assertEqual(refused.response_status_code, 409)
        self.assertEqual(refused.response_payload["result"]["status"], "refused_before_dispatch")
        self.assertEqual(self.transport.puts, 0)
        self.transport.busy = False
        fresh = self.review()
        self.assertEqual(self.run_change(fresh, key="fresh").status, "completed")
        self.assertEqual(self.transport.puts, 1)

    def test_historical_observation_uses_reviewed_policy_after_policy_change(self) -> None:
        review = self.review()
        self.transport.unknown = True
        self.assertEqual(self.run_change(review).status, "reconcile_required")
        current = load_policy(self.store, "fixture")
        self.store.compare_and_write_runner_relabel_policy(
            "fixture",
            {
                "policy": self.policy.model_copy(update={"enabled": False}).model_dump(mode="json"),
                "actor": self.actor,
                "reason": "disable future work",
                "expected_policy_sha256": current["policy_sha256"],
            },
            expected_sha256=current["policy_sha256"],
        )
        self.transport.unknown = False
        self.assertEqual(self.run_change(review, observe=True).status, "adopted")
        self.assertEqual(self.transport.puts, 1)

    def test_custody_and_policy_cas_are_immutable(self) -> None:
        review = self.review()
        with self.assertRaises(ValueError):
            self.store.write_runner_relabel_document(
                "review", review["review_id"], {**review, "actor": {"github_id": 90}}
            )
        with self.assertRaises(ValueError):
            self.store.compare_and_write_runner_relabel_policy("fixture", {}, expected_sha256="")
        self.assertEqual(
            self.store.read_runner_relabel_document("review", review["review_id"]), review
        )

    def test_unknown_evidence_only_recovery_does_not_construct_provider(self) -> None:
        review = self.review()
        self.transport.unknown = True
        self.run_change(review)
        self.factory.reset_mock()
        result = self.run_change(review, observe=True, evidence_only=True)
        self.assertEqual(result.status, "reconcile_required")
        self.factory.assert_not_called()

    def test_missing_capability_fails_closed_without_provider(self) -> None:
        with patch("control_plane.runner_relabel.resolve_context_secret_value") as secret:
            with self.assertRaises(RunnerRelabelInactive):
                with runner_transport(self.store, self.policy, write=True):
                    self.fail("Missing capability must never yield a provider.")
            secret.assert_not_called()

    def test_runner_capability_uses_exact_purpose_context_and_revokes_token(self) -> None:
        from control_plane.contracts.runner_relabel import RunnerRelabelCapability
        from control_plane.github_app_identity import GitHubAppInstallationToken
        from control_plane.runner_relabel import RUNNER_SECRET_BINDING, RUNNER_SECRET_INTEGRATION

        capability = RunnerRelabelCapability(
            app_id=91, installation_id=92, secret_context="fixture-runner-purpose", accepted=True
        )
        policy = self.policy.model_copy(update={"capability": capability})
        token = GitHubAppInstallationToken(
            token="synthetic-test-token",
            app_id=capability.app_id,
            installation_id=capability.installation_id,
            repository_id=policy.repository_id,
            repository=policy.repository,
            expires_at="fixture-unused",
        )
        with (
            patch(
                "control_plane.runner_relabel.resolve_context_secret_value",
                return_value="synthetic-test-key",
            ) as secret,
            patch(
                "control_plane.runner_relabel.mint_runner_relabel_installation_token",
                return_value=token,
            ) as mint,
            patch("control_plane.runner_relabel.revoke_installation_token") as revoke,
        ):
            with runner_transport(self.store, policy, write=False):
                pass
            secret.assert_called_once_with(
                integration=RUNNER_SECRET_INTEGRATION,
                context_name=capability.secret_context,
                binding_key=RUNNER_SECRET_BINDING,
                database_url=self.store.database_url,
            )
            self.assertFalse(mint.call_args.kwargs["write"])
            self.assertEqual(mint.call_args.kwargs["installation_id"], capability.installation_id)
            self.assertEqual(mint.call_args.kwargs["repository_id"], str(policy.repository_id))
            revoke.assert_called_once_with(installation_token=token)

    def test_retained_terminal_evidence_recovers_lost_completion_without_provider(self) -> None:
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                review = self.review()
                self.transport.put_error = 422 if rejected else None
                before = self.transport.puts
                key = str(rejected)
                with patch.object(
                    self.store,
                    "complete_mutation_reservation",
                    side_effect=OSError("fixture completion lost"),
                ):
                    with self.assertRaises(OSError):
                        self.run_change(review, key=key)
                self.assertEqual(self.transport.puts, before + 1)
                self.factory.reset_mock()
                original = self.store.read_idempotency_record(
                    scope=actor_scope(self.actor),
                    route_path=ROUTE.format(policy_id="fixture"),
                    idempotency_key=key,
                )
                assert original is not None
                after_expiry = format_launchplane_mutation_timestamp(
                    parse_launchplane_mutation_timestamp(
                        original.lease_expires_at, field_name="lease_expires_at"
                    )
                    + timedelta(seconds=1)
                )
                with patch.object(
                    self.store, "_database_mutation_timestamp", return_value=after_expiry
                ):
                    recovered = self.run_change(review, key=key, observe=True, evidence_only=True)
                self.assertEqual(recovered.status, "adopted")
                self.assertEqual(recovered.response_status_code, 409 if rejected else 202)
                self.factory.assert_not_called()
                self.transport.put_error = None

    def test_revocation_failure_preserves_review_apply_and_observation_results(self) -> None:
        from control_plane.contracts.runner_relabel import RunnerRelabelCapability
        from control_plane.github_app_identity import GitHubAppInstallationToken

        capability = RunnerRelabelCapability(
            app_id=91, installation_id=92, secret_context="fixture-runner-purpose", accepted=True
        )
        policy = self.policy.model_copy(update={"capability": capability})
        current = load_policy(self.store, policy.policy_id)
        self.store.compare_and_write_runner_relabel_policy(
            policy.policy_id,
            {
                "policy": policy.model_dump(mode="json"),
                "actor": self.actor,
                "reason": "fixture accepted capability",
                "expected_policy_sha256": current["policy_sha256"],
            },
            expected_sha256=current["policy_sha256"],
        )
        token = GitHubAppInstallationToken(
            token="synthetic-test-token",
            app_id=capability.app_id,
            installation_id=capability.installation_id,
            repository_id=policy.repository_id,
            repository=policy.repository,
            expires_at="fixture-unused",
        )
        self.factory.side_effect = runner_transport
        with (
            patch(
                "control_plane.runner_relabel.resolve_context_secret_value",
                return_value="synthetic-test-key",
            ),
            patch(
                "control_plane.runner_relabel.mint_runner_relabel_installation_token",
                return_value=token,
            ) as mint,
            patch(
                "control_plane.runner_relabel.UrllibMergeTrainGitHubTransport",
                return_value=self.transport,
            ),
            patch(
                "control_plane.runner_relabel.revoke_installation_token",
                side_effect=OSError("private provider revocation response"),
            ),
            self.assertLogs("control_plane.runner_relabel", level="WARNING") as logs,
        ):
            review = self.review()
            self.assertEqual(self.run_change(review).status, "completed")
            observation = RunnerRelabelAdapter(
                self.store, review, "observe-trace", "original"
            ).observe("unused", "", review["review_sha256"])
            self.assertEqual(observation.outcome, "present")
        self.assertEqual(
            [call.kwargs["write"] for call in mint.call_args_list], [False, True, False]
        )
        self.assertNotIn("private provider revocation response", "\n".join(logs.output))
        self.assertEqual(self.transport.puts, 1)

    def test_planned_evidence_failure_prevents_put_and_terminal_failure_retains_unknown(
        self,
    ) -> None:
        for stage in ("planned", "terminal-audit"):
            with self.subTest(stage=stage):
                review = self.review()
                write = self.store.write_runner_relabel_document

                def fail(kind: str, *args: Any, **kwargs: Any) -> None:
                    if kind == stage:
                        raise OSError("fixture evidence unavailable")
                    write(kind, *args, **kwargs)

                with patch.object(self.store, "write_runner_relabel_document", side_effect=fail):
                    if stage == "planned":
                        self.assertEqual(
                            self.run_change(review, key=stage).response_status_code, 409
                        )
                        self.assertEqual(self.transport.puts, 0)
                    else:
                        self.assertEqual(
                            self.run_change(review, key=stage).status, "reconcile_required"
                        )
                        self.assertEqual(self.transport.puts, 1)
                if stage == "terminal-audit":
                    self.assertEqual(
                        self.run_change(review, key=stage, observe=True).status, "adopted"
                    )
                    self.assertEqual(self.transport.puts, 1)

    def test_malformed_policy_envelope_is_not_authority(self) -> None:
        from control_plane.storage.postgres import LaunchplaneIdempotencyRow
        from sqlalchemy import select

        with self.store._session_factory() as session:
            row = session.scalar(
                select(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.scope == "runner-relabel-policy"
                )
            )
            assert row is not None
            bad = deepcopy(row.payload)
            bad["response_payload"]["revision"] = 999
            row.payload = bad
            session.commit()
        with self.assertRaises(ValueError):
            load_policy(self.store, "fixture")
