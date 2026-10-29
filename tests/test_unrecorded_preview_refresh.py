from datetime import datetime, timedelta, timezone
from contextlib import contextmanager, nullcontext
from collections.abc import Iterator
from typing import Any, Literal
import unittest
from unittest.mock import patch

from sqlalchemy import delete, select

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane.product_secret_copy import ProductSecretCopyFrom
from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyEnvelope,
    issue_odoo_preview_apply_plan,
)
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplanePreviewRow,
    LaunchplanePreviewGenerationRow,
    LaunchplaneIdempotencyRow,
)
from tests.support.auth import scoped_agent_transport
from tests.test_director_bounded_writes import DirectorBoundedWritesFixture
from tests.test_odoo_preview_apply_execution import _issued_plan


class UnrecordedPreviewRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.prepare_fixture()

    def prepare_fixture(
        self,
        *,
        created: bool = True,
        original_source: ProductSecretCopyFrom | None = None,
        original_snapshot: CleanTestingSnapshot | None = None,
        data_transport_mode: Literal["none", "driver"] = "driver",
    ) -> None:
        self.fixture = DirectorBoundedWritesFixture(self)
        self.store = self.fixture.store
        self.profile = self.fixture.profile
        self.profile.preview.data_transport_mode = data_transport_mode
        self.store.write_product_profile_record(self.profile)
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

    async def test_driver_original_with_unknown_or_upstream_data_history_refuses(self) -> None:
        for current_mode in ("none", "driver"):
            for evidence in (None, {"upstream_restore_requested": "true"}):
                with self.subTest(current_mode=current_mode, evidence=evidence):
                    self.prepare_fixture(created=False)
                    self.profile.preview.data_transport_mode = current_mode
                    self.store.write_product_profile_record(self.profile)
                    result: dict[str, object] = {"status": "fail", "compose_id": self.target_id}
                    if evidence is not None:
                        result["module_install_update_evidence"] = evidence
                    self.replace_original(response_payload={"result": result})
                    with patch(
                        "control_plane.http_app.execute_odoo_preview_apply_result",
                        return_value={"status": "blocked"},
                    ) as apply:
                        refused = await self.fixture.preview_apply()
                    self.assertEqual(refused.status_code, 409, refused.text)
                    apply.assert_not_called()
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

    async def test_existing_target_failed_original_qualifies_without_lifecycle(self) -> None:
        for mode in ("none", "driver"):
            with self.subTest(mode=mode):
                self.prepare_fixture(created=False, data_transport_mode=mode)
                with patch(
                    "control_plane.http_app.execute_odoo_preview_apply_result",
                    return_value={"status": "blocked"},
                ) as apply:
                    response = await self.fixture.preview_apply()
                self.assertEqual(response.status_code, 202, response.text)
                apply.assert_called_once()
                self.assertEqual(self.store.list_preview_records(), ())
                self.assert_original_preserved()

    async def test_failed_apply_and_original_observation_never_publish_success_or_reexecute(
        self,
    ) -> None:
        def execute(**kwargs: Any) -> dict[str, object]:
            kwargs["provider_effect_checkpoint"]("fixture-uncertain-effect")
            return {"status": "fail", "provider_effect_attempted": True}

        with patch(
            "control_plane.http_app.execute_odoo_preview_apply_result", side_effect=execute
        ) as apply:
            first = await self.fixture.preview_apply()
            self.assertEqual(first.status_code, 409, first.text)
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
        for scenario in (
            "current_import",
            "original_import",
            "current_snapshot",
            "original_snapshot",
        ):
            with self.subTest(scenario=scenario):
                self.prepare_fixture(
                    original_source=reference if scenario == "original_import" else None,
                    original_snapshot=snapshot if scenario == "original_snapshot" else None,
                )
                current = _issued_plan(self.request)
                if scenario == "current_import":
                    current.plan_request.fishbowl_source = reference
                elif scenario == "current_snapshot":
                    current.dry_run_plan.clean_snapshot = snapshot
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
