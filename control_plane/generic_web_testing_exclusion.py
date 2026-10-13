"""Inspect current dispatch exclusion without interpreting historical outcomes."""

from contextlib import ExitStack
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.generic_web_testing_exclusion import (
    GenericWebTestingExclusionResponse,
    TestingExclusionFacet,
)
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.dokploy import api as dokploy_api, source as dokploy_source
from control_plane.generic_web_deploy_http import (
    GENERIC_WEB_DEPLOY_ROUTE,
    resolve_generic_web_deploy_lane,
)
from control_plane.integration_allowances import canonical_sha256, utc_now_timestamp
from control_plane.product_config_service import resolve_product_config_profile
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.workflows.generic_web_deploy_provider import (
    decode_generic_web_provider_reconciliation_target,
    generic_web_provider_target_key,
)
from control_plane.workflows.generic_web_post_deploy import (
    generic_web_post_deploy_executor_for_profile,
)


class TestingExclusionRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _bind(
    store: PostgresRecordStore, product: str, context: str, instance: str
) -> tuple[
    LaunchplaneProductProfileRecord,
    DokployTargetRecord,
    DokployTargetIdRecord,
    ProviderTargetRecord,
]:
    if instance != "testing":
        raise TestingExclusionRefused("testing_exclusion_not_testing")
    profile = resolve_product_config_profile(
        store, product=product, context=context, instance=instance
    )
    _, lane = resolve_generic_web_deploy_lane(
        record_store=store, product=product, instance=instance
    )
    if lane.context != context:
        raise TestingExclusionRefused("testing_exclusion_lane_mismatch")
    if (
        generic_web_post_deploy_executor_for_profile(profile, instance=instance) is not None
        or lane.generic_web_extensions.configured
    ):
        raise TestingExclusionRefused("testing_exclusion_effect_family_unsupported")
    target = store.read_dokploy_target_record(context_name=context, instance_name=instance)
    target_id = store.read_dokploy_target_id_record(context_name=context, instance_name=instance)
    provider = store.read_provider_target_record(context_name=context, instance_name=instance)
    if (
        target.target_type != "compose"
        or provider.provider_id != "dokploy"
        or provider.target_category != "compose"
        or target_id.target_id != provider.target_id
    ):
        raise TestingExclusionRefused("testing_exclusion_target_mismatch")
    physical: tuple[ProviderTargetRecord | DokployTargetIdRecord, ...] = (
        *store.list_provider_target_records(),
        *store.list_dokploy_target_id_records(),
    )
    if any(
        item.target_id == target_id.target_id
        and (item.context, item.instance) != (context, instance)
        for item in physical
    ):
        raise TestingExclusionRefused("testing_exclusion_shared_target")
    return profile, target, target_id, provider


def _digest(records: tuple[BaseModel, ...]) -> str:
    return canonical_sha256([record.model_dump(mode="json") for record in records])


def _target_reservations(
    store: PostgresRecordStore, *, product: str, context: str, target_key: str, target_id: str
) -> tuple[LaunchplaneIdempotencyRecord, ...]:
    from control_plane.product_reconcile import reconcile_reservation_scope, RECONCILE_SOURCE

    prefix = f"{RECONCILE_SOURCE}:{product}:{context}:testing:"
    selected = []
    for row in store.list_held_provider_target_reservations():
        matches = row.provider_target_key == target_key or (
            row.scope == reconcile_reservation_scope(product)
            and row.idempotency_key.startswith(prefix)
        )
        if not matches and row.route_path == GENERIC_WEB_DEPLOY_ROUTE:
            try:
                snapshot = decode_generic_web_provider_reconciliation_target(row.reconciliation_key)
                matches = snapshot.provider_id == "dokploy" and snapshot.target_id == target_id
            except ValueError:
                pass
        if matches:
            selected.append(row)
    return tuple(selected)


