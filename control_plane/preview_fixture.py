from dataclasses import dataclass
from contextlib import AbstractContextManager
from collections.abc import Callable
import json
import click
from pydantic import BaseModel
from pathlib import Path
from typing import cast

from control_plane.copy_readback_diagnostics import readback_failure_evidence
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.preview_execution import (
    odoo_preview_target_key,
    odoo_preview_reconciliation_key,
)
from control_plane.contracts.preview_fixture import (
    PreviewFixtureBinding,
    PreviewFixtureIntent,
    PreviewFixtureReceipt,
    PreviewFixtureRequest,
    PreviewPartsImportRequest,
)
from control_plane.disposable_lanes import (
    DisposableLaneStore,
    require_disposable_lane,
    require_lane_target_ownership,
    resolve_disposable_preview,
)
from control_plane.workflows.generic_web_preview import (
    effective_preview_app_name_prefix,
    preview_application_name,
    resolve_generic_web_preview_slug,
)
from control_plane.dokploy import api, post_deploy, source
from control_plane.host_package import host_schedule
from control_plane import odoo_runtime_reads as reads
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane._preview_fixture_program import (
    PREVIEW_FIXTURE_RESULT_MARKER as PREVIEW_FIXTURE_RESULT_MARKER,
)


@dataclass
class FixtureEffectLease:
    lease: ProviderOperationLease
    started: bool = False

    def assert_current(self) -> None:
        self.lease.assert_current()

    def checkpoint_effect(self, phase: str) -> None:
        self.lease.checkpoint_effect(phase)
        self.started = True


def fixture_plan_sha256(receipt: PreviewFixtureReceipt) -> str:
    return canonical_json_sha256(
        {
            "identity": receipt.identity.model_dump(mode="json"),
            "fixture": receipt.fixture,
            "fixture_sha256": receipt.fixture_sha256,
        }
    )


@dataclass
class PreviewMaintenanceAdapter:
    store: PostgresRecordStore
    root: Path
    product: str
    request: PreviewFixtureRequest | PreviewPartsImportRequest
    trace_id: str
    binding: PreviewFixtureBinding | None = None
    failure_reason: str | None = None

    def selection(self, *, mutation: bool = False) -> reads.OdooRuntimeSelection:
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
        if mutation:
            profile = self.store.read_product_profile_record(self.product)
            slot = fixture_preview_slot(self.store, self.product, preview.anchor_pr_number)
            owned = resolve_disposable_preview(
                cast(DisposableLaneStore, self.store), profile, identity.context, slot
            )
            if (
                owned.preview_id != preview.preview_id
                or identity.instance
                != preview_application_name(
                    app_name_prefix=effective_preview_app_name_prefix(profile=profile),
                    preview_slug=slot,
                )
            ):
                raise ValueError("Fixture runtime differs from the current owned preview slot.")
            require_disposable_lane(
                cast(DisposableLaneStore, self.store),
                product=self.product,
                context=identity.context,
                instance=slot,
                preview=preview,
                preview_operation="refresh",
            )
        return selection

    def target_key(self) -> str:
        return odoo_preview_target_key(
            context=self.request.context, compose_name=self.request.instance
        )

    def reconciliation_key(self) -> str:
        return odoo_preview_reconciliation_key(
            context=self.request.context, compose_name=self.request.instance
        )

    def connect(self, *, mutation: bool = False) -> reads.OdooRuntimeConnection:
        selection = self.selection(mutation=mutation)
        connection = reads.connect_runtime(
            store=self.store,
            selection=selection,
            control_plane_root=self.root,
            database_url=self.store.database_url,
        )
        require_fixture_target_ownership(
            self.store,
            product=self.product,
            context=self.request.context,
            anchor_pr_number=self.store.read_preview_record(
                self.request.preview_id
            ).anchor_pr_number,
            target_id=connection.evidence.target_id,
            mutation=mutation,
        )
        return connection

    def capture_binding(self) -> PreviewFixtureBinding:
        binding = self.runtime_binding(self.connect())
        self.binding = binding
        return binding

    def runtime_binding(self, connection: reads.OdooRuntimeConnection) -> PreviewFixtureBinding:
        target = api.fetch_dokploy_target_payload(
            host=connection.host,
            token=connection.token,
            target_type="compose",
            target_id=connection.evidence.target_id,
        )
        # noinspection PyProtectedMember
        kind, lookup, _project, server = post_deploy._resolve_dokploy_schedule_runtime(
            host=connection.host,
            token=connection.token,
            compose_id=connection.evidence.target_id,
            compose_name=connection.app_name,
            target_payload=target,
        )
        return PreviewFixtureBinding.model_validate(
            {
                "identity": connection.selection.identity,
                "target_id": connection.evidence.target_id,
                "target_name": connection.evidence.target_name,
                "container_id": connection.evidence.container_id,
                "app_name": connection.app_name,
                "database": connection.credentials["ODOO_DB_NAME"],
                "schedule_type": kind,
                "lookup_id": lookup,
                "server_id": server,
                "anchor_pr_number": self.store.read_preview_record(
                    self.request.preview_id
                ).anchor_pr_number,
            }
        )

    def verified_binding(
        self, connection: reads.OdooRuntimeConnection, *, mode: str
    ) -> PreviewFixtureBinding:
        binding = self.binding or self.capture_binding()
        if (
            binding.identity != connection.selection.identity
            or binding.target_id != connection.evidence.target_id
            or mode != "readback"
            and binding.container_id != connection.evidence.container_id
            or binding.database != connection.credentials["ODOO_DB_NAME"]
            or binding.app_name != connection.app_name
        ):
            raise ValueError("Maintenance runtime differs from its retained binding.")
        return self.runtime_binding(connection) if mode == "readback" else binding

    def response(self, receipt: BaseModel, *, recovered: bool = False) -> dict[str, object]:
        return provider_operation_response_payload(
            trace_id=self.trace_id,
            records={
                "preview_id": self.request.preview_id,
                "generation_id": self.request.generation_id,
            },
            result={"receipt": receipt.model_dump(mode="json"), "recovered": recovered},
        )


