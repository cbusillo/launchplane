from datetime import UTC, datetime
from typing import Any

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    OdooConfigParameterOverride,
    OdooOverrideValue,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.testing_address import (
    ADDRESS_ROUTE,
    ADDRESS_SCOPE,
    TestingAddressRecord,
    TestingAddressPlan,
    retained_address_custody_sha256,
)
from control_plane.contracts.testing_publication import (
    TestingPublicationRecord,
    TestingServingSnapshot,
)
from control_plane.dokploy.testing_route import TestingLaneRoute
from control_plane.testing_generation_retirement import RetainedTestingGeneration
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage import postgres as db
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    OdooInstanceOverrideWrite,
)

ADDRESS_PLAN_ROUTE = ADDRESS_ROUTE + ":plan"


def read_testing_address_recovery_copy(
    store: PostgresRecordStore, plan: TestingAddressPlan
) -> TestingServingSnapshot:
    from control_plane.testing_publication_storage import (
        read_snapshot,
        testing_binding_matches_route,
    )

    current = read_snapshot(store, product=plan.product, context=plan.context)
    applied = read_testing_address(store, plan.publication_key)
    address_applied = applied is not None and applied.state == "applied" and applied.plan == plan
    expected_binding = plan.binding_after if address_applied else plan.before.binding
    for snapshot in (current, plan.before):
        if (
            snapshot.provider.provider_id != "dokploy"
            or snapshot.provider.target_category != "compose"
            or snapshot.provider.target_id != snapshot.target_id.target_id
        ):
            raise ValueError("Protective address recovery requires consistent native copy custody.")
    if (
        current.target_id.target_id != plan.before.target_id.target_id
        or current.target.target_name != plan.before.target.target_name
        or current.target.project_name != plan.before.target.project_name
        or expected_binding is None
        or not testing_binding_matches_route(
            current.binding,
            expected_binding,
            plan.route_after if address_applied else plan.route_before,
        )
    ):
        raise ValueError("Protective address recovery cannot replace another copy or binding.")
    return current


def require_testing_address_health(
    plan: TestingAddressPlan, health: tuple[HealthcheckEvidence, ...]
) -> None:
    from control_plane.workflows.odoo_verification import DEFAULT_ODOO_RUNTIME_HEALTH_PATH

    identity = plan.before.inventory.runtime_identity if plan.before.inventory else None
    hosts = (plan.route_after.host, plan.route_before.host)
    if len(health) != len(hosts):
        raise ValueError("Both testing hostnames require public runtime identity evidence.")
    for hostname, item in zip(hosts, health, strict=True):
        if (
            not item.verified
            or item.status != "pass"
            or item.runtime_identity_status != "match"
            or item.observed_runtime_identity is None
            or identity is None
            or item.observed_runtime_identity.model_dump(exclude={"deployed_at"})
            != identity.model_dump(exclude={"deployed_at"})
            or item.urls != ("https://" + hostname + DEFAULT_ODOO_RUNTIME_HEALTH_PATH,)
        ):
            raise ValueError(
                "Address publication requires verified HTTPS and its current runtime at both hosts."
            )


def check_testing_address_sources(
    store: PostgresRecordStore, session: Any, plan: TestingAddressPlan
) -> None:
    from control_plane.testing_publication_storage import (
        PUBLICATION_ROUTE,
        PUBLICATION_SCOPE,
        _snapshot,
        serving_snapshot_matches,
    )

    current = _snapshot(store, session, plan.product, plan.context)
    if (
        current.overrides is None
        or current.target.policies.staff_testing_hold is not None
        or not serving_snapshot_matches(current, plan.before)
    ):
        raise ValueError("The original testing copy changed before address publication.")
    profile = session.get(db.LaunchplaneProductProfileRow, plan.product, with_for_update=True)
    if profile is None or store.read_product_profile_payload(profile.payload) != plan.profile:
        raise ValueError("The original testing profile changed before address publication.")
    publication = session.scalar(
        store._idempotency_statement(
            scope=PUBLICATION_SCOPE,
            route_path=PUBLICATION_ROUTE,
            idempotency_key=plan.publication_key,
            for_update=True,
        )
    )
    if publication is None:
        raise ValueError("The original native publication is unavailable.")
    original_publication = TestingPublicationRecord.model_validate(
        publication.payload.get("response_payload")
    )
    if (
        canonical_json_sha256(original_publication.model_dump(mode="json"))
        != plan.publication_sha256
    ):
        raise ValueError("The original native publication changed before address amendment.")
    if (
        original_publication.state != "applied"
        or original_publication.plan.route != plan.route_before
        or original_publication.plan.candidate.compose_id != current.target_id.target_id
    ):
        raise ValueError("The address amendment no longer belongs to the current native copy.")
    check_testing_address_retained_custody(store, session, plan)


