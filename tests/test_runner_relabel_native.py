"""Native custody, exact effect and restart recovery against fictional runners."""

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
from control_plane.contracts.runner_relabel import RunnerRelabelPolicy, RunnerRelabelReviewRequest
from control_plane.provider_operations import run_durable_provider_operation
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
        self.unknown = False
        self.busy = False
        self.runner_name = "general-1"

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method == "PUT":
            self.puts += 1
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
                        "id": 7,
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
            runner_id=7,
            runner_name="general-1",
            host_name="build-1",
            registration_root="/srv/runners/general-1",
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
                try:
                    self.run_change(review, key=drift)
                except ValueError:
                    pass
                self.assertEqual(self.transport.puts, 0)
                self.transport.busy = False
                self.transport.runner_name = "general-1"
                self.transport.automatic = ("self-hosted", "linux")

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
            with self.assertRaisesRegex(
                RunnerRelabelInactive, "runner_purpose_capability_not_accepted"
            ):
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
        review = self.review()
        with patch.object(
            self.store,
            "complete_mutation_reservation",
            side_effect=OSError("fixture completion lost"),
        ):
            with self.assertRaises(OSError):
                self.run_change(review)
        self.assertEqual(self.transport.puts, 1)
        self.factory.reset_mock()
        original = self.store.read_idempotency_record(
            scope=actor_scope(self.actor),
            route_path=ROUTE.format(policy_id="fixture"),
            idempotency_key="original",
        )
        assert original is not None
        after_expiry = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(
                original.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        )
        with patch.object(self.store, "_database_mutation_timestamp", return_value=after_expiry):
            self.assertEqual(
                self.run_change(review, observe=True, evidence_only=True).status, "adopted"
            )
        self.factory.assert_not_called()

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
                        with self.assertRaises(ValueError):
                            self.run_change(review, key=stage)
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