def _inventory(read: Callable[[], object], *, kind: str, target_id: str) -> TestingExclusionFacet:
    try:
        rows = read()
    except (click.ClickException, OSError, ValueError):
        return TestingExclusionFacet(code="provider_read_unavailable")
    # These Dokploy endpoints return whole arrays. Envelopes, truncation markers,
    # over-bound arrays and malformed rows cannot be negative evidence.
    if (
        not isinstance(rows, list)
        or len(rows) > 10000
        or any(not isinstance(row, dict) for row in rows)
    ):
        return TestingExclusionFacet(code="provider_inventory_unproved")
    active = False
    count = len(rows)
    if kind == "deployments":
        ids: set[str] = set()
        for row in rows:
            deployment_id = row.get("deploymentId")
            if (
                row.get("composeId") != target_id
                or not isinstance(deployment_id, str)
                or not deployment_id
                or deployment_id in ids
            ):
                return TestingExclusionFacet(code="provider_inventory_unproved")
            ids.add(deployment_id)
            status = dokploy_api.deployment_status(row)
            if status in {"running", "queued", "pending"}:
                active = True
                continue
            if status not in {"done", "error"}:
                return TestingExclusionFacet(code="provider_inventory_unproved")
            try:
                started = parse_launchplane_mutation_timestamp(
                    row.get("startedAt", ""), field_name="startedAt"
                )
                finished = parse_launchplane_mutation_timestamp(
                    row.get("finishedAt", ""), field_name="finishedAt"
                )
                if finished < started:
                    raise ValueError("Out of order")
            except (ValueError, TypeError, AttributeError):
                return TestingExclusionFacet(code="provider_inventory_unproved")
    elif kind == "queue":
        count = 0
        for row in rows:
            data = row.get("data")
            if (
                not isinstance(data, dict)
                or not any(
                    isinstance(data.get(key), str) and data[key]
                    for key in ("composeId", "applicationId")
                )
                or not isinstance(row.get("state"), str)
            ):
                return TestingExclusionFacet(code="provider_inventory_unproved")
            if data.get("composeId") == target_id:
                count += 1
                active |= row["state"] not in {"completed", "failed"}
    else:
        # Any schedule, even disabled, keeps staging unproved. No provider write
        # is made to drain or disable it.
        active = bool(rows)
    return TestingExclusionFacet(
        state="unknown" if active else "quiescent",
        code="provider_effect_active" if active else "proved",
        complete=True,
        count=count,
    )


def _provider_target(payload: object, target_id: str) -> TestingExclusionFacet:
    if (
        not isinstance(payload, dict)
        or payload.get("composeId") != target_id
        or payload.get("composeType") != "docker-compose"
        or not isinstance(payload.get("appName"), str)
        or not payload["appName"].strip()
    ):
        return TestingExclusionFacet(code="provider_target_unproved")
    if payload.get("composeStatus") not in {"idle", "error", "done"}:
        return TestingExclusionFacet(code="provider_effect_active")
    return TestingExclusionFacet(state="quiescent", code="proved", complete=True)