def check_testing_address_retained_custody(
    store: PostgresRecordStore, session: Any, plan: TestingAddressPlan
) -> None:
    for key, digest in plan.operation_sha256.items():
        operation = session.get(
            db.LaunchplaneOdooStableTargetReplacementOperationRow, key, with_for_update=True
        )
        if operation is None:
            raise ValueError("An original retained operation is unavailable.")
        original = OdooStableTargetReplacementOperationRecord.model_validate(operation.payload)
        if retained_address_custody_sha256(original) != digest:
            raise ValueError("An original retained operation's custody changed.")
        if not store.testing_replacement_execution_is_excluded(key):
            raise ValueError("An original retained executor is not excluded.")
    retirement = session.get(
        db.LaunchplaneRetainedTestingGenerationRow,
        plan.retirement_operation_id,
        with_for_update=True,
    )
    if retirement is None:
        raise ValueError("The original retained testing generation is unavailable.")
    retained = RetainedTestingGeneration.model_validate(retirement.payload)
    if retained_address_custody_sha256(retained) != plan.retirement_sha256:
        raise ValueError("The original retained testing generation changed.")
    reservations = tuple(
        item
        for item in store._retained_disposable_hostname_reservations(session)
        if item.hostname == plan.route_after.host
    )
    if reservations != plan.reservations:
        raise ValueError("The original hostname reservation changed before publication.")


def verify_testing_address_sources(store: PostgresRecordStore, plan: TestingAddressPlan) -> None:
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        check_testing_address_sources(store, session, plan)


def verify_testing_address_retained_custody(
    store: PostgresRecordStore, plan: TestingAddressPlan
) -> None:
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        check_testing_address_retained_custody(store, session, plan)


def address_overrides(
    record: OdooInstanceOverrideRecord | None, *, context: str, base_url: str, stamp: str
) -> OdooInstanceOverrideRecord:
    if record is None:
        record = OdooInstanceOverrideRecord(context=context, instance="testing", updated_at=stamp)
    if (record.context, record.instance) != (context, "testing"):
        raise ValueError("Canonical address overrides require the same testing lane.")
    forced = {"web.base.url": base_url, "web.base.url.freeze": "True"}
    values: dict[str, Any] = {
        "config_parameters": tuple(
            item for item in record.config_parameters if item.key not in forced
        )
        + tuple(
            OdooConfigParameterOverride(
                key=key, value=OdooOverrideValue(source="literal", value=value)
            )
            for key, value in forced.items()
        ),
        "updated_at": stamp,
        "last_apply": record.last_apply.for_override_edit(),
    }
    if record.website_bootstrap is not None:
        values["website_bootstrap"] = record.website_bootstrap.model_copy(
            update={"canonical_url": base_url}
        )
    return OdooInstanceOverrideRecord.model_validate({**record.model_dump(), **values})


def _save_address_evidence(
    store: PostgresRecordStore, session: Any, record: TestingAddressRecord, *, plan_only: bool
) -> None:
    plan = record.plan
    route = ADDRESS_PLAN_ROUTE if plan_only else ADDRESS_ROUTE
    key = plan.digest if plan_only else plan.publication_key
    stamp = (record.applied_at or plan.created_at).isoformat()
    session.merge(
        store._idempotency_row(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_mutation_reservation_id(
                    scope=ADDRESS_SCOPE, route_path=route, idempotency_key=key
                ),
                scope=ADDRESS_SCOPE,
                route_path=route,
                idempotency_key=key,
                request_fingerprint=plan.digest,
                created_at=plan.created_at.isoformat(),
                updated_at=stamp,
                recorded_at=stamp,
                response_status_code=200,
                response_trace_id="testing-address:" + key,
                response_payload=record.model_dump(mode="json"),
            )
        )
    )


