from dataclasses import dataclass
import hashlib
import hmac

from control_plane import odoo_runtime_reads as reads
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.preview_fixture import PreviewFixtureIntent
from control_plane.contracts.preview_parts_import import (
    IMPORT_TIMEOUT_SECONDS,
    PreviewPartsImportReceipt,
)
from control_plane.dokploy import post_deploy, source
from control_plane.host_package import host_schedule, host_package
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.odoo_preview_import_source import resolve_fishbowl_preview_source
from control_plane.preview_fixture import PreviewMaintenanceAdapter
from control_plane.preview_reservation_recovery import original_preview_intent
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)


def import_plan_sha256(receipt: PreviewPartsImportReceipt) -> str:
    return canonical_json_sha256(
        {
            "identity": receipt.identity.model_dump(mode="json"),
            "source_authority_sha256": receipt.source_authority_sha256,
            "timeout_seconds": receipt.timeout_seconds,
            "parts_only": receipt.parts_only,
        }
    )


@dataclass
class PreviewPartsImportAdapter(PreviewMaintenanceAdapter):
    source_authority_sha256: str = ""
    effect_started: bool = False

    def qualified_source(self, connection: reads.OdooRuntimeConnection) -> tuple[str, str]:
        profile = self.store.read_product_profile_record(self.product)
        identity = connection.selection.identity
        preparation = self.store.read_idempotency_record_by_id(identity.deployment_record_id)
        if preparation is None or preparation.state != "completed":
            raise ValueError("Import requires its completed preview preparation.")
        records = preparation.response_payload.get("records")
        if (
            not isinstance(records, dict)
            or records.get("preview_id") != identity.preview_id
            or records.get("generation_id") != identity.preview_generation_id
        ):
            raise ValueError("Preparation differs from the exact serving generation.")
        original = original_preview_intent(preparation, profile=profile, store=self.store)
        issued = original.issued_plan
        reference = issued.plan_request.fishbowl_source
        if (
            reference is None
            or issued.plan_request.source_git_ref != identity.source_git_ref
            or issued.plan_request.manifest is None
            or issued.plan_request.manifest.artifact_id != identity.artifact_id
            or issued.dry_run_plan.compose_name != identity.instance
        ):
            raise ValueError("Import source differs from the selected preview preparation.")
        selected = resolve_fishbowl_preview_source(
            store=self.store,
            profile=profile,
            preview_slug=issued.preview_slug,
            copy_from=reference,
            manifest=issued.plan_request.manifest,
            include_secret_value=True,
        )
        if selected.authority_sha256 != issued.fishbowl_source_sha256:
            raise ValueError("Managed preview source changed since preparation.")
        values = {
            item.key: (
                item.value.value
                if item.value.source == "literal"
                else selected.environment[config_parameter_secret_env_key(item.key)]
            )
            for item in selected.parameters
        }
        settings_sha256 = canonical_json_sha256(values)
        before = reads.inspect_runtime_container(
            connection.host,
            connection.token,
            connection.server_id,
            connection.evidence.container_id,
            connection.app_name,
            identity,
        )
        password_key = config_parameter_secret_env_key("fishbowl.password")
        if not hmac.compare_digest(
            before.get(password_key, ""), selected.environment[password_key]
        ):
            raise ValueError("Preview secret differs from its current managed source.")
        return selected.authority_sha256, settings_sha256

    def original_intent(self, retained: dict[str, object] | None = None) -> dict[str, object]:
        if retained:
            intent = PreviewFixtureIntent.model_validate(retained)
            if (
                intent.product != self.product
                or intent.maintenance != self.request
                or not intent.source_authority_sha256
            ):
                raise ValueError("Import differs from its retained original.")
            self.binding = intent.fixture_binding
            self.source_authority_sha256 = intent.source_authority_sha256
        else:
            self.capture_binding()
            self.source_authority_sha256, _ = self.qualified_source(self.connect())
        if self.binding is None:
            raise ValueError("Exact import binding unavailable.")
        return PreviewFixtureIntent(
            product=self.product,
            maintenance=self.request,
            fixture_binding=self.binding,
            source_authority_sha256=self.source_authority_sha256,
        ).model_dump(mode="json")

    def invoke(
        self, mode: str, lease: ProviderOperationLease | None = None, *, operation: str = ""
    ) -> PreviewPartsImportReceipt:
        connection = self.connect(mutation=mode == "apply")
        binding = self.binding or self.capture_binding()
        if binding != self.runtime_binding(connection):
            raise ValueError("Import runtime differs from its retained exact binding.")
        authority, settings = self.qualified_source(connection)
        if self.source_authority_sha256 and authority != self.source_authority_sha256:
            raise ValueError("Import managed source differs from its retained original.")
        self.source_authority_sha256 = authority
        spec = {
            "identity": binding.identity.model_dump(mode="json"),
            "container_id": binding.container_id,
            "app_name": binding.app_name,
            "database": binding.database,
            "mode": mode,
            "operation": operation,
            "source_authority_sha256": authority,
            "settings_sha256": settings,
            "timeout_seconds": IMPORT_TIMEOUT_SECONDS,
            "odoo_program": host_package().programs["odoo_parts_import"],
            "probe_program": host_package().programs["import_grants_probe"],
        }
        script = host_schedule("preview_parts_import", spec)
        app = "preview-parts-import-" + canonical_json_sha256(script)

        def checkpoint(phase: str) -> None:
            current = self.connect(mutation=mode == "apply")
            if self.runtime_binding(current) != binding or self.qualified_source(current) != (
                authority,
                settings,
            ):
                raise ValueError("Import source or preview changed before dispatch.")
            reads.assert_selection_current(self.store, connection.selection)
            if lease is not None and phase == post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER:
                lease.checkpoint_effect(phase)
                self.effect_started = True

        evidence = post_deploy.run_manual_data_schedule(
            host=connection.host,
            token=connection.token,
            target_definition=source.DokployTargetDefinition(
                context=binding.identity.context,
                instance=binding.identity.instance,
                target_id=binding.target_id,
                target_name=binding.target_name,
            ),
            schedule_lookup_id=binding.lookup_id,
            schedule_type=binding.schedule_type,
            schedule_name="Launchplane " + app,
            schedule_app_name=app,
            schedule_payload={
                "name": "Launchplane " + app,
                "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
                "appName": app,
                "shellType": "bash",
                "scheduleType": binding.schedule_type,
                "command": "control-plane preview parts import",
                "script": script,
                "serverId": binding.server_id,
                "userId": binding.lookup_id if binding.schedule_type == "dokploy-server" else None,
                "enabled": False,
                "timezone": "UTC",
            },
            resolved_schedule_execution_timeout_seconds=IMPORT_TIMEOUT_SECONDS + 90,
            before_provider_mutation=checkpoint,
        )
        encoded = evidence.get("preview_parts_import_result", "")
        if not encoded or len(encoded) > 32768:
            raise ValueError("Import receipt unavailable.")
        receipt = PreviewPartsImportReceipt.model_validate_json(bytes.fromhex(encoded))
        if (
            receipt.identity != binding.identity
            or receipt.source_authority_sha256 != authority
            or receipt.mode != mode
            or receipt.operation != operation
        ):
            raise ValueError("Import receipt differs from its original.")
        checkpoint("readback")
        return receipt

    def plan(self) -> tuple[PreviewPartsImportReceipt, str]:
        receipt = self.invoke("dry-run")
        return receipt, import_plan_sha256(receipt)

    def response(
        self, receipt: PreviewPartsImportReceipt, *, recovered: bool = False
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
        operation = hashlib.sha256(provider_operation_key.encode()).hexdigest()
        try:
            planned, digest = self.plan()
            if planned.outcome != "qualified" or digest != self.request.reviewed_plan_sha256:
                raise ValueError("Import preflight did not match its review.")
        except Exception as error:
            raise ProviderMutationRejectedError(
                ValueError("Import preflight unavailable.")
            ) from error
        self.effect_started = False
        try:
            receipt = self.invoke("apply", lease, operation=operation)
        except Exception as error:
            if not self.effect_started:
                raise ProviderMutationRejectedError(
                    ValueError("Import dispatch did not begin.")
                ) from error
            raise ProviderMutationUnknownError(
                "Import outcome is unknown; reconcile the original key."
            ) from error
        if not receipt.committed:
            raise ProviderMutationUnknownError(
                "Import completion is unverified; preserve its original."
            )
        return ProviderMutationOutcome(202, self.response(receipt))

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        if not provider_effect_phase:
            return ProviderObservation("absent", retry_safe=True)
        try:
            receipt = self.invoke(
                "readback", operation=hashlib.sha256(provider_operation_key.encode()).hexdigest()
            )
            if (
                receipt.committed
                and import_plan_sha256(receipt) == self.request.reviewed_plan_sha256
            ):
                return ProviderObservation(
                    "present", response_payload=self.response(receipt, recovered=True)
                )
        except Exception:
            return ProviderObservation("unknown")
        return ProviderObservation("unknown")