@dataclass
class PreviewFixtureAdapter(PreviewMaintenanceAdapter):
    def original_intent(self, retained: dict[str, object] | None = None) -> dict[str, object]:
        if retained:
            intent = PreviewFixtureIntent.model_validate(retained)
            if intent.product != self.product or intent.maintenance != self.request:
                raise ValueError("Fixture operation differs from its original intent.")
            self.binding = intent.fixture_binding
        else:
            binding = self.capture_binding()
            intent = PreviewFixtureIntent(
                product=self.product,
                maintenance=self.request,
                fixture_binding=binding,
            )
        return intent.model_dump(mode="json")

    def invoke(
        self,
        mode: str,
        lease: ProviderOperationLease | None = None,
        *,
        expected_fixture_sha256: str = "",
    ) -> PreviewFixtureReceipt:
        self.failure_reason = None
        connection = self.connect(mutation=mode == "apply")
        binding = self.verified_binding(connection, mode=mode)
        selection = connection.selection

        def checkpoint(phase: str) -> None:
            self.selection(mutation=mode == "apply")
            require_fixture_target_ownership(
                self.store,
                product=self.product,
                context=selection.identity.context,
                anchor_pr_number=binding.anchor_pr_number,
                target_id=binding.target_id,
                mutation=mode == "apply",
            )
            reads.assert_selection_current(self.store, selection)
            if lease is not None and phase == post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER:
                lease.checkpoint_effect(phase)

        try:
            evidence = run_fixture_schedule(
                host=connection.host,
                token=connection.token,
                binding=binding,
                mode=mode,
                fixture=self.request.fixture
                if isinstance(self.request, PreviewFixtureRequest)
                else "",
                before_provider_mutation=checkpoint,
                expected_fixture_sha256=expected_fixture_sha256,
            )
        except post_deploy.OdooPostDeployReadbackFailure as error:
            self.failure_reason = readback_failure_evidence(error.evidence)[
                "readback_failed_predicate"
            ]
            raise
        encoded = evidence.get(PREVIEW_FIXTURE_RESULT_MARKER, "")
        if not encoded or len(encoded) > 65536:
            raise ValueError("Fixture receipt is unavailable.")
        receipt = PreviewFixtureReceipt.model_validate(json.loads(bytes.fromhex(encoded)))
        if (
            receipt.identity != selection.identity
            or not isinstance(self.request, PreviewFixtureRequest)
            or receipt.fixture != self.request.fixture
            or receipt.mode != mode
        ):
            raise ValueError("Fixture receipt differs from the selected preview.")
        reads.assert_selection_current(self.store, selection)
        return receipt

    def plan(self) -> tuple[PreviewFixtureReceipt, str]:
        receipt = self.invoke("dry-run")
        return receipt, fixture_plan_sha256(receipt)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        del provider_operation_key
        try:
            historical = self.invoke("readback")
            if fixture_plan_sha256(historical) != self.request.reviewed_plan_sha256:
                raise ValueError("Fixture or serving generation changed; review a new dry run.")
            if historical.records:
                return ProviderMutationOutcome(
                    202, self.response(historical, recovered=True), provider_effect_performed=False
                )
            planned, digest = self.plan()
            if digest != self.request.reviewed_plan_sha256:
                raise ValueError("Fixture or serving generation changed; review a new dry run.")
        except Exception as error:
            raise ProviderMutationRejectedError(
                ValueError("Fixture preflight did not match its review.")
            ) from error
        effect = FixtureEffectLease(lease)
        try:
            receipt = self.invoke("apply", effect, expected_fixture_sha256=planned.fixture_sha256)
        except Exception as error:
            if not effect.started:
                raise ProviderMutationRejectedError(
                    ValueError("Fixture dispatch did not begin.")
                ) from error
            raise ProviderMutationUnknownError("Fixture outcome is unverified.") from error
        if receipt.committed and receipt.fixture_sha256 != planned.fixture_sha256:
            raise ProviderMutationUnknownError(
                "Applied fixture differs from its review; reconcile the original operation."
            )
        return ProviderMutationOutcome(202 if receipt.committed else 409, self.response(receipt))

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        del provider_operation_key
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        if not provider_effect_phase:
            return ProviderObservation("absent", retry_safe=True)
        try:
            receipt = self.invoke("readback")
            digest = fixture_plan_sha256(receipt)
            if digest != self.request.reviewed_plan_sha256:
                return ProviderObservation("unknown")
        except (ValueError, OSError, RuntimeError, KeyError, click.ClickException):
            return ProviderObservation("unknown")
        if not receipt.records:
            return ProviderObservation("unknown")
        return ProviderObservation(
            "present", response_payload=self.response(receipt, recovered=True)
        )