def save_address_plan(store: PostgresRecordStore, plan: TestingAddressPlan) -> None:
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_landing_authority(session, "testing-address-plan:" + plan.digest)
        _save_address_evidence(store, session, TestingAddressRecord(plan=plan), plan_only=True)
        session.commit()


def read_address_plan(store: PostgresRecordStore, digest: str) -> TestingAddressPlan:
    evidence = store.read_idempotency_record(
        scope=ADDRESS_SCOPE, route_path=ADDRESS_PLAN_ROUTE, idempotency_key=digest
    )
    if evidence is None:
        raise ValueError("The reviewed testing address plan is unavailable.")
    record = TestingAddressRecord.model_validate(evidence.response_payload)
    if evidence.request_fingerprint != digest or record.plan.digest != digest:
        raise ValueError("The reviewed testing address plan changed.")
    return record.plan


def require_address_publication_lease(
    store: PostgresRecordStore,
    session: Any,
    plan: TestingAddressPlan,
    reservation: LaunchplaneIdempotencyRecord,
) -> None:
    row = session.scalar(
        store._idempotency_statement(
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            for_update=True,
        )
    )
    lease = LaunchplaneIdempotencyRecord.model_validate(row.payload) if row else None
    if (
        lease is None
        or lease.state != "running"
        or lease.reconciliation_key != plan.digest
        or not store._mutation_reservation_matches(lease, reservation)
        or parse_launchplane_mutation_timestamp(
            lease.lease_expires_at, field_name="lease_expires_at"
        )
        <= parse_launchplane_mutation_timestamp(
            store._database_mutation_timestamp(session), field_name="observed_at"
        )
    ):
        raise ValueError("The original address operation no longer owns its publication.")


def settled_testing_address(
    store: PostgresRecordStore, plan: TestingAddressPlan
) -> TestingAddressRecord | None:
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        store._lock_landing_authority(session, "testing-address:" + plan.publication_key)
        row = session.scalar(
            store._idempotency_statement(
                scope=ADDRESS_SCOPE,
                route_path=ADDRESS_ROUTE,
                idempotency_key=plan.publication_key,
                for_update=True,
            )
        )
        if row is None:
            return None
        record = TestingAddressRecord.model_validate(row.payload["response_payload"])
        if record.state != "applied" or record.plan != plan:
            raise ValueError("The original address operation has another committed publication.")
        return record