def inspect_generic_web_testing_exclusion(
    *,
    store: PostgresRecordStore,
    root: Path,
    product: str,
    context: str,
    instance: str,
    trace_id: str,
) -> GenericWebTestingExclusionResponse:
    bindings = _bind(store, product, context, instance)
    _, target, target_id_record, provider = bindings
    target_id = target_id_record.target_id
    target_key = generic_web_provider_target_key(
        provider_id=provider.provider_id,
        context=context,
        instance=instance,
        target_type=target.target_type,
        target_id=target_id,
    )
    # Include repointed historical lane reservations as well as this physical
    # target. A new binding must never hide an old executor.
    from control_plane.product_reconcile import reconcile_reservation_scope, RECONCILE_SOURCE

    prefix = f"{RECONCILE_SOURCE}:{product}:{context}:testing:"
    reservations = _target_reservations(
        store, product=product, context=context, target_key=target_key, target_id=target_id
    )
    response = GenericWebTestingExclusionResponse(
        trace_id=trace_id,
        product=product,
        context=context,
        observed_at=utc_now_timestamp(),
        binding_sha256=_digest(bindings),
        reservation_sha256=_digest(reservations),
        hold=TestingExclusionFacet(state="held", code="proved", complete=True)
        if target.policies.staff_testing_hold
        else TestingExclusionFacet(code="hold_absent", complete=True),
        execution=TestingExclusionFacet(code="reservation_fence_absent"),
        deployments=TestingExclusionFacet(),
        queue=TestingExclusionFacet(),
        schedules=TestingExclusionFacet(),
        provider_target=TestingExclusionFacet(),
    )
    # Keep the existing executor lock for the whole provider observation. This
    # does not renew, retire, reclassify or acquire any mutation reservation.
    with ExitStack() as stack:
        check = stack.enter_context(
            store.testing_replacement_execution_lock(testing_event_execution_lock_name(target_key))
        )
        if check is None:
            response.execution = TestingExclusionFacet(code="execution_lock_busy")
        elif reservations:
            try:
                for row in reservations:
                    snapshot = decode_generic_web_provider_reconciliation_target(
                        row.reconciliation_key
                    )
                    lookup = store.lookup_existing_mutation_reservation(
                        route_path=row.route_path,
                        idempotency_key=row.idempotency_key,
                        request_fingerprint=row.request_fingerprint,
                    )
                    if (
                        row.state != "reconcile_required"
                        or row.scope != reconcile_reservation_scope(product)
                        or row.route_path != GENERIC_WEB_DEPLOY_ROUTE
                        or row.provider_target_key != target_key
                        or not row.idempotency_key.startswith(prefix)
                        or snapshot.target_type != target.target_type
                        or (
                            snapshot.context,
                            snapshot.instance,
                            snapshot.target_id,
                            snapshot.provider_id,
                        )
                        != (context, instance, target_id, provider.provider_id)
                        or snapshot.product not in {"", product}
                        or lookup.record != row
                        or not row.lease_expires_at
                        or parse_launchplane_mutation_timestamp(
                            row.lease_expires_at, field_name="lease"
                        )
                        >= parse_launchplane_mutation_timestamp(
                            lookup.observed_at, field_name="now"
                        )
                    ):
                        raise ValueError("Unproved execution owner")
                check()
                response.execution = TestingExclusionFacet(
                    state="excluded", code="proved", complete=True, count=len(reservations)
                )
            except (ValueError, RuntimeError, SQLAlchemyError, OSError):
                response.execution = TestingExclusionFacet(code="execution_ownership_unproved")
        try:
            host, token = dokploy_source.read_dokploy_config(control_plane_root=root)
        except (click.ClickException, OSError, ValueError):
            response.provider_target = TestingExclusionFacet(code="provider_read_unavailable")
            return response

        def read_target() -> object:
            return dokploy_api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=target_id
            )

        try:
            before = read_target()
            response.provider_target = _provider_target(before, target_id)
        except (click.ClickException, OSError, ValueError):
            before = None
            response.provider_target = TestingExclusionFacet(code="provider_read_unavailable")
        inspections: tuple[tuple[str, str, dict[str, str | int]], ...] = (
            ("deployments", "/api/deployment.allByType", {"id": target_id, "type": "compose"}),
            ("queue", "/api/deployment.queueList", {}),
            ("schedules", "/api/schedule.list", {"id": target_id, "scheduleType": "compose"}),
        )
        for name, path, query in inspections:
            facet = _inventory(
                lambda: dokploy_api.dokploy_request(host=host, token=token, path=path, query=query),
                kind=name,
                target_id=target_id,
            )
            setattr(response, name, facet)
        try:
            after = read_target()
            # Compare privately; provider env/source contents never leave here.
            if before is not None and before != after:
                response.provider_target = TestingExclusionFacet(code="binding_changed")
                response.execution = TestingExclusionFacet(code="binding_changed")
        except (click.ClickException, OSError, ValueError):
            response.provider_target = TestingExclusionFacet(code="provider_read_unavailable")
        try:
            if (
                _bind(store, product, context, instance) != bindings
                or _target_reservations(
                    store,
                    product=product,
                    context=context,
                    target_key=target_key,
                    target_id=target_id,
                )
                != reservations
            ):
                response.provider_target = TestingExclusionFacet(code="binding_changed")
                response.execution = TestingExclusionFacet(code="binding_changed")
        except (click.ClickException, OSError, ValueError, FileNotFoundError):
            response.provider_target = TestingExclusionFacet(code="binding_changed")
            response.execution = TestingExclusionFacet(code="binding_changed")
        if check is not None:
            try:
                check()
            except (RuntimeError, SQLAlchemyError, OSError):
                response.execution = TestingExclusionFacet(code="execution_lock_lost")
        if all(
            facet.code == "proved"
            for facet in (
                response.hold,
                response.execution,
                response.deployments,
                response.queue,
                response.schedules,
                response.provider_target,
            )
        ):
            response.status = "excluded"
        return response