def fixture_schedule_script(
    binding: PreviewFixtureBinding, *, fixture: str, mode: str, expected_fixture_sha256: str = ""
) -> str:
    spec: dict[str, object] = {
        "identity": binding.identity.model_dump(mode="json"),
        "fixture": fixture,
        "mode": mode,
        "container_id": binding.container_id,
        "app_name": binding.app_name,
        "database": binding.database,
        "expected_fixture_sha256": expected_fixture_sha256,
    }
    return host_schedule("preview_fixture", spec)


def fixture_schedule_app_name(script: str, *, action: str = "preview-fixture") -> str:
    return action + "-" + canonical_json_sha256(script)


def run_fixture_schedule(
    *,
    host: str,
    token: str,
    binding: PreviewFixtureBinding,
    mode: str,
    fixture: str,
    before_provider_mutation: Callable[[str], None] | None = None,
    provider_mutation_guard: Callable[[], AbstractContextManager[None]] | None = None,
    expected_fixture_sha256: str = "",
) -> dict[str, str]:
    script = fixture_schedule_script(
        binding, fixture=fixture, mode=mode, expected_fixture_sha256=expected_fixture_sha256
    )
    return run_preview_maintenance_schedule(
        host=host,
        token=token,
        binding=binding,
        script=script,
        action="preview-fixture",
        timeout_seconds=240,
        before_provider_mutation=before_provider_mutation,
        provider_mutation_guard=provider_mutation_guard,
    )


