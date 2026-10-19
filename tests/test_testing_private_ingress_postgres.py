"""Private-ingress recovery through real PostgreSQL reservations and leases."""

from typing import Any, cast
from unittest.mock import patch

from control_plane.provider_operations import run_durable_provider_operation
from control_plane.testing_private_ingress import ROUTE
from control_plane.contracts.ingress_route_audit_record import TESTING_PRIVATE_INGRESS_AUDIT_PREFIX
from control_plane.contracts.private_health_endpoint_record import (
    private_health_endpoint_record_sha256,
)
from control_plane.outbound_http import PublicHttpResponse
from control_plane.testing_private_health import fetch_testing_private_health
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)
from tests import test_testing_private_ingress as private_ingress_tests
from tests.test_postgres_integration import _head_postgres_database


class RealPostgresTestingPrivateIngressRecoveryTests(private_ingress_tests.PrivateIngressFixture):
    def test_historical_cleanup_preserves_original_key_recovery(self) -> None:
        self.create()
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        self.assertEqual(
            self.run_ingress(
                self.adapter(operation="disable", reviewed_plan_sha256=digest), "first-disable"
            ).status,
            "completed",
        )
        host_id = self.client.proxy_hosts[0].id
        self.client.enable_proxy_host(host_id)
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        adapter = self.adapter(operation="disable", reviewed_plan_sha256=digest)
        snapshot = adapter._snapshot
        snapshots = 0

        def fail_before_effects(*args: Any, **kwargs: Any) -> Any:
            nonlocal snapshots
            snapshots += 1
            if snapshots == 2:
                raise RuntimeError("provider read failed before effects")
            return snapshot(*args, **kwargs)

        with (
            patch.object(adapter, "_snapshot", side_effect=fail_before_effects),
            self.assertRaises(ValueError),
        ):
            self.run_ingress(adapter, "cleanup")
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        with self.assertRaises(ValueError):
            self.adapter().plan()
        with patch.object(self.client, "disable_proxy_host", side_effect=ValueError("rejected")):
            self.assertEqual(self.run_ingress(adapter, "cleanup").status, "reconcile_required")
        self.assertEqual(self.run_ingress(adapter, "different-key").status, "target_busy")
        self.client.proxy_hosts[0].access_list_id = 0
        self.assertEqual(self.run_ingress(adapter, "cleanup").status, "reconcile_required")
        self.client.proxy_hosts[0].access_list_id = self.body.access_list_id
        with patch.object(self.client, "get_proxy_host", side_effect=RuntimeError("read failed")):
            self.assertEqual(self.run_ingress(adapter, "cleanup").status, "reconcile_required")
        self.assertEqual(self.run_ingress(adapter, "cleanup").status, "completed")
        self.assertTrue(adapter.readback()["applied"])

        self.client.enable_proxy_host(host_id)
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        adapter = self.adapter(operation="disable", reviewed_plan_sha256=digest)
        toggle = self.client.disable_proxy_host

        def lost_response(host_id: int) -> Any:
            toggle(host_id)
            raise TimeoutError("landed before response loss")

        with patch.object(self.client, "disable_proxy_host", side_effect=lost_response):
            self.assertEqual(self.run_ingress(adapter, "lost-cleanup").status, "reconcile_required")
        with patch.object(self.client, "disable_proxy_host", wraps=toggle) as repeated:
            self.assertEqual(self.run_ingress(adapter, "lost-cleanup").status, "adopted")
            repeated.assert_not_called()
        self.assertFalse(self.client.proxy_hosts[0].enabled)

    def test_late_toggle_response_is_adopted_without_repeating_toggle(self) -> None:
        self.create()
        for operation, method in (
            ("disable", "disable_proxy_host"),
            ("create", "enable_proxy_host"),
        ):
            with self.subTest(operation=operation):
                digest = self.adapter(operation=operation).plan()["plan_sha256"]
                adapter = self.adapter(operation=operation, reviewed_plan_sha256=digest)
                toggle = getattr(self.client, method)

                def lost_response(host_id: int) -> Any:
                    toggle(host_id)
                    raise TimeoutError("toggle landed before response loss")

                with patch.object(self.client, method, side_effect=lost_response):
                    self.assertEqual(
                        self.run_ingress(adapter, operation).status, "reconcile_required"
                    )
                with patch.object(self.client, method, wraps=toggle) as repeated:
                    self.assertEqual(self.run_ingress(adapter, operation).status, "adopted")
                    repeated.assert_not_called()
                self.assertEqual(self.client.proxy_hosts[0].enabled, operation == "create")
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])

    def test_original_key_resumes_rejected_disable_toggle(self) -> None:
        self.create()
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        adapter = self.adapter(operation="disable", reviewed_plan_sha256=digest)
        with patch.object(
            self.client, "disable_proxy_host", side_effect=ValueError("toggle rejected")
        ) as toggle:
            self.assertEqual(self.run_ingress(adapter, "disable-key").status, "reconcile_required")
            toggle.assert_called_once()
        self.assertTrue(self.client.proxy_hosts[0].enabled)
        self.assertEqual(self.run_ingress(adapter, "different-key").status, "target_busy")
        self.assertEqual(self.run_ingress(adapter, "disable-key").status, "completed")
        self.assertFalse(self.client.proxy_hosts[0].enabled)
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])
        self.assertEqual(sum(call.startswith("disable:") for call in self.client.calls), 1)

    def test_original_key_resumes_rejected_enable_toggle_after_metadata_change(self) -> None:
        self.create()
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        self.assertEqual(
            self.run_ingress(
                self.adapter(operation="disable", reviewed_plan_sha256=digest), "disable-key"
            ).status,
            "completed",
        )
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        with patch.object(
            self.client, "enable_proxy_host", side_effect=TimeoutError("toggle not landed")
        ):
            self.assertEqual(self.run_ingress(adapter, "enable-key").status, "reconcile_required")
        self.assertFalse(self.client.proxy_hosts[0].enabled)
        self.assertEqual(self.run_ingress(adapter, "different-key").status, "target_busy")
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"display_name": "Updated description"})
        )
        self.assertEqual(self.run_ingress(adapter, "enable-key").status, "completed")
        self.assertTrue(self.client.proxy_hosts[0].enabled)
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])
        self.assertEqual(sum(call.startswith("enable:") for call in self.client.calls), 1)

    def test_toggle_retry_retains_hold_for_drift_and_failed_read(self) -> None:
        self.create()
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        adapter = self.adapter(operation="disable", reviewed_plan_sha256=digest)
        with patch.object(
            self.client, "disable_proxy_host", side_effect=ValueError("toggle rejected")
        ):
            self.assertEqual(self.run_ingress(adapter, "disable-key").status, "reconcile_required")
        self.client.proxy_hosts[0].access_list_id = 0
        self.assertEqual(self.run_ingress(adapter, "disable-key").status, "reconcile_required")
        self.client.proxy_hosts[0].access_list_id = self.body.access_list_id
        with patch.object(self.client, "get_proxy_host", side_effect=RuntimeError("read failed")):
            self.assertEqual(self.run_ingress(adapter, "disable-key").status, "reconcile_required")
        self.assertEqual(
            adapter.observe("wrong-key", "apply_private_testing_edge", "key").outcome, "unknown"
        )
        self.assertEqual(self.run_ingress(adapter, "different-key").status, "target_busy")
        self.assertTrue(self.client.proxy_hosts[0].enabled)

    def test_reenable_retry_does_not_recreate_a_removed_application_domain(self) -> None:
        self.create()
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        self.assertEqual(
            self.run_ingress(
                self.adapter(operation="disable", reviewed_plan_sha256=digest), "disable-key"
            ).status,
            "completed",
        )
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        with patch.object(
            self.client, "enable_proxy_host", side_effect=ValueError("toggle rejected")
        ):
            self.assertEqual(self.run_ingress(adapter, "enable-key").status, "reconcile_required")
        self.domains.clear()
        self.assertEqual(self.run_ingress(adapter, "enable-key").status, "reconcile_required")
        self.assertEqual(self.run_ingress(adapter, "different-key").status, "target_busy")
        self.assertEqual(self.domains, [])
        self.assertFalse(self.client.proxy_hosts[0].enabled)
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])

    def test_corrected_authority_replans_after_proven_absence(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        provider_call = self.dokploy_request

        def reject_domain(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/domain.create":
                raise ValueError("Synthetic wrong runtime port")
            return provider_call(**kwargs)

        with patch(
            "control_plane.testing_private_ingress.api.dokploy_request", side_effect=reject_domain
        ):
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        corrected = self.profile.model_copy(update={"runtime_port": self.profile.runtime_port + 1})
        self.store.write_product_profile_record(corrected)
        resolved = self.run_ingress(adapter)
        self.assertEqual(resolved.status, "completed")
        result = cast(dict[str, object], resolved.response_payload["result"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["replan_required"])
        self.assertTrue(result["previous_effects_absent"])
        self.assertEqual(self.domains, [])
        self.assertEqual(self.client.proxy_hosts, [])
        with patch("control_plane.testing_private_ingress.api.dokploy_request") as dokploy:
            with patch.object(self.client, "create_proxy_host") as edge:
                self.assertEqual(self.run_ingress(adapter).status, "replayed")
                dokploy.assert_not_called()
                edge.assert_not_called()
        fresh = self.adapter().plan()["plan_sha256"]
        self.assertEqual(
            self.run_ingress(self.adapter(reviewed_plan_sha256=fresh), "corrected-plan").status,
            "completed",
        )
        self.assertEqual(self.domains[0]["port"], corrected.runtime_port)
        # The completed original-key replan leaves historical pending audit
        # evidence. It must not hold health after the fresh route completes.
        self.assertTrue(
            any(
                audit.status == "pending" for audit in self.store.list_ingress_route_audit_records()
            )
        )
        with (
            patch(
                "control_plane.testing_private_health.build_npmplus_ingress_client_from_env",
                return_value=self.client,
            ),
            patch(
                "control_plane.testing_private_health.request_private_edge_health",
                return_value=PublicHttpResponse(200, self.endpoint.url, 0, (), b"healthy"),
            ),
        ):
            response = fetch_testing_private_health(
                root=self.root,
                store=self.store,
                product=self.profile.product,
                context=self.context,
                instance="testing",
                endpoint_key=self.endpoint.endpoint_key,
                endpoint_sha256=private_health_endpoint_record_sha256(self.endpoint),
                url=self.endpoint.url,
                timeout_seconds=3,
            )
        self.assertEqual(response.status_code, 200)

    def test_same_key_after_clean_rejection_resolves_stale_pending_receipt(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        with patch.object(
            self.store, "write_product_public_hosts_bundle", side_effect=ValueError("guard refused")
        ):
            with self.assertRaises(ValueError):
                self.run_ingress(adapter)
            self.assertIsNone(
                self.store.read_idempotency_record(
                    scope="private-ingress-recovery-fixture",
                    route_path=ROUTE,
                    idempotency_key="original-key",
                )
            )
            resolved = self.run_ingress(adapter)
        self.assertEqual(resolved.status, "completed")
        result = cast(dict[str, object], resolved.response_payload["result"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["replan_required"])
        self.assertTrue(result["previous_effects_absent"])
        self.assertEqual(self.domains, [])
        self.assertEqual(self.client.proxy_hosts, [])
        self.assertEqual(self.run_ingress(adapter, "fresh-plan").status, "completed")

    def test_absence_uses_original_application_after_binding_replacement(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        with patch.object(
            self.client, "create_proxy_host", side_effect=ValueError("edge rejected")
        ):
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        replacement = self.target_id.model_copy(update={"target_id": "replacement-app"})
        self.store.write_dokploy_target_id_record(replacement)
        self.store.write_provider_target_record(
            self.provider.model_copy(update={"target_id": replacement.target_id})
        )
        self.actual["applicationId"] = replacement.target_id

        def inventory(**kwargs: Any) -> Any:
            if kwargs["application_id"] == self.target_id.target_id:
                return tuple(self.domains)
            return ()

        with patch(
            "control_plane.testing_private_ingress.api.fetch_dokploy_application_domains",
            side_effect=inventory,
        ) as read:
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
            self.assertIn(
                self.target_id.target_id,
                [call.kwargs["application_id"] for call in read.call_args_list],
            )
            self.domains.clear()
            lock_name = testing_event_execution_lock_name(
                dokploy_generic_web_provider_target_key(self.provider)
            )
            with self.store.testing_replacement_execution_lock(lock_name) as check:
                self.assertIsNotNone(check)
                self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
                self.assertEqual(self.run_ingress(adapter, "replacement-key").status, "target_busy")
            resolved = self.run_ingress(adapter)
            self.assertEqual(resolved.status, "completed")
            self.assertTrue(
                cast(dict[str, object], resolved.response_payload["result"])["replan_required"]
            )
        self.assertEqual(self.client.proxy_hosts, [])

    def test_uncertain_absence_and_legacy_receipt_keep_original_hold(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        provider_call = self.dokploy_request

        def reject_domain(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/domain.create":
                raise ValueError("domain rejected")
            return provider_call(**kwargs)

        with patch(
            "control_plane.testing_private_ingress.api.dokploy_request", side_effect=reject_domain
        ):
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"runtime_port": self.profile.runtime_port + 1})
        )
        with patch(
            "control_plane.testing_private_ingress.api.fetch_dokploy_application_domains",
            side_effect=RuntimeError("read failed"),
        ):
            self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        for name in ("*.example.test", "~^bridge", "bridge-testing.example.test"):
            with self.subTest(name=name):
                self.client.other_domains = (name,)
                self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        self.client.other_domains = ()
        audit = self.store.read_ingress_route_audit_record(
            TESTING_PRIVATE_INGRESS_AUDIT_PREFIX + str(digest)
        )
        self.store.write_ingress_route_audit_record(
            audit.model_copy(update={"testing_application_id": ""})
        )
        self.assertEqual(self.run_ingress(adapter).status, "reconcile_required")
        self.assertEqual(self.run_ingress(adapter, "different-key").status, "target_busy")
        self.assertEqual(self.domains, [])
        self.assertEqual(self.client.proxy_hosts, [])

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
