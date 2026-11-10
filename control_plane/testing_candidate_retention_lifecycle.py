from collections.abc import Callable
import json
from pathlib import Path
from typing import Any

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.retained_volume_sweep import RETAINED_VOLUME_SWEEP_ROUTE
from control_plane.dokploy import api, source
from control_plane.dokploy.testing_retained_copy import retained_copy_operation
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore


def candidate_lifecycle_key(parent_key: str, namespace: str, phase: str) -> str:
    return canonical_json_sha256({"parent": parent_key, "phase": phase, "candidate": namespace})


class CandidateRetentionLifecycle:
    def __init__(
        self,
        store: PostgresRecordStore,
        root: Path,
        custody: dict[str, Any],
        phase: str,
        guard: Callable[[], None],
    ) -> None:
        self.store, self.root, self.custody = store, root, custody
        self.phase, self.guard = phase, guard

    def target_key(self) -> str:
        return f"native-testing-retention:{self.custody['namespace']}:{self.phase}"

    def reconciliation_key(self) -> str:
        return json.dumps({"custody": self.custody, "phase": self.phase}, sort_keys=True)

    def read(self, mode: str) -> dict[str, Any]:
        host, token = source.read_dokploy_config(
            control_plane_root=self.root, database_url=self.store.database_url
        )
        return retained_copy_operation(
            host=host,
            token=token,
            server_id=self.custody["server"],
            payload={**self.custody["payload"], "mode": mode},
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        result = self.read("removal-readback" if self.phase == "remove" else "read")
        complete = result.get("availability") == "absent" or (
            self.phase == "stop" and result.get("stopped") is True
        )
        return (
            ProviderObservation("present", response_payload=result)
            if complete
            else ProviderObservation("unknown")
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        try:
            self.guard()
        except ValueError as error:
            raise ProviderMutationRejectedError(error) from error
        lease.assert_current()
        performed = False
        host, token = source.read_dokploy_config(
            control_plane_root=self.root, database_url=self.store.database_url
        )
        if self.phase == "stop":
            current = self.read("read")
            if current.get("stopped") is not True:
                self.guard()
                lease.checkpoint_effect("stop_failed_candidate")
                try:
                    api.dokploy_request(
                        host=host,
                        token=token,
                        path="/api/compose.stop",
                        method="POST",
                        payload={"composeId": self.custody["target"]},
                    )
                except Exception as error:
                    raise ProviderMutationUnknownError(
                        "Original candidate stop needs readback."
                    ) from error
                performed = True
            result = self.read("read")
            if result.get("stopped") is not True:
                raise ProviderMutationUnknownError("Original candidate stop is unverified.")
        else:

            def dispatch(effect: Callable[[], None]) -> None:
                self.guard()
                lease.checkpoint_effect("remove_failed_candidate_containers")
                effect()

            result = retained_copy_operation(
                host=host,
                token=token,
                server_id=self.custody["server"],
                payload={**self.custody["payload"], "mode": "remove"},
                dispatch_guard=dispatch,
            )
            performed = True
            if result.get("availability") != "absent":
                raise ProviderMutationUnknownError("Original candidate removal is unverified.")
            result = self.read("removal-readback")
            if result.get("availability") != "absent":
                raise ProviderMutationUnknownError("Original candidate absence is unverified.")
        return ProviderMutationOutcome(200, result, provider_effect_performed=performed)

    def execute(self, parent_key: str, *, trace_id: str, mutate: bool) -> bool:
        key = candidate_lifecycle_key(parent_key, self.custody["namespace"], self.phase)
        scope = "native-testing-retention-lifecycle"
        original = self.store.read_idempotency_record(
            scope=scope, route_path=RETAINED_VOLUME_SWEEP_ROUTE, idempotency_key=key
        )
        if original is None and not mutate:
            return False
        result = run_durable_provider_operation(
            store=self.store,
            scope=scope,
            route_path=RETAINED_VOLUME_SWEEP_ROUTE,
            idempotency_key=key,
            request_fingerprint=canonical_json_sha256(self.custody),
            lease_owner=trace_id,
            response_trace_id=trace_id,
            adapter=self,
            allow_mutation=mutate and original is None,
        )
        if result.status not in {"completed", "adopted", "replayed"}:
            raise ProviderMutationUnknownError("Read back the original candidate lifecycle effect.")
        return True