def run_preview_maintenance_schedule(
    *,
    host: str,
    token: str,
    binding: PreviewFixtureBinding,
    script: str,
    action: str,
    timeout_seconds: int,
    before_provider_mutation: Callable[[str], None] | None = None,
    provider_mutation_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> dict[str, str]:
    schedule_app_name = fixture_schedule_app_name(script, action=action)
    schedule_name = "Launchplane " + schedule_app_name
    return post_deploy.run_manual_data_schedule(
        host=host,
        token=token,
        target_definition=source.DokployTargetDefinition(
            context=binding.identity.context,
            instance=binding.identity.instance,
            target_id=binding.target_id,
            target_name=binding.target_name,
        ),
        schedule_lookup_id=binding.lookup_id,
        schedule_type=binding.schedule_type,
        schedule_name=schedule_name,
        schedule_app_name=schedule_app_name,
        schedule_payload={
            "name": schedule_name,
            "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
            "appName": schedule_app_name,
            "shellType": "bash",
            "scheduleType": binding.schedule_type,
            "command": "control-plane " + action.replace("-", " "),
            "script": script,
            "serverId": binding.server_id,
            "userId": binding.lookup_id if binding.schedule_type == "dokploy-server" else None,
            "enabled": False,
            "timezone": "UTC",
        },
        resolved_schedule_execution_timeout_seconds=timeout_seconds,
        before_provider_mutation=before_provider_mutation,
        provider_mutation_guard=provider_mutation_guard,
    )


class FixtureContainerStillRunning(ValueError):
    pass


def fixture_preview_slot(store: PostgresRecordStore, product: str, anchor_pr_number: int) -> str:
    return resolve_generic_web_preview_slug(
        profile=store.read_product_profile_record(product),
        preview_slug="",
        anchor_pr_number=anchor_pr_number,
        label="Preview fixture",
    )


def require_fixture_target_ownership(
    store: PostgresRecordStore,
    *,
    product: str,
    context: str,
    anchor_pr_number: int,
    target_id: str,
    mutation: bool = False,
) -> None:
    slot = fixture_preview_slot(store, product, anchor_pr_number)
    targets: tuple[DokployTargetIdRecord | ProviderTargetRecord, ...] = (
        *store.list_dokploy_target_id_records(),
        *store.list_physical_provider_target_records(),
    )
    require_lane_target_ownership(
        targets,
        context=context,
        instance=slot,
        target_id=target_id,
    )
    if mutation and any(
        (item.context, item.instance) == (context, slot) and item.target_id != target_id
        for item in targets
    ):
        raise ValueError("Fixture runtime differs from its owned preview target.")


def require_fixture_container_cancelled(
    *,
    store: PostgresRecordStore,
    root: Path,
    intent: PreviewFixtureIntent,
    cancel: bool = False,
    before_provider_mutation: Callable[[str], None] | None = None,
    provider_mutation_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> None:
    binding = intent.fixture_binding
    require_fixture_target_ownership(
        store,
        product=intent.product,
        context=binding.identity.context,
        anchor_pr_number=binding.anchor_pr_number,
        target_id=binding.target_id,
    )
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    evidence = run_fixture_schedule(
        host=host,
        token=token,
        binding=binding,
        mode="cancel" if cancel else "cancel-readback",
        fixture=intent.maintenance.fixture
        if isinstance(intent.maintenance, PreviewFixtureRequest)
        else "",
        before_provider_mutation=before_provider_mutation,
        provider_mutation_guard=provider_mutation_guard,
    )
    encoded = evidence.get(PREVIEW_FIXTURE_RESULT_MARKER, "")
    if not encoded or len(encoded) > 65536:
        raise ValueError("Original fixture container absence is unavailable.")
    observed = json.loads(bytes.fromhex(encoded))
    if observed == {"container_id": binding.container_id, "stopped": False}:
        raise FixtureContainerStillRunning("Original fixture container can still execute.")
    if observed != {"container_id": binding.container_id, "stopped": True}:
        raise ValueError("Original fixture container can still execute.")
