"""Private-ingress recovery through real PostgreSQL reservations and leases."""

from typing import Any
from unittest.mock import patch

from control_plane.provider_operations import run_durable_provider_operation
from control_plane.testing_private_ingress import ROUTE
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)
from tests import test_testing_private_ingress as private_ingress_tests
from tests.test_postgres_integration import _head_postgres_database


class RealPostgresTestingPrivateIngressRecoveryTests(private_ingress_tests.PrivateIngressFixture):
    def setUp(self) -> None:
        self.database_url = self.enterContext(_head_postgres_database())
        super().setUp()

    def run_ingress(self, adapter: Any, key: str = "original-key") -> Any:
        return run_durable_provider_operation(
            store=self.store,
            scope="private-ingress-recovery-fixture",
            route_path=ROUTE,
            idempotency_key=key,
            request_fingerprint=key,
            lease_owner="fixture-worker",
            response_trace_id="fixture-trace",
            adapter=adapter,
        )

    def test_original_key_recovers_absent_domain_through_durable_runner(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        provider_call = self.dokploy_request

        def reject_domain(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/domain.create":
                raise ValueError("Synthetic domain rejection")
            return provider_call(**kwargs)

        with patch(
            "control_plane.testing_private_ingress.api.dokploy_request", side_effect=reject_domain
        ):
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        self.assertEqual(self.domains, [])
        lock_name = testing_event_execution_lock_name(
            dokploy_generic_web_provider_target_key(self.provider)
        )
        with self.store.testing_replacement_execution_lock(lock_name) as check:
            self.assertIsNotNone(check)
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        self.assertEqual(self.run_ingress(adapter).status, "completed")
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])
        self.assertEqual(len(self.client.proxy_hosts), 1)

    def test_changed_authority_after_observation_retains_partial_hold(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        with patch.object(
            self.client, "create_proxy_host", side_effect=ValueError("edge rejected")
        ):
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        observe = adapter.observe

        def drift_after_observation(*args: Any) -> Any:
            result = observe(*args)
            self.assertEqual(result.outcome, "absent")
            self.store.write_product_profile_record(
                self.profile.model_copy(update={"runtime_port": self.profile.runtime_port + 1})
            )
            return result

        with patch.object(adapter, "observe", side_effect=drift_after_observation):
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        self.assertEqual(self.run_ingress(adapter, "different-key").status, "target_busy")
        self.store.write_product_profile_record(self.profile)
        self.assertEqual(self.run_ingress(adapter).status, "completed")
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])

    def test_busy_resume_retains_partial_hold_and_later_completes(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        with patch.object(
            self.client, "create_proxy_host", side_effect=ValueError("edge rejected")
        ):
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        lock_name = testing_event_execution_lock_name(
            dokploy_generic_web_provider_target_key(self.provider)
        )
        with self.store.testing_replacement_execution_lock(lock_name) as check:
            self.assertIsNotNone(check)
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
            self.assertEqual(self.run_ingress(adapter, "different-key").status, "target_busy")
        stored = self.store.read_idempotency_record(
            scope="private-ingress-recovery-fixture",
            route_path=ROUTE,
            idempotency_key="original-key",
        )
        assert stored is not None
        self.assertEqual(stored.state, "reconcile_required")
        self.assertEqual(self.run_ingress(adapter).status, "completed")
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])
        self.assertEqual(len(self.client.proxy_hosts), 1)