def commit_testing_address(
    store: PostgresRecordStore,
    plan: TestingAddressPlan,
    reservation: LaunchplaneIdempotencyRecord,
    now: datetime,
    public_health: tuple[HealthcheckEvidence, ...],
) -> TestingAddressRecord:
    from control_plane.testing_publication_storage import _snapshot

    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        store._lock_route_binding_write(session, binding_key=plan.binding_after.binding_key)
        store._lock_landing_authority(session, "testing-address:" + plan.publication_key)
        require_address_publication_lease(store, session, plan, reservation)
        prior = session.scalar(
            store._idempotency_statement(
                scope=ADDRESS_SCOPE,
                route_path=ADDRESS_ROUTE,
                idempotency_key=plan.publication_key,
                for_update=True,
            )
        )
        if prior is not None:
            record = TestingAddressRecord.model_validate(prior.payload["response_payload"])
            if record.plan != plan or record.state != "applied":
                raise ValueError("This testing publication already has another address receipt.")
            return record
        check_testing_address_sources(store, session, plan)
        require_testing_address_health(plan, public_health)
        current = _snapshot(store, session, plan.product, plan.context)
        assert current.overrides is not None
        stamp = now.astimezone(UTC).isoformat()
        profile = plan.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"base_url": "https://" + plan.route_after.host})
                    if (lane.context, lane.instance) == (plan.context, "testing")
                    else lane
                    for lane in plan.profile.lanes
                ),
                "updated_at": stamp,
            }
        )
        overrides = address_overrides(
            current.overrides,
            context=plan.context,
            base_url="https://" + plan.route_after.host,
            stamp=stamp,
        )
        store._write_product_authority_bundle(
            ProductAuthorityBundle(
                required_context_owner=(plan.product, plan.context),
                expected_product_profiles=(plan.profile,),
                product_profiles=(profile,),
                odoo_instance_override_writes=(
                    OdooInstanceOverrideWrite(
                        record=overrides,
                        expected_record=current.overrides,
                    ),
                ),
            ),
            write_session=session,
            commit=False,
            write_transaction_started=True,
        )
        from control_plane.route_binding_reconcile import _EVIDENCE_FRESHNESS_WINDOW

        binding = plan.binding_after.model_copy(
            update={
                "updated_at": stamp,
                "source": plan.binding_after.source.model_copy(
                    update={
                        "refreshed_at": stamp,
                        "freshness_status": "verified",
                        "stale_after": (now + _EVIDENCE_FRESHNESS_WINDOW).isoformat(),
                        "source_versions": {
                            **plan.binding_after.source.source_versions,
                            "testing_address_observation": canonical_json_sha256(
                                {
                                    "plan": plan.digest,
                                    "health": [
                                        item.model_dump(mode="json") for item in public_health
                                    ],
                                    "observed_at": stamp,
                                }
                            ),
                        },
                    }
                ),
            }
        )
        session.merge(store._route_binding_row(binding))
        result = TestingAddressRecord(
            plan=plan, state="applied", applied_at=now, public_health=public_health
        )
        _save_address_evidence(store, session, result, plan_only=False)
        require_address_publication_lease(store, session, plan, reservation)
        session.commit()
        return result


def read_testing_address(
    store: PostgresRecordStore, publication_key: str
) -> TestingAddressRecord | None:
    evidence = store.read_idempotency_record(
        scope=ADDRESS_SCOPE, route_path=ADDRESS_ROUTE, idempotency_key=publication_key
    )
    if evidence is None:
        return None
    record = TestingAddressRecord.model_validate(evidence.response_payload)
    if record.plan.publication_key != publication_key or (
        evidence.request_fingerprint != record.plan.digest
    ):
        raise ValueError("The original testing address receipt changed.")
    return record


def effective_testing_route(
    store: PostgresRecordStore, publication: TestingPublicationRecord
) -> TestingLaneRoute:
    binding = store.read_route_binding_record(
        product=publication.plan.candidate.product,
        context_name=publication.plan.candidate.context,
        instance_name="testing",
    )
    key = binding.source.source_versions.get("testing_address")
    if not key:
        return publication.plan.route
    record = read_testing_address(store, key)
    if (
        record is None
        or record.state != "applied"
        or (
            key != publication.plan.key
            or record.plan.publication_sha256
            != canonical_json_sha256(publication.model_dump(mode="json"))
            or record.plan.route_before != publication.plan.route
        )
    ):
        raise ValueError("The testing address has no applied original publication receipt.")
    return record.plan.route_after


def amended_testing_maintenance_overrides(
    store: object, context: str, record: OdooInstanceOverrideRecord | None
) -> OdooInstanceOverrideRecord | None:
    if not isinstance(store, PostgresRecordStore) or record is None or record.instance != "testing":
        return record
    bindings = tuple(
        item
        for item in store.list_route_binding_records(context_name=context, instance_name="testing")
        if item.source.source_versions.get("testing_address")
    )
    if not bindings:
        return record
    if len(bindings) != 1:
        raise ValueError("Canonical testing address maintenance has ambiguous ownership.")
    from control_plane.testing_serving_copy import published_testing_copy

    publication = published_testing_copy(store, product=bindings[0].product, context=context)
    if publication is None:
        raise ValueError("Canonical testing address maintenance lost its publication.")
    route = effective_testing_route(store, publication)
    return address_overrides(
        record, context=context, base_url="https://" + route.host, stamp=record.updated_at
    )
