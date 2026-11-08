from datetime import datetime, timedelta, timezone
from contextlib import contextmanager, nullcontext
from collections.abc import Iterator
from typing import Any
import unittest
from unittest.mock import patch

import click
from sqlalchemy import delete, select

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord, ScalarValue
from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane.product_secret_copy import ProductSecretCopyFrom
from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyEnvelope,
    issue_odoo_preview_apply_plan,
)
from control_plane.workflows.odoo_preview_runtime import ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplanePreviewRow,
    LaunchplanePreviewGenerationRow,
    LaunchplaneIdempotencyRow,
    ORIGINAL_MUTATION_INTENT_ROUTE,
)
from tests.support.auth import scoped_agent_transport
from tests.test_director_bounded_writes import DirectorBoundedWritesFixture
from tests.test_odoo_preview_apply_execution import _issued_plan


class UnrecordedPreviewRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.prepare_fixture()

    async def test_conditional_http_plan_does_not_block_original_bound_recovery(self) -> None:
        from control_plane import odoo_preview_apply_http as preview_http

        candidate = _issued_plan(self.request)
        with patch.object(preview_http, "_build_odoo_preview_apply_inputs", return_value=candidate):
            blocked = await self.fixture.post(
                "/v1/drivers/odoo/preview-apply-inputs",
                {
                    "product": self.profile.product,
                    "inputs": candidate.plan_request.model_dump(mode="json"),
                },
                "ordinary-unrecorded-plan",
            )
            self.assertEqual(blocked.status_code, 202, blocked.text)
            self.assertEqual(blocked.json()["result"]["status"], "blocked")
            response = await self.fixture.post(
                "/v1/drivers/odoo/preview-apply-inputs",
                {
                    "product": self.profile.product,
                    "inputs": candidate.plan_request.model_dump(mode="json"),
                    "unrecorded_original_reservation_id": self.original.record_id,
                },
                "original-bound-plan",
            )
        self.assertEqual(response.status_code, 202, response.text)
        planned = response.json()["result"]
        self.assertEqual(planned["status"], "ready")
        self.assertNotIn("blockers", planned)
        self.fixture.plan_id = planned["plan_provenance"]["plan_id"]
        with patch(
            "control_plane.http_app.execute_odoo_preview_apply_result",
            return_value=self.passing_result(),
        ) as execute:
            applied = await self.fixture.preview_apply()
        self.assertEqual(applied.status_code, 202, applied.text)
        execute.assert_called_once()
        self.assert_original_preserved()

    def prepare_fixture(
        self,
        *,
        created: bool = True,
        original_source: ProductSecretCopyFrom | None = None,
        original_snapshot: CleanTestingSnapshot | None = None,
        capture_source_decision: bool = True,
    ) -> None:
        self.fixture = DirectorBoundedWritesFixture(self, record_source_history=False)
        self.store = self.fixture.store
        self.profile = self.fixture.profile
        with self.store._session_factory() as session:
            session.execute(delete(LaunchplanePreviewGenerationRow))
            session.execute(delete(LaunchplanePreviewRow))
            session.commit()
        self.request = OdooPreviewApplyEnvelope.model_validate(self.fixture.payload)
        original_request = self.request.model_copy(deep=True)
        if created:
            original_request.apply.dry_run_plan.compose_ref = "${created.composeId:preview}"
        original_request.apply.dry_run_plan.clean_snapshot = original_snapshot
        original_plan = _issued_plan(original_request)
        original_plan.plan_request.fishbowl_source = original_source
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.fixture.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=original_request,
            issued_plan=issue_odoo_preview_apply_plan(
                result=original_plan, plan_id="original-failed-preview"
            ),
            reservation_scope="original-preview",
            idempotency_key="original-failed-preview",
            request_fingerprint="original-request",
            trace_id="original-preview",
        )
        if capture_source_decision and self.profile.preview.data_transport_mode == "none":
            operation.adapter.capture_source_decision()
        original = self.store.reserve_mutation(
            scope=operation.reservation_scope,
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=operation.idempotency_key,
            request_fingerprint=operation.request_fingerprint,
            lease_owner="original-executor",
            reconciliation_key=operation.adapter.reconciliation_key(),
            provider_target_key=operation.adapter.target_key(),
            original_intent=operation.adapter.original_intent(),
        ).record
        assert original is not None
        self.original = original.model_copy(
            update={
                "state": "completed",
                "response_status_code": 502,
                "response_trace_id": "failed-original",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "response_payload": {"result": {"status": "fail", "compose_id": self.target_id}},
            }
        )
        completed = self.store.complete_mutation_reservation(completion=self.original)
        self.assertEqual(completed.status, "completed")
        assert completed.record is not None
        self.original = completed.record
        self.retained = self.store.read_original_mutation_intent(self.original)
        self.fixture.payload["unrecorded_original_reservation_id"] = self.original.record_id

    def replace_original(self, **changes: Any) -> None:
        assert self.original is not None
        self.original = type(self.original).model_validate(
            {**self.original.model_dump(), **changes}
        )
        with self.store._session_factory() as session:
            row = session.scalar(
                select(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.record_id == self.original.record_id
                )
            )
            assert row is not None
            self.store._sync_idempotency_row(row, self.original)
            session.commit()

    def hold_original(self, *, expired: bool = True) -> None:
        expiry = datetime.now(timezone.utc) - timedelta(
            seconds=ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS + 60 if expired else 0
        )
        self.replace_original(
            state="reconcile_required",
            updated_at=(expiry - timedelta(seconds=1)).isoformat(),
            lease_owner="original-executor",
            lease_expires_at=expiry.isoformat(),
            provider_effect_phase="post_deploy_schedule_trigger",
            provider_effect_started_at=self.original.created_at,
            response_status_code=None,
            response_trace_id="",
            recorded_at="",
            response_payload={},
        )

    @property
    def target_id(self) -> str:
        return str(self.request.apply.dry_run_plan.compose_ref)

    def assert_original_preserved(self) -> None:
        assert self.original is not None
        self.assertEqual(
            self.store.read_idempotency_record_by_id(self.original.record_id), self.original
        )
        self.assertEqual(self.store.read_original_mutation_intent(self.original), self.retained)

    def passing_result(self) -> dict[str, object]:
        plan = self.request.apply.dry_run_plan
        return {
            "status": "pass",
            "operation": "refresh",
            "product": self.profile.product,
            "repository": self.profile.repository,
            "preview_slug": plan.preview_slug,
            "preview_url": plan.preview_url,
            "domain_host": plan.domain_host,
            "compose_id": plan.compose_ref,
            "compose_name": plan.compose_name,
        }

    async def test_refresh_uses_normal_collector_then_publishes_only_real_success_and_replays(
        self,
    ) -> None:
        def execute(**kwargs: Any) -> dict[str, object]:
            self.assertTrue(kwargs["qualify_existing_preview"])
            self.assertEqual(self.store.list_preview_records(), ())
            reservation = self.store.read_idempotency_record_by_id(kwargs["deployment_record_id"])
            assert reservation is not None
            decision = kwargs["expected_source_decision"]
            self.assertEqual(
                self.store.read_original_mutation_intent(reservation)["effective_source"],
                decision.model_dump(mode="json"),
            )
            kwargs["provider_effect_checkpoint"]("fixture-qualified-effect")
            return self.passing_result()

        with patch(
            "control_plane.http_app.execute_odoo_preview_apply_result", side_effect=execute
        ) as apply:
            accepted = await self.fixture.preview_apply()
            self.assertEqual(accepted.status_code, 202, accepted.text)
            replay = await self.fixture.preview_apply()
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertTrue(replay.json()["replayed"])
            self.fixture.payload["unrecorded_original_reservation_id"] = (
                "mutation-reservation-" + "a" * 64
            )
            conflict = await self.fixture.preview_apply()
            self.assertEqual(conflict.status_code, 409, conflict.text)
            apply.assert_called_once()
        preview = self.store.read_preview_record(self.fixture.preview.preview_id)
        self.assertEqual(preview.state, "active")
        self.assertTrue(preview.serving_generation_id)
        self.assert_original_preserved()

    async def test_without_explicit_original_binding_the_missing_lifecycle_still_refuses(
        self,
    ) -> None:
        self.fixture.payload.pop("unrecorded_original_reservation_id")
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply:
            response = await self.fixture.preview_apply()
        self.assertEqual(response.status_code, 409, response.text)
        apply.assert_not_called()
        self.assert_original_preserved()

    async def test_driver_continuation_uses_the_resolved_source_before_dispatch(self) -> None:
        from control_plane.workflows.odoo_preview_runtime import ODOO_PREVIEW_REQUIRED_ENV_KEYS

        for upstream in (False, True):
            with self.subTest(upstream=upstream):
                self.prepare_fixture()
                self.profile = self.profile.model_copy(
                    update={
                        "preview": self.profile.preview.model_copy(
                            update={"data_transport_mode": "driver"}
                        )
                    }
                )
                self.fixture.profile = self.profile
                self.store.write_product_profile_record(self.profile)
                self.fixture.issue_preview()
                values: dict[str, ScalarValue] = {
                    key: f"fixture-{key.lower()}" for key in ODOO_PREVIEW_REQUIRED_ENV_KEYS
                }
                if upstream:
                    values["ODOO_UPSTREAM_HOST"] = "source.example.test"
                self.store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context=self.profile.preview.context,
                        instance=self.profile.preview.template_instance,
                        env=values,
                        updated_at=self.profile.updated_at,
                        source_label="fixture",
                    )
                )
                with patch(
                    "control_plane.http_app.execute_odoo_preview_apply_result",
                    return_value=self.passing_result(),
                ) as apply:
                    response = await self.fixture.preview_apply()
                self.assertEqual(response.status_code, 409 if upstream else 202, response.text)
                if upstream:
                    self.assertEqual(
                        response.json()["error"]["code"], "odoo_preview_source_history_unknown"
                    )
                    apply.assert_not_called()
                    self.assert_original_preserved()
                else:
                    apply.assert_called_once()
                    self.assertEqual(
                        apply.call_args.kwargs["expected_source_decision"].source, "none"
                    )

    async def test_current_none_mode_cannot_erase_unknown_or_restored_original_history(
        self,
    ) -> None:
        from tests.support.profiles import _odoo_preview_profile_payload

        def driver_profile() -> dict[str, Any]:
            payload = _odoo_preview_profile_payload()
            preview = payload["preview"]
            assert isinstance(preview, dict)
            preview["data_transport_mode"] = "driver"
            return payload

        for recorded in (False, True):
            for history in (
                None,
                {"upstream_restore_requested": "false"},
                {"upstream_restore_requested": "true", "odoo_restore_completed": "true"},
                {"upstream_restore_requested": "false", "odoo_restore_completed": "false"},
            ):
                with self.subTest(recorded=recorded, history=history):
                    with patch(
                        "tests.test_director_bounded_writes._odoo_preview_profile_payload",
                        side_effect=driver_profile,
                    ):
                        self.prepare_fixture(created=False)
                    self.profile = self.profile.model_copy(
                        update={
                            "preview": self.profile.preview.model_copy(
                                update={"data_transport_mode": "none"}
                            )
                        }
                    )
                    self.fixture.profile = self.profile
                    self.store.write_product_profile_record(self.profile)
                    self.fixture.issue_preview()
                    result: dict[str, object] = {
                        "status": "pass" if recorded else "fail",
                        "compose_id": self.target_id,
                    }
                    if history is not None:
                        result["module_install_update_evidence"] = history
                    self.replace_original(response_payload={"result": result})
                    if recorded:
                        self.store.write_preview_record(self.fixture.preview)
                        self.fixture.payload.pop("unrecorded_original_reservation_id")
                    with patch(
                        "control_plane.http_app.execute_odoo_preview_apply_result"
                    ) as execute:
                        response = await self.fixture.preview_apply()
                    self.assertEqual(response.status_code, 409, response.text)
                    execute.assert_not_called()
                    self.assert_original_preserved()

    def test_independent_recipient_ignores_only_proven_unallocated_or_disjoint_originals(
        self,
    ) -> None:
        from control_plane.preview_source import require_source_free_preview_history

        self.prepare_fixture(capture_source_decision=False)
        self.fixture.record_source_history()

        def require_history() -> None:
            require_source_free_preview_history(
                self.store,
                profile=self.profile,
                issued_plan=_issued_plan(self.request),
                operation_record_id="new-operation",
            )

        with self.assertRaises(ValueError):
            require_history()
        self.replace_original(
            response_payload={"result": {"status": "blocked", "provider_effect_attempted": False}}
        )
        require_history()
        self.assert_original_preserved()
        assert self.original is not None
        self.replace_original(
            provider_effect_started_at=self.original.created_at,
            provider_effect_phase="compose_create",
        )
        with self.assertRaises(ValueError):
            require_history()
        self.replace_original(provider_effect_started_at="", provider_effect_phase="")
        self.replace_original(response_payload={"result": {"status": "fail"}})
        with self.assertRaises(ValueError):
            require_history()
        self.replace_original(
            response_payload={"result": {"status": "fail", "compose_id": "disjoint-recipient"}}
        )
        require_history()
        self.assert_original_preserved()

    async def test_existing_target_failed_original_qualifies_without_lifecycle(self) -> None:
        self.prepare_fixture(created=False)
        with patch(
            "control_plane.http_app.execute_odoo_preview_apply_result",
            return_value={"status": "blocked"},
        ) as apply:
            response = await self.fixture.preview_apply()
        self.assertEqual(response.status_code, 202, response.text)
        apply.assert_called_once()
        self.assertEqual(self.store.list_preview_records(), ())
        self.assert_original_preserved()

    async def test_expired_held_original_continues_only_through_guarded_supersession(self) -> None:
        for created in (False, True):
            with self.subTest(created=created):
                self.prepare_fixture(created=created)
                self.hold_original()

                def execute(**kwargs: Any) -> dict[str, object]:
                    self.assertTrue(kwargs["qualify_existing_preview"])
                    self.assertFalse(self.store.list_preview_records())
                    settled = self.store.read_idempotency_record_by_id(self.original.record_id)
                    assert settled is not None
                    self.assertEqual(settled.state, "completed")
                    self.assertEqual(settled.response_payload["result"]["status"], "fail")
                    self.assertEqual(
                        self.store.read_original_mutation_intent(settled), self.retained
                    )
                    kwargs["provider_effect_checkpoint"]("fixture-qualified-effect")
                    return self.passing_result()

                with (
                    patch(
                        "control_plane.http_app.execute_odoo_preview_apply_result",
                        side_effect=execute,
                    ) as apply,
                    patch(
                        "control_plane.http_app.odoo_preview_supersession_is_quiescent",
                        return_value=True,
                    ),
                    patch(
                        "control_plane.workflows.odoo_preview_runtime.resolve_original_preview_compose_id",
                        return_value=self.target_id,
                    ),
                ):
                    accepted = await self.fixture.preview_apply()
                    self.assertEqual(accepted.status_code, 202, accepted.text)
                    replayed = await self.fixture.preview_apply()
                    self.assertEqual(replayed.status_code, 202, replayed.text)
                    self.assertTrue(replayed.json()["replayed"])
                    apply.assert_called_once()

    async def test_held_original_grace_quiescence_target_and_protection_keep_the_fence(
        self,
    ) -> None:
        @contextmanager
        def busy(*_args: object, **_kwargs: object) -> Iterator[None]:
            yield None

        for refusal in (
            "grace",
            "quiescence",
            "target",
            "inventory",
            "executor",
            "in_use",
            "only_copy",
            "shared",
        ):
            with self.subTest(refusal=refusal):
                self.prepare_fixture()
                self.hold_original(expired=refusal != "grace")
                if refusal in {"in_use", "only_copy", "shared"}:
                    field = {
                        "in_use": "usage",
                        "only_copy": "data",
                        "shared": "production_resources",
                    }[refusal]
                    self.fixture.safety(**{field: refusal})
                with (
                    patch.object(self.store, "testing_replacement_execution_lock", side_effect=busy)
                    if refusal == "executor"
                    else nullcontext(),
                    patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply,
                    patch(
                        "control_plane.http_app.odoo_preview_supersession_is_quiescent",
                        return_value=refusal != "quiescence",
                    ),
                    patch(
                        "control_plane.workflows.odoo_preview_runtime.resolve_original_preview_compose_id",
                        return_value="different-target" if refusal == "target" else self.target_id,
                        side_effect=click.ClickException("Unavailable original inventory")
                        if refusal == "inventory"
                        else None,
                    ),
                ):
                    refused = await self.fixture.preview_apply()
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertEqual(
                    refused.json()["error"]["code"],
                    "mutation_reconciliation_required"
                    if refusal == "quiescence"
                    else "mutation_in_progress"
                    if refusal == "executor"
                    else "disposable_lane_safety_unknown",
                )
                apply.assert_not_called()
                self.assertFalse(self.store.list_preview_records())
                self.assert_original_preserved()

    async def test_legacy_continuation_observes_without_reexecution_or_inventing_source(
        self,
    ) -> None:
        def execute(**kwargs: Any) -> dict[str, object]:
            reservation = self.store.read_idempotency_record_by_id(kwargs["deployment_record_id"])
            assert reservation is not None
            self.source_intent = self.store.read_original_mutation_intent(reservation)
            self.assertEqual(
                self.source_intent["effective_source"],
                kwargs["expected_source_decision"].model_dump(mode="json"),
            )
            kwargs["provider_effect_checkpoint"]("fixture-uncertain-effect")
            return {"status": "fail", "provider_effect_attempted": True}

        with patch(
            "control_plane.http_app.execute_odoo_preview_apply_result", side_effect=execute
        ) as apply:
            first = await self.fixture.preview_apply()
            self.assertEqual(first.status_code, 409, first.text)
            operation_id = apply.call_args.kwargs["deployment_record_id"]
            reservation = self.store.read_idempotency_record_by_id(operation_id)
            assert reservation is not None
            self.source_intent.pop("effective_source")
            with self.store._session_factory() as session:
                receipt = session.scalar(
                    select(LaunchplaneIdempotencyRow).where(
                        LaunchplaneIdempotencyRow.scope == reservation.scope,
                        LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                        LaunchplaneIdempotencyRow.idempotency_key == operation_id,
                    )
                )
                assert receipt is not None
                payload = dict(receipt.payload)
                payload["response_payload"] = {
                    **payload["response_payload"],
                    "original_intent": self.source_intent,
                }
                receipt.payload = payload
                session.commit()
            with patch(
                "control_plane.http_app.observe_odoo_preview_apply_result",
                side_effect=[
                    ("unknown", None, False),
                    ("unknown", None, False),
                    ("present", {"status": "fail"}, False),
                ],
            ) as observe:
                for _ in range(2):
                    uncertain = await self.fixture.preview_apply()
                    self.assertEqual(uncertain.status_code, 409, uncertain.text)
                settled = await self.fixture.preview_apply()
                self.assertEqual(settled.status_code, 502, settled.text)
                repeated = await self.fixture.preview_apply()
                self.assertEqual(repeated.status_code, 502, repeated.text)
                self.assertEqual(observe.call_count, 3)
            reservation = self.store.read_idempotency_record_by_id(operation_id)
            assert reservation is not None
            self.assertEqual(
                self.store.read_original_mutation_intent(reservation), self.source_intent
            )
            apply.assert_called_once()
        self.assertEqual(self.store.list_preview_records(), ())
        self.assertEqual(self.store.list_preview_generation_records(), ())
        self.assert_original_preserved()

    async def test_protection_or_configuration_drift_rechecks_before_and_after_first_effect(
        self,
    ) -> None:
        for after_effect in (False, True):
            for protection in (True, False):
                with self.subTest(after_effect=after_effect, protection=protection):
                    self.prepare_fixture()

                    def execute(**kwargs: Any) -> None:
                        if after_effect:
                            kwargs["provider_effect_checkpoint"]("fixture-first-effect")
                        if protection:
                            self.fixture.safety(usage="in_use")
                        else:
                            self.store.write_runtime_environment_record(
                                RuntimeEnvironmentRecord(
                                    scope="instance",
                                    context=self.profile.preview.context,
                                    instance="preview",
                                    env={"ODOO_PROXY_MODE": str(after_effect)},
                                    updated_at=datetime.now(timezone.utc).isoformat(),
                                )
                            )
                        kwargs["provider_effect_checkpoint"]("fixture-next-effect")
                        self.fail("Changed authority must fence dispatch")

                    with patch(
                        "control_plane.http_app.execute_odoo_preview_apply_result",
                        side_effect=execute,
                    ):
                        response = await self.fixture.preview_apply()
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "mutation_reconciliation_required"
                        if after_effect
                        else "disposable_lane_safety_unknown",
                    )
                    self.assertEqual(self.store.list_preview_records(), ())
                    self.assert_original_preserved()

    async def test_recorded_protection_blocks_even_after_expiry(self) -> None:
        for field, value in (
            ("usage", "in_use"),
            ("data", "only_copy"),
            ("production_resources", "shared"),
            ("production_resources", "separation_ready"),
        ):
            with self.subTest(field=field, value=value):
                now = datetime.now(timezone.utc)
                self.fixture.safety(
                    **{field: value},
                    observed_at=now - timedelta(minutes=10),
                    expires_at=now - timedelta(minutes=5),
                )
                with patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply:
                    refused = await self.fixture.preview_apply()
                self.assertEqual(refused.status_code, 409, refused.text)
                apply.assert_not_called()
        self.assert_original_preserved()

    async def test_foreign_or_changed_target_and_newer_lifecycle_refuse_before_driver(self) -> None:
        for target_kind in ("recorded", "physical", "different"):
            with self.subTest(target_kind=target_kind):
                if target_kind == "physical":
                    record = ProviderTargetRecord(
                        context="foreign",
                        instance="prod",
                        provider_id="dokploy",
                        target_category="compose",
                        target_id=self.target_id,
                        display_name="foreign",
                        updated_at=self.profile.updated_at,
                    )
                    manager = patch.object(
                        self.store, "list_physical_provider_target_records", return_value=(record,)
                    )
                else:
                    record_id = DokployTargetIdRecord(
                        context=self.profile.preview.context
                        if target_kind == "different"
                        else "foreign",
                        instance=self.request.apply.dry_run_plan.preview_slug
                        if target_kind == "different"
                        else "prod",
                        target_id="other-target" if target_kind == "different" else self.target_id,
                        updated_at=self.profile.updated_at,
                    )
                    manager = patch.object(
                        self.store, "list_dokploy_target_id_records", return_value=(record_id,)
                    )
                with (
                    manager,
                    patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply,
                ):
                    refused = await self.fixture.preview_apply()
                self.assertEqual(refused.status_code, 409, refused.text)
                apply.assert_not_called()
        self.store.write_preview_record(self.fixture.preview)
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply:
            refused = await self.fixture.preview_apply()
        self.assertEqual(refused.status_code, 409, refused.text)
        apply.assert_not_called()

    async def test_staff_hold_and_scoped_caller_refuse(self) -> None:
        target = DokployTargetRecord.model_validate(
            {
                "context": self.profile.preview.context,
                "instance": self.request.apply.dry_run_plan.preview_slug,
                "target_type": "compose",
                "target_name": "preview",
                "updated_at": self.profile.updated_at,
                "policies": {
                    "staff_testing_hold": DokployTargetStaffTestingHold(
                        reason="staff is testing"
                    ).model_dump(mode="json")
                },
            }
        )
        self.store.write_dokploy_target_record(target)
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply:
            refused = await self.fixture.preview_apply()
            self.assertEqual(refused.status_code, 409, refused.text)
            with scoped_agent_transport():
                scoped = await self.fixture.preview_apply()
            self.assertEqual(scoped.status_code, 403, scoped.text)
            apply.assert_not_called()

    async def test_inflight_or_unqualified_original_and_executor_refuse(self) -> None:
        @contextmanager
        def busy(*_args: object, **_kwargs: object) -> Iterator[None]:
            yield None

        for scenario in (
            "running",
            "reconcile_required",
            "successful",
            "missing_target",
            "contradictory_target",
            "other_original",
            "executor",
        ):
            with self.subTest(scenario=scenario):
                self.prepare_fixture(created=scenario != "contradictory_target")
                if scenario in {"running", "reconcile_required"}:
                    self.replace_original(
                        state=scenario,
                        lease_owner="active-executor",
                        lease_expires_at=(
                            datetime.now(timezone.utc) + timedelta(minutes=5)
                        ).isoformat(),
                        response_status_code=None,
                        response_trace_id="",
                        recorded_at="",
                        response_payload={},
                    )
                elif scenario == "successful":
                    self.replace_original(
                        response_payload={
                            "result": {"status": "pass", "compose_id": self.target_id}
                        }
                    )
                elif scenario == "missing_target":
                    self.replace_original(response_payload={"result": {"status": "fail"}})
                elif scenario == "contradictory_target":
                    self.replace_original(
                        response_payload={
                            "result": {"status": "fail", "compose_id": "different-target"}
                        }
                    )
                elif scenario == "other_original":
                    assert self.original is not None
                    self.store.reserve_mutation(
                        scope="other-preview",
                        route_path=ODOO_PREVIEW_APPLY_ROUTE,
                        idempotency_key="other-original",
                        request_fingerprint="other",
                        lease_owner="other",
                        reconciliation_key=self.original.reconciliation_key,
                        provider_target_key=self.original.provider_target_key,
                    )
                manager = (
                    patch.object(self.store, "testing_replacement_execution_lock", side_effect=busy)
                    if scenario == "executor"
                    else nullcontext()
                )
                with (
                    manager,
                    patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply,
                ):
                    refused = await self.fixture.preview_apply()
                self.assertEqual(refused.status_code, 409, refused.text)
                apply.assert_not_called()
                self.assertEqual(self.store.list_preview_records(), ())
                self.assert_original_preserved()

    async def test_changed_plan_and_shared_profile_ownership_refuse(self) -> None:
        for scenario in (
            "environment_id",
            "compose_ref",
            "compose_name",
            "foreign_context",
            "historical_context",
            "stable_collision",
            "profile_drift",
            "snapshot",
        ):
            with self.subTest(scenario=scenario):
                self.prepare_fixture()
                if scenario in {"environment_id", "compose_ref", "compose_name"}:
                    self.fixture.payload["apply"]["dry_run_plan"][scenario] = "different"
                    self.fixture.issue_preview()
                elif scenario == "profile_drift":
                    self.store.write_product_profile_record(
                        self.profile.model_copy(update={"repository": "example/changed"})
                    )
                elif scenario == "snapshot":
                    preview = self.profile.preview.model_copy(
                        update={"clean_testing_snapshot": True}
                    )
                    self.store.write_product_profile_record(
                        self.profile.model_copy(update={"preview": preview})
                    )
                else:
                    other = self.profile.model_copy(deep=True)
                    other.product = "foreign-product"
                    other.repository = "example/foreign"
                    if scenario == "stable_collision":
                        other.preview.context = "foreign-context"
                        other.lanes = (
                            other.lanes[0].model_copy(
                                update={
                                    "context": self.profile.preview.context,
                                    "instance": self.request.apply.dry_run_plan.preview_slug,
                                }
                            ),
                        )
                    elif scenario == "historical_context":
                        other.preview.context = "foreign-context"
                        other.historical_contexts = (self.profile.preview.context,)
                    self.store.write_product_profile_record(other)
                with patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply:
                    refused = await self.fixture.preview_apply()
                self.assertEqual(refused.status_code, 409, refused.text)
                apply.assert_not_called()
                self.assert_original_preserved()

    async def test_recovery_field_cannot_select_destroy(self) -> None:
        self.fixture.payload["apply"]["dry_run_plan"]["operation"] = "destroy"
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply:
            refused = await self.fixture.preview_apply()
        self.assertEqual(refused.status_code, 400, refused.text)
        apply.assert_not_called()

    async def test_recorded_preview_also_refuses_a_physical_target_alias(self) -> None:
        fixture = DirectorBoundedWritesFixture(self)
        store: PostgresRecordStore = fixture.store
        alias = ProviderTargetRecord(
            context="foreign",
            instance="prod",
            provider_id="dokploy",
            target_category="compose",
            target_id=fixture.payload["apply"]["dry_run_plan"]["compose_ref"],
            display_name="foreign",
            updated_at=fixture.profile.updated_at,
        )
        with (
            patch.object(store, "list_physical_provider_target_records", return_value=(alias,)),
            patch(
                "control_plane.http_app.execute_odoo_preview_apply_result",
                return_value={"status": "blocked"},
            ) as apply,
        ):
            refused = await fixture.preview_apply()
        self.assertEqual(refused.status_code, 409, refused.text)
        apply.assert_not_called()

    async def test_orphan_generation_prevents_unrecorded_qualification(self) -> None:
        from control_plane.contracts.preview_generation_record import (
            PreviewGenerationRecord,
            PreviewPullRequestSummary,
        )

        generation = PreviewGenerationRecord(
            generation_id="newer-generation",
            preview_id=self.fixture.preview.preview_id,
            sequence=1,
            state="failed",
            requested_reason="interrupted lifecycle publication",
            requested_at=datetime.now(timezone.utc).isoformat(),
            resolved_manifest_fingerprint="newer-manifest",
            anchor_summary=PreviewPullRequestSummary(
                repo=self.fixture.preview.anchor_repo,
                pr_number=self.fixture.preview.anchor_pr_number,
                head_sha="newer-head",
                pr_url=self.fixture.preview.anchor_pr_url,
            ),
        )
        self.store.write_preview_generation_record(generation)
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply:
            refused = await self.fixture.preview_apply()
        self.assertEqual(refused.status_code, 409, refused.text)
        apply.assert_not_called()
        self.assertEqual(self.store.list_preview_records(), ())

    async def test_expired_issued_plan_refuses_original_bound_refresh(self) -> None:
        expired = issue_odoo_preview_apply_plan(
            result=_issued_plan(self.request),
            plan_id=self.fixture.plan_id,
            issued_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        with patch(
            "tests.test_director_bounded_writes.issue_odoo_preview_apply_plan", return_value=expired
        ):
            self.fixture.issue_preview()
        with patch(
            "control_plane.odoo_preview_apply_http.execute_odoo_preview_dokploy_apply"
        ) as apply:
            refused = await self.fixture.preview_apply()
        self.assertEqual(refused.status_code, 409, refused.text)
        apply.assert_not_called()

    def test_source_carrying_plan_or_original_cannot_use_source_free_qualification(self) -> None:
        from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
        from control_plane.preview_reservation_recovery import require_unrecorded_preview_refresh

        reference = ProductSecretCopyFrom(
            context="cm", instance="testing", version_id="fixture-version"
        )
        snapshot = CleanTestingSnapshot(
            snapshot_id="a" * 64,
            product=self.profile.product,
            context=self.profile.preview.context,
            server_id="snapshot-server",
            captured_at=datetime.now(timezone.utc).isoformat(),
            database_sha256="b" * 64,
            filestore_sha256="c" * 64,
            storage_budget=OdooTestingDrillStorageBudget(
                database_bytes=1, filestore_bytes=1, staging_bytes=1, reserve_bytes=1
            ),
        )
        for scenario in ("current_import", "original_import", "original_snapshot"):
            with self.subTest(scenario=scenario):
                self.prepare_fixture(
                    original_source=reference if scenario == "original_import" else None,
                    original_snapshot=snapshot if scenario == "original_snapshot" else None,
                )
                current = _issued_plan(self.request)
                if scenario == "current_import":
                    current.plan_request.fishbowl_source = reference
                assert self.original is not None
                with self.assertRaises(ValueError):
                    require_unrecorded_preview_refresh(
                        self.store,
                        profile=self.profile,
                        issued_plan=current,
                        original_reservation_id=self.original.record_id,
                        operation_record_id="new-operation",
                    )
                self.assertEqual(self.store.list_preview_records(), ())
                self.assert_original_preserved()
