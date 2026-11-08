from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.contracts.testing_activation import TestingActivationRecord
from control_plane.production_recipient_retirement import (
    RetirementDispatchLease,
    service_references,
)
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
)
from control_plane.retained_volume_sweep_execution import _host_result, _inventory_clear
from control_plane.testing_application_activation import ApplicationActivationAdapter
from control_plane.testing_rebuild_driver import TestingRebuildRefused

if TYPE_CHECKING:
    from control_plane.generic_web_testing_rebuild import GenericWebTestingRebuildExecution


VOLUME_RETIREMENT_PERMIT_SECONDS = 300


@dataclass
class TestingRebuildVolumeRetirement:
    execution: GenericWebTestingRebuildExecution
    activation: ApplicationActivationAdapter
    successor: TestingActivationRecord
    predecessor: TestingActivationRecord
    volume: RetainedVolumeBinding

    def target_key(self) -> str:
        return self.execution.parent.target_key() + ":volume:" + self.volume.volume_identity_sha256

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(
            {
                "successor": self.successor.plan_sha256,
                "predecessor": self.predecessor.plan_sha256,
                "volume": self.volume.model_dump(mode="json"),
            }
        )

    def observe(self, key: str, phase: str, reconciliation_key: str) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)

    def validate_dispatch(self, permit: dict[str, Any]) -> None:
        from control_plane.testing_rebuild_service import NativeTestingRebuildPending

        self.execution.guard("testing_rebuild_retire_volume")
        self.execution.require_retirement_custody(self.predecessor)
        self.activation.require_unchanged(self.successor)
        self.activation.verify_provider(self.successor)
        readiness = self.successor.application_readiness
        assert readiness is not None
        health = self.activation.read_edge_health(
            self.successor, expected=readiness.runtime_identity
        )
        if not health.verified or health.status not in {"pass", "fail"}:
            raise NativeTestingRebuildPending(
                "Current testing health is unavailable before cleanup."
            )
        if (
            health.status != "pass"
            or health.runtime_identity_status != "match"
            or time.time() >= permit["fresh_until"]
            or service_references(self.execution.store, (self.volume.volume_name,))
            or not _inventory_clear(self.execution.root, self.volume, exact=True)
        ):
            raise TestingRebuildRefused(
                "testing_predecessor_retirement_held",
                "The retained volume does not have current independent cleanup custody.",
            )

    def apply(self, key: str, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        from control_plane.testing_rebuild_service import NativeTestingRebuildPending

        if not isinstance(lease, ProviderEvidenceLease):
            raise ValueError("Volume retirement requires its original evidence lease.")
        lease.assert_current()
        row = lease.evidence_reservation()
        original = self.execution.store.read_original_mutation_intent(row)
        permit = original.get("retirement")
        if (
            not isinstance(permit, dict)
            or permit.get("binding") != self.reconciliation_key()
            or original.get("volume") != self.volume.model_dump(mode="json")
        ):
            raise NativeTestingRebuildPending("The original volume removal permit is unavailable.")
        if row.provider_effect_started_at:
            status, _ = _host_result(
                self.execution.root, self.volume, "readback", key, retirement=permit
            )
        else:
            status, _ = _host_result(
                self.execution.root, self.volume, "dry-run", key, retirement=permit
            )
            if status == "eligible":
                try:
                    status, _ = _host_result(
                        self.execution.root,
                        self.volume,
                        "apply",
                        key,
                        RetirementDispatchLease(lease, lambda: self.validate_dispatch(permit)),
                        retirement=permit,
                    )
                except TestingRebuildRefused:
                    status = "held"
        if status not in {"removed", "absent", "held"}:
            raise NativeTestingRebuildPending(
                "Observe the original volume removal before continuing."
            )
        return ProviderMutationOutcome(
            200,
            {"retired": status in {"removed", "absent"}},
            provider_effect_performed=bool(lease.evidence_reservation().provider_effect_started_at),
        )
