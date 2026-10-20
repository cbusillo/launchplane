"""Original-generation certificate repair never reboots the bootstrap pipeline."""

from contextlib import nullcontext
from typing import Any
from unittest.mock import Mock, patch

from control_plane.odoo_testing_restore import generation_binding
from control_plane.provider_operations import ProviderMutationRejectedError
from control_plane.testing_bootstrap_route_repair import TestingBootstrapRouteRepair
from control_plane.testing_replacement_bootstrap import bootstrap_request_sha256
from tests.test_odoo_testing_restore import generation
from tests.test_testing_replacement_bootstrap import FreshTestingBootstrapScenario


class TestingBootstrapRouteRepairTests(FreshTestingBootstrapScenario):
    def setUp(self) -> None:
        self.seed("sqlite+pysqlite:///:memory:", initial_generation=True)
        self.patches()
        self.request = self.request.model_copy(update={"certificate_type": "letsencrypt"})
        self.generation = generation().model_copy(
            update={
                "product": self.profile.product,
                "context": self.fixture.context,
                "operation_id": self.request.operation_id,
                "domain_host": self.request.domain_host,
                "request_sha256": bootstrap_request_sha256(self.request),
                "target_id": "fresh-compose",
                "namespace": self.resources.namespace,
                "certificate_type": self.request.certificate_type,
                "reconcile_requested_at": self.profile.updated_at,
            }
        )
        self.enterContext(
            patch.object(
                self.store, "read_fresh_odoo_testing_generation", return_value=self.generation
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.read_current_testing_generation",
                return_value=self.generation,
            )
        )
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(update={"base_url": "https://" + self.request.domain_host})
                        if lane.instance == "testing"
                        else lane
                        for lane in self.profile.lanes
                    )
                }
            )
        )
        allocation = self.store.read_disposable_resource_preparation(self.request.operation_id)
        assert allocation is not None
        self.enterContext(
            patch.object(
                self.store,
                "read_disposable_resource_preparation",
                return_value=allocation.model_copy(
                    update={"initial_reconcile_requested_at": self.profile.updated_at}
                ),
            )
        )
        for method in (
            "testing_replacement_execution_lock",
            "testing_verification_lane_guard",
            "serialize_disposable_testing_resources",
        ):
            self.enterContext(
                patch.object(
                    self.store, method, side_effect=lambda *a, **kw: nullcontext(lambda: None)
                )
            )
        self.usage = self.enterContext(
            patch("control_plane.testing_bootstrap_route_repair.require_testing_generation_usage")
        )
        self.enterContext(
            patch(
                "control_plane.testing_bootstrap_route_repair._target_reservations", return_value=()
            )
        )
        self.enterContext(
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("provider.invalid", "fixture-token"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_compose_domains_for_target_setup",
                side_effect=lambda **kw: tuple(self.routes),
            )
        )
        self.route(
            domain_host=self.request.domain_host,
            compose_id=self.generation.target_id,
            runtime_port=self.profile.runtime_port,
            certificate_type="none",
        )
        self.routes[0]["domainType"] = "compose"

    @staticmethod
    def result(payload: dict[str, object] | None) -> dict[str, Any]:
        assert payload is not None
        result = payload["result"]
        assert isinstance(result, dict)
        return result

    def repair(self) -> TestingBootstrapRouteRepair:
        return TestingBootstrapRouteRepair(self.adapter())

    def write_route(self, **kwargs: Any) -> str:
        # Simulate the provider's acknowledged update of the existing row.
        self.routes[0]["certificateType"] = kwargs["payload"]["certificateType"]
        return str(self.routes[0]["domainId"])

    def test_repair_changes_only_certificate_and_preserves_original_receipts(self) -> None:
        original_hash = bootstrap_request_sha256(self.request)
        for mode in ("route-repair-dry-run", "route-repair-apply", "route-repair-readback"):
            self.assertEqual(
                bootstrap_request_sha256(self.request.model_copy(update={"mode": mode})),
                original_hash,
            )
        with self.assertRaises(ValueError):
            self.adapter().readback()
        repair = self.repair()
        plan = repair.plan()
        self.request = self.request.model_copy(
            update={"mode": "route-repair-apply", "reviewed_plan_sha256": plan["plan_sha256"]}
        )
        repair = self.repair()
        phases: list[str] = []
        lease = Mock(unsafe=True)
        lease.checkpoint_effect.side_effect = phases.append
        with (
            patch(
                "control_plane.testing_bootstrap_route_repair.dokploy_api.dokploy_request",
                side_effect=self.write_route,
            ) as write,
            patch.object(self.store, "request_fresh_testing_reconcile") as queue,
            patch.object(self.store, "write_fresh_odoo_testing_generation") as generation_write,
            patch.object(self.store, "write_product_public_hosts_bundle") as bundle,
        ):
            outcome = repair.apply("separate-repair-key", lease)
            self.assertEqual(
                self.result(outcome.response_payload)["plan_sha256"], plan["plan_sha256"]
            )
            self.assertTrue(self.adapter().readback()["applied"])
            self.assertTrue(repair.readback()["route_verified"])
            observed = repair.observe("separate-repair-key", phases[0], repair.reconciliation_key())
            self.assertEqual(observed.outcome, "present")
            self.assertEqual(
                self.result(observed.response_payload)["plan_sha256"], plan["plan_sha256"]
            )
            write.assert_called_once()
            self.assertEqual(
                write.call_args.kwargs["payload"]["certificateType"],
                self.generation.certificate_type,
            )
            queue.assert_not_called()
            generation_write.assert_not_called()
            bundle.assert_not_called()
        self.assertEqual(plan["generation_binding_sha256"], generation_binding(self.generation))
        self.assertFalse(self.result(outcome.response_payload)["runtime_verified"])

    def test_stale_review_missing_route_other_drift_and_protected_use_refuse_without_effect(
        self,
    ) -> None:
        plan = self.repair().plan()
        self.request = self.request.model_copy(update={"reviewed_plan_sha256": plan["plan_sha256"]})
        for fault in ("stale", "missing", "port", "usage", "busy", "held-provider"):
            with (
                self.subTest(fault=fault),
                patch(
                    "control_plane.testing_bootstrap_route_repair.dokploy_api.dokploy_request"
                ) as write,
            ):
                from contextlib import ExitStack

                with ExitStack() as stack:
                    if fault == "stale":
                        stack.enter_context(
                            patch.object(
                                self.store,
                                "read_fresh_odoo_testing_generation",
                                return_value=self.generation.model_copy(
                                    update={"non_use_tracking_started_at": "changed"}
                                ),
                            )
                        )
                    elif fault == "missing":
                        stack.enter_context(
                            patch(
                                "control_plane.dokploy_target_setup_http.fetch_dokploy_compose_domains_for_target_setup",
                                return_value=(),
                            )
                        )
                    elif fault == "port":
                        self.routes[0]["port"] += 1
                    elif fault == "usage":
                        stack.enter_context(
                            patch(
                                "control_plane.testing_bootstrap_route_repair.require_testing_generation_usage",
                                side_effect=ValueError("protected"),
                            )
                        )
                    elif fault == "busy":
                        stack.enter_context(
                            patch.object(
                                self.store,
                                "testing_replacement_execution_lock",
                                side_effect=lambda *a, **kw: nullcontext(None),
                            )
                        )
                    else:
                        stack.enter_context(
                            patch(
                                "control_plane.testing_bootstrap_route_repair._target_reservations",
                                return_value=(object(),),
                            )
                        )
                    with self.assertRaises(ProviderMutationRejectedError):
                        self.repair().apply("repair-key", Mock())
                if fault == "port":
                    self.routes[0]["port"] -= 1
                write.assert_not_called()

    def test_uncertain_write_keeps_unknown_and_never_repeats(self) -> None:
        plan = self.repair().plan()
        self.request = self.request.model_copy(update={"reviewed_plan_sha256": plan["plan_sha256"]})
        lease = Mock(unsafe=True)
        phases: list[str] = []
        lease.checkpoint_effect.side_effect = phases.append
        with patch(
            "control_plane.testing_bootstrap_route_repair.dokploy_api.dokploy_request",
            side_effect=OSError("lost reply"),
        ) as write:
            with self.assertRaises(OSError):
                self.repair().apply("repair-key", lease)
            self.assertEqual(
                self.repair()
                .observe("repair-key", phases[0], self.repair().reconciliation_key())
                .outcome,
                "unknown",
            )
            write.assert_called_once()

    def test_lost_acknowledgment_adopts_only_the_same_generation_and_route(self) -> None:
        plan = self.repair().plan()
        self.request = self.request.model_copy(update={"reviewed_plan_sha256": plan["plan_sha256"]})
        phases: list[str] = []
        lease = Mock(unsafe=True)
        lease.checkpoint_effect.side_effect = phases.append

        def lost_after_write(**kwargs: Any) -> None:
            self.write_route(**kwargs)
            self.routes[0]["updatedAt"] = "provider-observation-metadata"
            raise OSError("lost acknowledgment")

        with patch(
            "control_plane.testing_bootstrap_route_repair.dokploy_api.dokploy_request",
            side_effect=lost_after_write,
        ) as write:
            with self.assertRaises(OSError):
                self.repair().apply("repair-key", lease)
            observed = self.repair().observe("repair-key", phases[0], "retained")
            self.assertEqual(observed.outcome, "present")
            self.routes[0]["domainId"] = "different-existing-row"
            self.assertEqual(
                self.repair().observe("repair-key", phases[0], "retained").outcome, "unknown"
            )
            write.assert_called_once()
