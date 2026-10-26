from typing import TYPE_CHECKING
import json

from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
from control_plane.contracts.testing_address import ADDRESS_SCOPE
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import (
    NativeSourceAuthorizationDenied,
    NativeSourceUnavailable,
)
from control_plane.dokploy.testing_route import observe_testing_route, route_document
from control_plane.provider_operations import (
    DurableProviderMutationAdapter,
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.testing_address_qualification import (
    inspect_address_domains,
    require_idle_address_recipients,
    require_unbound_address_recipients,
)
from control_plane.testing_address_storage import (
    read_testing_address_recovery_copy,
    verify_testing_address_retained_custody,
    verify_testing_address_sources,
)

if TYPE_CHECKING:
    from control_plane.testing_address import TestingAddressAdapter


PREPARATION_ROUTE = "service:testing-address:recipient-preparation"


class AddressRecipientPreparation(DurableProviderMutationAdapter):
    def __init__(
        self,
        adapter: TestingAddressAdapter,
        parent_lease: ProviderOperationLease,
        target_id: str,
        *,
        settle_only: bool,
    ) -> None:
        self.adapter, self.parent_lease, self.target_id = adapter, parent_lease, target_id
        self.settle_only = settle_only

    def provider_target_origin(self) -> ProviderTargetOrigin:
        return self.adapter.provider_target_origin().model_copy(
            update={"target_id": self.target_id}
        )

    def target_key(self) -> str:
        return self.provider_target_origin().target_key()

    def reconciliation_key(self) -> str:
        return self.adapter.plan.digest

    def observe_automation(self) -> bool:
        self.parent_lease.assert_current()
        read_testing_address_recovery_copy(self.adapter.store, self.adapter.plan)
        verify_testing_address_retained_custody(self.adapter.store, self.adapter.plan)
        host, token = source.read_dokploy_config(
            control_plane_root=self.adapter.root, database_url=self.adapter.store.database_url
        )
        try:
            target = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=self.target_id
            )
        except api.DokployRequestFailed as error:
            if error.status_code in {401, 403}:
                raise NativeSourceAuthorizationDenied(
                    "The existing provider identity denied original recipient readback."
                ) from error
            raise
        enabled = target.get("autoDeploy")
        if (
            target.get("composeId") != self.target_id
            or target.get("serverId") != self.adapter.plan.server_id
            or target.get("appName") not in self.adapter.plan.retained_projects
            or type(enabled) is not bool
        ):
            raise NativeSourceUnavailable("The original retained recipient readback is unknown.")
        return enabled

    def read_automation(self) -> bool:
        enabled = self.observe_automation()
        if self.settle_only:
            require_unbound_address_recipients(self.adapter.store, frozenset({self.target_id}))
            return enabled
        verify_testing_address_sources(self.adapter.store, self.adapter.plan)
        host, token = source.read_dokploy_config(
            control_plane_root=self.adapter.root, database_url=self.adapter.store.database_url
        )
        plan = self.adapter.plan
        retained_ids = frozenset(str(item.target_id) for item in plan.reservations)
        require_unbound_address_recipients(self.adapter.store, retained_ids)
        require_idle_address_recipients(
            host=host, token=token, context=plan.context, retained_ids=retained_ids
        )
        inventory = inspect_address_domains(
            host=host,
            token=token,
            hostname=plan.route_after.host,
            retained_ids=retained_ids,
            server_id=plan.server_id,
        )
        if (
            inventory.digest != plan.domain_inventory_sha256
            or inventory.projects != plan.retained_projects
            or any(
                enabled and not plan.retained_automation[target]
                for target, enabled in inventory.automation.items()
            )
        ):
            raise ValueError("The retained hostname recipient changed before preparation.")
        frame = self.adapter.transport.read()
        before = plan.route_before
        owned = json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None
        if (
            owned != route_document(before, before.new_backend)
            or observe_testing_route(before, frame["raw"], owned) != before.new_backend
        ):
            raise ValueError("Recipient preparation requires the unchanged original testing route.")
        return inventory.automation[self.target_id]

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        if not self.observe_automation():
            return ProviderObservation("present", 200, {"automatic_deployment_disabled": True})
        if self.settle_only and not provider_effect_phase:
            return ProviderObservation("present", 200, {"not_dispatched": True})
        return ProviderObservation(
            "absent", retry_safe=True, preserve_effect_checkpoint=bool(provider_effect_phase)
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        try:
            enabled = self.read_automation()
        except Exception as error:
            raise ProviderMutationRejectedError(error) from error
        if enabled:
            host, token = source.read_dokploy_config(
                control_plane_root=self.adapter.root, database_url=self.adapter.store.database_url
            )
            self.parent_lease.checkpoint_effect("testing-address-recipient-preparation")
            lease.checkpoint_effect("disable-retained-auto-deploy")
            try:
                api.dokploy_request(
                    host=host,
                    token=token,
                    path="/api/compose.update",
                    method="POST",
                    payload={"composeId": self.target_id, "autoDeploy": False},
                )
            except api.DokployRequestFailed as error:
                if error.status_code in {401, 403}:
                    raise NativeSourceAuthorizationDenied(
                        "The existing provider identity denied retained recipient preparation."
                    ) from error
                raise
        if self.read_automation():
            raise NativeSourceUnavailable("The original recipient preparation has not converged.")
        return ProviderMutationOutcome(
            200, {"automatic_deployment_disabled": True}, provider_effect_performed=enabled
        )


def prepare_address_recipients(
    adapter: TestingAddressAdapter, lease: ProviderOperationLease, *, settle_only: bool = False
) -> None:
    assert isinstance(lease, ProviderEvidenceLease)
    for target, enabled in sorted(adapter.plan.retained_automation.items()):
        if not enabled:
            continue
        key = adapter.plan.digest + ":" + target
        if (
            settle_only
            and adapter.store.read_idempotency_record(
                scope=ADDRESS_SCOPE, route_path=PREPARATION_ROUTE, idempotency_key=key
            )
            is None
        ):
            continue
        result = run_durable_provider_operation(
            store=adapter.store,
            scope=ADDRESS_SCOPE,
            route_path=PREPARATION_ROUTE,
            idempotency_key=key,
            request_fingerprint=adapter.plan.digest,
            lease_owner=lease.evidence_reservation().lease_owner,
            response_trace_id=adapter.trace_id,
            adapter=AddressRecipientPreparation(adapter, lease, target, settle_only=settle_only),
            original_intent={"testing_address_plan": adapter.plan.model_dump(mode="json")},
        )
        if result.status not in {"completed", "replayed", "adopted"}:
            raise NativeSourceUnavailable(
                "Reconcile the original retained recipient preparation without another request."
            )
