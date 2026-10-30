from dataclasses import dataclass
import json
from pathlib import Path
from typing import cast

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.preview_execution import odoo_preview_target_key
from control_plane.contracts.preview_fixture import PreviewFixtureReceipt, PreviewFixtureRequest
from control_plane.disposable_lanes import (
    DisposableLaneStore,
    require_disposable_lane,
    require_lane_target_ownership,
)
from control_plane.dokploy import api, post_deploy, source
from control_plane.host_package import host_schedule
from control_plane import odoo_runtime_reads as reads
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore


@dataclass
class PreviewFixtureAdapter:
    store: PostgresRecordStore
    root: Path
    product: str
    request: PreviewFixtureRequest
    trace_id: str

    def selection(self) -> reads.OdooRuntimeSelection:
        preview = self.store.read_preview_record(self.request.preview_id)
        selection = reads.select_preview_runtime(self.store, preview)
        identity = selection.identity
        if (
            identity.product != self.product
            or identity.context != self.request.context
            or identity.instance != self.request.instance
            or identity.preview_generation_id != self.request.generation_id
            or identity.environment_kind != "preview"
            or identity.instance in {"testing", "prod", "production"}
        ):
            raise ValueError("Fixture target differs from the reviewed preview generation.")
        require_disposable_lane(
            cast(DisposableLaneStore, self.store),
            product=self.product,
            context=identity.context,
            instance=identity.instance,
            preview=preview,
            preview_operation="refresh",
        )
        return selection

    def target_key(self) -> str:
        return odoo_preview_target_key(
            context=self.request.context, compose_name=self.request.instance
        )

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(
            [self.target_key(), self.request.generation_id, self.request.fixture]
        )

    def invoke(
        self, mode: str, lease: ProviderOperationLease | None = None
    ) -> PreviewFixtureReceipt:
        selection = self.selection()
        connection = reads.connect_runtime(
            store=self.store,
            selection=selection,
            control_plane_root=self.root,
            database_url=self.store.database_url,
        )
        require_lane_target_ownership(
            (
                *self.store.list_dokploy_target_id_records(),
                *self.store.list_physical_provider_target_records(),
            ),
            context=self.request.context,
            instance=self.request.instance,
            target_id=connection.evidence.target_id,
        )
        target = api.fetch_dokploy_target_payload(
            host=connection.host,
            token=connection.token,
            target_type="compose",
            target_id=connection.evidence.target_id,
        )
        kind, lookup, _project, server = post_deploy._resolve_dokploy_schedule_runtime(
            host=connection.host,
            token=connection.token,
            compose_id=connection.evidence.target_id,
            compose_name=connection.app_name,
            target_payload=target,
        )
        spec: dict[str, object] = {
            "identity": selection.identity.model_dump(mode="json"),
            "fixture": self.request.fixture,
            "mode": mode,
            "container_id": connection.evidence.container_id,
            "app_name": connection.app_name,
            "database": connection.credentials["ODOO_DB_NAME"],
        }
        script = host_schedule("preview_fixture", spec)
        schedule_name = (
            "Launchplane preview fixture " + canonical_json_sha256([self.trace_id, spec])[:24]
        )

        def checkpoint(phase: str) -> None:
            self.selection()
            reads.assert_selection_current(self.store, selection)
            if lease is not None:
                lease.checkpoint_effect(phase)

        evidence = post_deploy.run_manual_data_schedule(
            host=connection.host,
            token=connection.token,
            target_definition=source.DokployTargetDefinition(
                context=self.request.context,
                instance=self.request.instance,
                target_id=connection.evidence.target_id,
                target_name=connection.evidence.target_name,
            ),
            schedule_lookup_id=lookup,
            schedule_type=kind,
            schedule_name=schedule_name,
            schedule_app_name=connection.app_name,
            schedule_payload={
                "name": schedule_name,
                "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
                "appName": connection.app_name,
                "shellType": "bash",
                "scheduleType": kind,
                "command": "control-plane preview fixture",
                "script": script,
                "serverId": server,
                "userId": lookup if kind == "dokploy-server" else None,
                "enabled": False,
                "timezone": "UTC",
            },
            resolved_schedule_execution_timeout_seconds=240,
            before_provider_mutation=checkpoint,
        )
        encoded = evidence.get("preview_fixture_result", "")
        if not encoded or len(encoded) > 65536:
            raise ValueError("Fixture receipt is unavailable.")
        receipt = PreviewFixtureReceipt.model_validate(json.loads(bytes.fromhex(encoded)))
        if (
            receipt.identity != selection.identity
            or receipt.fixture != self.request.fixture
            or receipt.mode != mode
        ):
            raise ValueError("Fixture receipt differs from the selected preview.")
        reads.assert_selection_current(self.store, selection)
        return receipt

    def plan(self) -> tuple[PreviewFixtureReceipt, str]:
        receipt = self.invoke("dry-run")
        return receipt, canonical_json_sha256(
            {
                "identity": receipt.identity.model_dump(mode="json"),
                "fixture": receipt.fixture,
                "fixture_sha256": receipt.fixture_sha256,
            }
        )

    def response(
        self, receipt: PreviewFixtureReceipt, *, recovered: bool = False
    ) -> dict[str, object]:
        return provider_operation_response_payload(
            trace_id=self.trace_id,
            records={
                "preview_id": self.request.preview_id,
                "generation_id": self.request.generation_id,
            },
            result={"receipt": receipt.model_dump(mode="json"), "recovered": recovered},
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        try:
            planned, digest = self.plan()
            if digest != self.request.reviewed_plan_sha256:
                raise ValueError("Fixture or serving generation changed; review a new dry run.")
        except (ValueError, api.DokployScheduleExecutionFailed) as error:
            raise ProviderMutationRejectedError(
                ValueError("Fixture preflight did not match its review.")
            ) from error
        receipt = self.invoke("apply", lease)
        if receipt.fixture_sha256 != planned.fixture_sha256:
            raise ValueError(
                "Applied fixture differs from its review; reconcile the original operation."
            )
        return ProviderMutationOutcome(202, self.response(receipt))

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        try:
            receipt = self.invoke("readback")
            digest = canonical_json_sha256(
                {
                    "identity": receipt.identity.model_dump(mode="json"),
                    "fixture": receipt.fixture,
                    "fixture_sha256": receipt.fixture_sha256,
                }
            )
            if digest != self.request.reviewed_plan_sha256:
                return ProviderObservation("unknown")
        except (ValueError, api.DokployScheduleExecutionFailed):
            return ProviderObservation("unknown")
        return ProviderObservation(
            "present", response_payload=self.response(receipt, recovered=True)
        )
