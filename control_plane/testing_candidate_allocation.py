"""Allocate independent testing storage while the current lane keeps serving."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.dokploy import api, source
from control_plane.dokploy.sandbox_placement import create_isolated_placement, created_placement
from control_plane.dokploy.target_source_setup import require_empty_compose_source
from control_plane.dokploy_target_setup_http import fetch_dokploy_compose_domains_for_target_setup
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.provider_operations import ProviderOperationLease
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_replacement_resources import (
    IncompleteTestingResources,
    run_resource_schedule,
)


def candidate_resources(record: TestingCandidateAllocation) -> OdooTestingResources:
    return OdooTestingResources(
        product=record.product,
        context=record.context,
        operation_id=record.operation_id,
        target_id=record.compose_id,
        namespace=record.namespace,
    )


def prepare_candidate_storage(
    *,
    root: Path,
    store: PostgresRecordStore,
    allocation: TestingCandidateAllocation,
    lease: ProviderOperationLease,
    guard: Callable[[], None],
) -> TestingCandidateAllocation:
    """Run within the caller's authorized, exclusively owned lane operation.

    ``guard`` rechecks the operation's intent, serving authority, holds, use,
    custody and isolation. The durable provider lease remains responsible for
    original-key observation after an uncertain call. This stage never changes
    serving target records, runtime values, URLs or the public route.
    """

    def checkpoint() -> None:
        lease.assert_current()
        guard()

    checkpoint()
    existing = store.read_testing_candidate(allocation.namespace)
    record: TestingCandidateAllocation
    if existing is None:
        if (
            allocation.effect
            or allocation.project_id
            or allocation.environment_id
            or allocation.compose_id
            or allocation.resources_binding_sha256
        ):
            raise ValueError("Candidate allocation must begin before any provider effect.")
        store.compare_and_write_testing_candidate(None, allocation)
        record = allocation
    else:
        record = existing
        record.require_successor(allocation)
    if record.effect in {"project_id", "environment_id", "compose_id"} and not getattr(
        record, record.effect
    ):
        raise ValueError("Candidate creation is uncertain; use original-key observation.")

    def save(**updates: str) -> None:
        nonlocal record
        checkpoint()
        updated = TestingCandidateAllocation.model_validate({**record.model_dump(), **updates})
        store.compare_and_write_testing_candidate(record, updated)
        record = updated

    def before_create(field: str) -> None:
        save(effect=field)
        lease.checkpoint_effect(field)

    if not record.compose_id:
        create_isolated_placement(
            root,
            namespace=record.namespace,
            server_id=record.server_id,
            description="Independent testing candidate",
            checkpoint=checkpoint,
            before_create=before_create,
            record_identity=lambda field, identifier: save(**{field: identifier}),
            recorded_placement=(record.project_id, record.environment_id, record.compose_id),
        )
    resources = candidate_resources(record)
    definition = DokployTargetDefinition(
        context=record.context,
        instance="testing",
        target_type="compose",
        target_id=record.compose_id,
        project_name=record.namespace,
        target_name=record.namespace,
    )

    def run(mode: Literal["prepare", "readback"]) -> None:
        # Do not allow caller-supplied source, domains or retained environment to
        # turn storage qualification into a deployment of the existing lane.
        if mode not in {"prepare", "readback"}:
            raise ValueError("Unsupported candidate storage operation.")
        run_resource_schedule(
            root=root,
            store=store,
            resources=resources,
            mode=mode,
            checkpoint=checkpoint,
            candidate_target=definition,
            candidate_server_id=record.server_id,
        )

    if record.effect == "storage":
        try:
            run("readback")
        except IncompleteTestingResources:
            if record.resources_binding_sha256:
                raise ValueError("Previously qualified candidate storage changed.") from None
        else:
            save(resources_binding_sha256=resources.binding_sha256)
            return record
    save(effect="storage")
    lease.checkpoint_effect("independent_storage")
    run("prepare")
    save(resources_binding_sha256=resources.binding_sha256)
    return record


def observe_candidate_creation(
    *,
    root: Path,
    store: PostgresRecordStore,
    namespace: str,
    guard: Callable[[], None],
) -> TestingCandidateAllocation | None:
    """Recover returned identities from the original namespace, never recreate.

    Absence after an uncertain request is not permission to dispatch again.
    The caller keeps that provider operation unknown when this returns None.
    """
    guard()
    record = store.read_testing_candidate(namespace)
    if record is None or record.effect not in {"project_id", "environment_id", "compose_id"}:
        return None
    fields = ("project_id", "environment_id", "compose_id")
    observed = created_placement(root, namespace)
    index = fields.index(record.effect)
    if not observed[index]:
        return None
    if any(observed[index + 1 :]) or any(
        getattr(record, field) and getattr(record, field) != actual
        for field, actual in zip(fields, observed, strict=True)
    ):
        raise ValueError("Candidate placement disagrees with its original creation.")
    if observed[2]:
        host, token = source.read_dokploy_config(control_plane_root=root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=observed[2]
        )
        require_empty_compose_source(payload, observed[2])
        if (
            payload.get("environmentId") != observed[1]
            or payload.get("serverId") != record.server_id
            or payload.get("env") not in (None, "")
        ):
            raise ValueError("Unrecorded candidate has changed placement or acquired credentials.")
        if api.list_deployments_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=observed[2],
            require_complete=True,
        ) or fetch_dokploy_compose_domains_for_target_setup(
            host=host, token=token, compose_id=observed[2]
        ):
            raise ValueError("Unrecorded candidate has runtime or route history.")
    guard()
    updated = TestingCandidateAllocation.model_validate(
        {**record.model_dump(), **dict(zip(fields, observed, strict=True))}
    )
    store.compare_and_write_testing_candidate(record, updated)
    return updated
