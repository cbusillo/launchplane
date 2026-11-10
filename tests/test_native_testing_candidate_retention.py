from datetime import datetime, timezone
from typing import Any
import unittest
from unittest.mock import Mock, PropertyMock

from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.testing_candidate_retention import candidate_record_refusals


class CandidateRetentionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = TestingCandidateAllocation(
            namespace=testing_candidate_namespace("odoo", "site", "failed-original"),
            product="odoo",
            context="site",
            operation_id="failed-original",
            intent_sha256="a" * 64,
            serving_target_id="selected",
            server_id="host",
            project_id="project",
            environment_id="environment",
            compose_id="failed-copy",
            created_at=datetime.now(timezone.utc),
        )
        self.operation: Any = Mock(
            product="odoo",
            context="site",
            instance="testing",
            status="fail",
            lease_owner="",
            lease_expires_at="",
            is_native_testing_rebuild=True,
        )

    def refusal(self, **kwargs: Any) -> tuple[str, ...]:
        return candidate_record_refusals(
            self.candidate,
            self.operation,
            product="odoo",
            context="site",
            receiver_operation_id="pending-original",
            selected_targets=(),
            **kwargs,
        )

    def test_active_original_foreign_instance_and_selected_copy_each_refuse(self) -> None:
        for field, value, reason in (
            ("status", "pending", "original_not_terminal"),
            ("status", "reconciliation_required", "original_not_terminal"),
            ("instance", "prod", "foreign_original"),
            ("product", "foreign", "foreign_original"),
            ("lease_expires_at", "unknown", "original_lease_unknown"),
        ):
            with self.subTest(field=field):
                original = getattr(self.operation, field)
                setattr(self.operation, field, value)
                self.assertIn(reason, self.refusal())
                setattr(self.operation, field, original)
        reasons = candidate_record_refusals(
            self.candidate,
            self.operation,
            product="odoo",
            context="site",
            receiver_operation_id="failed-original",
            selected_targets=("failed-copy",),
        )
        self.assertIn("receiver_candidate", reasons)
        self.assertIn("selected_target", reasons)


class CandidateRetentionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        from control_plane import testing_candidate_retention as retention
        from control_plane.contracts.retained_volume_sweep import NativeTestingSweepRequest
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from tests.test_testing_replacement_resources import TestingReplacementResourceFixture
        from unittest.mock import patch

        self.retention = retention
        fixture = TestingReplacementResourceFixture(self)
        self.store, self.root = fixture.store, fixture.root
        self.request = NativeTestingSweepRequest(
            kind="native-testing",
            product=fixture.profile.product,
            context=fixture.context,
            operation_id=fixture.operation.operation_id,
        )
        original = fixture.operation.model_copy(
            update={
                "operation_id": "failed-original",
                "idempotency_key": "failed-key",
                "status": "fail",
                "phase": "failed",
                "finished_at": fixture.operation.updated_at,
                "error_message": "Fabricated restore failed before serving",
                "request": fixture.operation.request.model_copy(
                    update={
                        "strategy": "replace-and-cutover",
                        "data_source_mode": "empty",
                        "allow_empty_data": True,
                        "artifact_id": "fixture-artifact",
                        "source_git_ref": "a" * 40,
                    }
                ),
            }
        )
        original = type(original).model_validate(original.model_dump())
        self.store.write_odoo_stable_target_replacement_operation_record(original)
        self.candidate = TestingCandidateAllocation(
            namespace=testing_candidate_namespace(
                original.product, original.context, original.operation_id
            ),
            product=original.product,
            context=original.context,
            operation_id=original.operation_id,
            intent_sha256="a" * 64,
            serving_target_id="compose-testing",
            server_id="host",
            project_id="project",
            environment_id="environment",
            compose_id="failed-copy",
            created_at=datetime.now(timezone.utc),
        )
        with self.store._session_factory() as session:
            session.add(
                LaunchplaneTestingCandidateRow(
                    namespace=self.candidate.namespace,
                    payload=self.candidate.model_dump(mode="json"),
                )
            )
            session.commit()
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                provider_id="dokploy",
                context=self.candidate.namespace,
                instance="testing-candidate",
                target_category="compose",
                target_id="failed-copy",
                display_name="Fabricated failed candidate",
                updated_at=original.updated_at,
            )
        )
        self.observation: dict[str, Any] = {
            "volumes": [
                {
                    "role": "ODOO_DB_VOLUME",
                    "name": self.candidate.namespace + "-db",
                    "status": "observed",
                    "reason": "",
                    "identity": "b" * 64,
                    "engine_identity": "c" * 64,
                    "empty": True,
                    "boot_id": "boot",
                    "monotonic_deadline": 1000000000,
                }
            ],
            "capacity": [],
            "capacity_refusal": "receiver_generation_unavailable",
        }
        self.inspect = self.enterContext(
            patch.object(
                retention, "inspect_candidate_resources", return_value=(self.observation, "")
            )
        )
        self.enterContext(patch.object(retention, "_inventory_clear", return_value=True))
        self.host = self.enterContext(
            patch.object(retention, "_host_result", return_value=("eligible", "qualified"))
        )

    def plan(self) -> Any:
        return self.retention.build_native_testing_sweep(
            self.store, self.root, self.request, "trace"
        )[0]

    def test_original_records_survive_and_a_selected_target_loses_eligibility(self) -> None:
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        before = self.store.read_testing_candidate(self.candidate.namespace)
        self.assertEqual(self.plan().candidates[0].status, "eligible")
        self.assertEqual(self.store.read_testing_candidate(self.candidate.namespace), before)
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.request.context,
                instance="testing",
                target_id=self.candidate.compose_id,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        held = self.plan().candidates[0]
        self.assertEqual(held.status, "held")
        self.assertIn("selected_target", held.reasons)

    def test_foreign_selection_refuses_without_reading_a_provider(self) -> None:
        self.request = self.request.model_copy(update={"candidates": ("lp-testing-" + "f" * 32,)})
        with self.assertRaisesRegex(ValueError, "foreign or unknown"):
            self.plan()
        self.inspect.assert_not_called()

    def test_optional_http_mode_keeps_director_boundary_and_refuses_production(self) -> None:
        import asyncio
        from typing import cast
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
        from control_plane.http_routes.retained_volume_sweep import (
            RetainedVolumeSweepDependencies,
            register_retained_volume_sweep_route,
        )
        from control_plane.http_routes.support import ApiRouteRegistrar
        from control_plane.service_auth import TerminalAgentIdentity
        from tests.support.http import request

        def app(director: bool) -> FastAPI:
            application = FastAPI()

            def error(**kwargs: Any) -> HTTPException:
                return HTTPException(kwargs["status_code"], detail=kwargs)

            register_retained_volume_sweep_route(
                cast(ApiRouteRegistrar, cast(object, application)),
                dependencies=RetainedVolumeSweepDependencies(
                    read_identity=lambda: TerminalAgentIdentity(
                        "fixture", "test", director_agent=director
                    ),
                    get_record_store=lambda: self.store,
                    next_trace_id=lambda: "http-fixture",
                    authorization_allows=lambda **_: False,
                    http_error=error,
                    control_plane_root=self.root,
                    error_response_model=BaseModel,
                ),
            )
            return application

        payload = self.request.model_dump(mode="json")
        for director, instance, status in (
            (True, "testing", 200),
            (False, "testing", 403),
            (True, "prod", 422),
        ):
            with self.subTest(director=director, instance=instance):
                result = asyncio.run(
                    request(
                        app(director),
                        "POST",
                        "/v1/retained-volume-sweep",
                        payload={**payload, "instance": instance},
                    )
                )
                self.assertEqual(result.status_code, status, result.text)
                if status == 200:
                    self.assertEqual(
                        result.json()["candidates"][0]["operation_id"], self.candidate.operation_id
                    )

    def test_physical_sources_and_unqualified_nonempty_copy_are_never_removed(self) -> None:
        self.observation["volumes"][0].update(empty=False, reason="source_inputs_preserved")
        self.assertEqual(self.plan().candidates[0].resources[0].reason, "source_inputs_preserved")
        self.host.assert_not_called()

    def test_nonempty_unrecorded_copy_is_held(self) -> None:
        self.observation["volumes"][0]["empty"] = False
        self.assertEqual(
            self.plan().candidates[0].resources[0].reason, "only_needed_copy_unqualified"
        )
        self.host.assert_not_called()

    def test_free_space_changes_do_not_invalidate_reviewed_resource_ownership(self) -> None:
        self.request = self.request.model_copy(update={"candidates": (self.candidate.namespace,)})
        self.observation["volumes"][0]["free_bytes"] = 100
        before = self.plan()
        self.observation["volumes"][0]["free_bytes"] = 90
        after = self.plan()
        self.assertEqual(before.plan_sha256, after.plan_sha256)
        self.assertNotEqual(before.candidates[0].resources, after.candidates[0].resources)
        self.observation["volumes"][0]["identity"] = "d" * 64
        self.assertNotEqual(after.plan_sha256, self.plan().plan_sha256)

    def test_provider_stop_transition_preserves_custody_but_new_work_refuses(self) -> None:
        payload = {"composeId": self.candidate.compose_id, "composeStatus": "error"}
        before = self.retention.candidate_provider_snapshot(payload)
        payload["composeStatus"] = "idle"
        self.assertEqual(before, self.retention.candidate_provider_snapshot(payload))
        payload["composeId"] = "foreign"
        self.assertNotEqual(before, self.retention.candidate_provider_snapshot(payload))
        for status in ("running", "unknown", ""):
            payload["composeStatus"] = status
            with self.assertRaisesRegex(ValueError, "active or unknown"):
                self.retention.candidate_provider_snapshot(payload)

    def test_changed_selection_refuses_before_the_host_effect(self) -> None:
        from contextlib import nullcontext
        from unittest.mock import patch
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        self.request = self.request.model_copy(update={"candidates": (self.candidate.namespace,)})
        plan, private = self.retention.build_native_testing_sweep(
            self.store, self.root, self.request, "trace"
        )
        request = self.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.request.context,
                instance="testing",
                target_id=self.candidate.compose_id,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        self.host.reset_mock()
        with (
            patch.object(
                self.retention, "build_native_testing_sweep", return_value=(plan, private)
            ),
            patch.object(
                type(self.store),
                "database_dialect_name",
                new_callable=PropertyMock,
                return_value="postgresql",
            ),
            patch.object(
                self.store, "testing_candidate_retention_guard", return_value=nullcontext(Mock())
            ),
        ):
            with self.assertRaisesRegex(ValueError, "ownership or current references changed"):
                self.retention.execute_native_testing_sweep_request(
                    self.store,
                    self.root,
                    request,
                    actor="fixture-director",
                    key="changed-selection",
                    trace_id="trace",
                )
        self.host.assert_not_called()
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    def test_provider_read_error_holds_one_candidate_without_a_mutation_claim(self) -> None:
        from control_plane.dokploy.api import DokployRequestFailed

        self.inspect.side_effect = DokployRequestFailed(
            method="GET", path="/api/compose.one", detail="missing", status_code=404
        )
        candidate = self.plan().candidates[0]
        self.assertEqual(candidate.status, "held")
        self.host.assert_not_called()

    def test_uncertain_original_reconciles_without_repeating_removal(self) -> None:
        from contextlib import nullcontext
        from unittest.mock import patch
        from control_plane.provider_operations import ProviderMutationUnknownError

        self.request = self.request.model_copy(update={"candidates": (self.candidate.namespace,)})
        digest = self.plan().plan_sha256
        request = self.request.model_copy(update={"mode": "apply", "reviewed_plan_sha256": digest})
        removals = []
        uncertain = True

        def host(
            root: Any, binding: Any, mode: str, key: str, lease: Any = None, **kwargs: Any
        ) -> tuple[str, str]:
            if mode == "apply":
                lease.checkpoint_effect("volume_removal")
                removals.append(key)
                return "unknown", "effect_unknown"
            if mode == "readback":
                return ("unknown", "effect_unknown") if uncertain else ("absent", "removed")
            return "eligible", "qualified"

        self.host.side_effect = host
        with (
            patch.object(
                type(self.store),
                "database_dialect_name",
                new_callable=PropertyMock,
                return_value="postgresql",
            ),
            patch.object(
                self.store,
                "testing_candidate_retention_guard",
                side_effect=lambda _: nullcontext(Mock()),
            ),
        ):
            for _ in range(2):
                with self.assertRaises(ProviderMutationUnknownError):
                    self.retention.execute_native_testing_sweep_request(
                        self.store,
                        self.root,
                        request,
                        actor="fixture-director",
                        key="cleanup-original",
                        trace_id="trace",
                    )
            self.assertEqual(len(removals), 1)
            uncertain = False
            observed = self.retention.execute_native_testing_sweep_request(
                self.store,
                self.root,
                request.model_copy(update={"mode": "readback"}),
                actor="fixture-director",
                key="cleanup-original",
                trace_id="readback",
            )
            self.assertEqual(observed.candidates[0].status, "removed")
            self.assertEqual(len(removals), 1)
            self.assertIsNotNone(self.store.read_testing_candidate(self.candidate.namespace))
