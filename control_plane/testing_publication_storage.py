"""One transaction adopts serving metadata; its receipt resolves lost replies."""

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import or_, select

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.contracts.testing_publication import (
    TestingPublicationPlan,
    TestingPublicationRecord,
    TestingServingSnapshot,
)
from control_plane.contracts.testing_rebuild import TestingRouteObservation
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretRecord, SecretBinding
from control_plane.storage import postgres as db
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.route_binding_reconcile import route_binding_authority_projection
from control_plane.testing_candidate_runtime import (
    testing_profile_binding,
    testing_serving_target_authority,
)
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
    RuntimeEnvironmentWrite,
    require_bundle_context_owner,
)

PUBLICATION_SCOPE = "launchplane:testing-serving-publication:v1"
PUBLICATION_ROUTE = "service:testing-serving-publication"


def _same(left: object, right: object) -> bool:
    return canonical_json_sha256(left) == canonical_json_sha256(right)


def testing_binding_authority(
    binding: EnvironmentRouteBindingRecord | None,
) -> tuple[object, ...] | None:
    """Share route ownership and publication custody across serving-copy checks."""
    if binding is None:
        return None
    return (
        route_binding_authority_projection(binding),
        binding.source.source_kind,
        binding.source.source_label,
        binding.source.source_versions.get("testing_publication"),
        binding.source.source_versions.get("testing_route"),
    )


def serving_snapshot_matches(
    actual: TestingServingSnapshot, expected: TestingServingSnapshot
) -> bool:
    """Serving authority and custody identify a copy; observation metadata does not."""
    values = []
    for snapshot in (actual, expected):
        payload = snapshot.model_dump(mode="json")
        payload["target"] = testing_serving_target_authority(snapshot.target)
        payload["binding"] = testing_binding_authority(snapshot.binding)
        values.append(payload)
    return _same(*values)


def _snapshot(
    store: db.PostgresRecordStore, session: Any, product: str, context: str
) -> TestingServingSnapshot:
    payload: dict[str, object] = {}
    for name, orm, identity in (
        ("target", db.LaunchplaneDokployTargetRow, (context, "testing")),
        ("target_id", db.LaunchplaneDokployTargetIdRow, (context, "testing")),
        ("provider", db.LaunchplaneProviderTargetRow, (context, "testing")),
        ("runtime", db.LaunchplaneRuntimeEnvironmentRow, ("instance", context, "testing")),
        ("overrides", db.LaunchplaneOdooInstanceOverrideRow, (context, "testing")),
        ("inventory", db.LaunchplaneInventoryRow, (context, "testing")),
        ("generation", db.LaunchplaneFreshOdooTestingGenerationRow, (product, context)),
        ("binding", db.LaunchplaneRouteBindingRow, (product, context, "testing")),
    ):
        row = session.get(orm, identity, with_for_update=True)
        payload[name] = row.payload if row is not None else None
    return TestingServingSnapshot.model_validate(payload)


def read_snapshot(
    store: db.PostgresRecordStore, *, product: str, context: str
) -> TestingServingSnapshot:
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        return _snapshot(store, session, product, context)


def _lease(
    store: db.PostgresRecordStore,
    session: Any,
    plan: TestingPublicationPlan,
    reservation: LaunchplaneIdempotencyRecord,
) -> None:
    if (reservation.record_id, reservation.provider_target_key) != (
        plan.mutation_record_id,
        plan.provider_target_key,
    ):
        raise ValueError("Publication belongs to the original provider operation.")
    row = session.scalar(
        store._idempotency_statement(
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            for_update=True,
        )
    )
    current = LaunchplaneIdempotencyRecord.model_validate(row.payload) if row else None
    if (
        current is None
        or current.state != "running"
        or not store._mutation_reservation_matches(current, reservation)
        or parse_launchplane_mutation_timestamp(
            current.lease_expires_at, field_name="lease_expires_at"
        )
        <= parse_launchplane_mutation_timestamp(
            store._database_mutation_timestamp(session), field_name="observed_at"
        )
    ):
        raise ValueError("Provider operation no longer owns this publication.")


def _row(store: db.PostgresRecordStore, session: Any, plan: TestingPublicationPlan) -> Any:
    return session.scalar(
        store._idempotency_statement(
            scope=PUBLICATION_SCOPE,
            route_path=PUBLICATION_ROUTE,
            idempotency_key=plan.key,
            for_update=True,
        )
    )


def _record(row: Any, plan: TestingPublicationPlan) -> TestingPublicationRecord | None:
    if row is None:
        return None
    evidence = LaunchplaneIdempotencyRecord.model_validate(row.payload)
    record = TestingPublicationRecord.model_validate(evidence.response_payload)
    if evidence.request_fingerprint != plan.digest or record.plan != plan:
        raise ValueError("Publication cannot replace its original serving preimages.")
    return record


def _save(store: db.PostgresRecordStore, session: Any, record: TestingPublicationRecord) -> None:
    plan = record.plan
    session.merge(
        store._idempotency_row(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_mutation_reservation_id(
                    scope=PUBLICATION_SCOPE, route_path=PUBLICATION_ROUTE, idempotency_key=plan.key
                ),
                scope=PUBLICATION_SCOPE,
                route_path=PUBLICATION_ROUTE,
                idempotency_key=plan.key,
                request_fingerprint=plan.digest,
                created_at=plan.created_at.isoformat(),
                updated_at=record.updated_at.isoformat(),
                recorded_at=record.updated_at.isoformat(),
                response_status_code=200,
                response_trace_id="testing-publication:" + plan.key,
                response_payload=record.model_dump(mode="json"),
            )
        )
    )


def _sources(
    store: db.PostgresRecordStore,
    session: Any,
    plan: TestingPublicationPlan,
    *,
    runtime_after: bool = False,
) -> None:
    from control_plane.testing_publication import require_original_candidate_inputs

    require_original_candidate_inputs(plan)
    profiles = tuple(
        LaunchplaneProductProfileRecord.model_validate(row.payload)
        for row in session.scalars(select(db.LaunchplaneProductProfileRow))
    )
    require_bundle_context_owner(
        ProductAuthorityBundle(
            required_context_owner=(plan.candidate.product, plan.candidate.context)
        ),
        profiles,
    )
    current_profile = next(
        (item for item in profiles if item.product == plan.profile.product), None
    )
    if current_profile is None or testing_profile_binding(
        current_profile, plan.candidate.context
    ) != testing_profile_binding(plan.profile, plan.candidate.context):
        raise ValueError("Product authority changed before publication.")
    candidate = session.get(
        db.LaunchplaneTestingCandidateRow, plan.candidate.namespace, with_for_update=True
    )
    if candidate is None or not _same(candidate.payload, store._payload_dict(plan.candidate)):
        raise ValueError("Candidate custody or private readiness changed.")
    expected = plan.runtime_read_set
    if runtime_after:
        expected = expected.model_copy(
            update={
                "records": tuple(
                    item
                    for item in expected.records
                    if (item.scope, item.context, item.instance)
                    != ("instance", plan.candidate.context, "testing")
                )
                + ((plan.after.runtime,) if plan.after.runtime else ())
            }
        )
    if not expected.matches(
        RuntimeEnvironmentRecord.model_validate(row.payload)
        for row in session.scalars(select(db.LaunchplaneRuntimeEnvironmentRow))
    ):
        raise ValueError("Runtime input selection changed before publication.")
    if not plan.secret_read_set.matches(
        (
            SecretRecord.model_validate(row.payload)
            for row in session.scalars(select(db.LaunchplaneSecretRow))
        ),
        (
            SecretBinding.model_validate(row.payload)
            for row in session.scalars(select(db.LaunchplaneSecretBindingRow))
        ),
    ):
        raise ValueError("Credential selection changed before publication.")


def prepare_publication(
    store: db.PostgresRecordStore,
    *,
    plan: TestingPublicationPlan,
    reservation: LaunchplaneIdempotencyRecord,
) -> TestingPublicationRecord:
    plan = TestingPublicationPlan.model_validate(plan.model_dump())
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        store._lock_landing_authority(session, "testing-publication:" + plan.key)
        _lease(store, session, plan, reservation)
        existing = _record(_row(store, session, plan), plan)
        if existing is not None:
            return existing
        _sources(store, session, plan)
        if not serving_snapshot_matches(
            _snapshot(store, session, plan.candidate.product, plan.candidate.context),
            plan.before,
        ):
            raise ValueError("Serving authority changed before publication preparation.")
        record = TestingPublicationRecord(plan=plan, updated_at=plan.created_at)
        _save(store, session, record)
        session.commit()
        return record


def _public(
    plan: TestingPublicationPlan, observation: TestingRouteObservation, now: datetime
) -> HealthcheckEvidence:
    observation = TestingRouteObservation.model_validate(observation.model_dump())
    app = plan.candidate.application
    assert app is not None
    health = observation.public_health
    origin = urlsplit(app.plan.base_url)
    if (
        (observation.route_id, observation.host, observation.target_url, observation.slot_id)
        != (plan.route.route_id, plan.route.host, plan.route.new_backend, plan.candidate.namespace)
        or not timedelta(0) <= now - observation.observed_at <= timedelta(minutes=2)
        or health is None
        or not health.verified
        or health.status != "pass"
        or health.observed_runtime_identity != app.plan.runtime_identity
        or not health.urls
    ):
        raise ValueError("Publication needs fresh applied routing and verified candidate health.")
    for url in health.urls:
        parsed = urlsplit(url)
        if (
            (parsed.scheme, parsed.hostname, parsed.port or 443)
            != (origin.scheme, origin.hostname, origin.port or 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Public readiness must use the unchanged testing origin.")
    return health


def _retain_old(
    store: db.PostgresRecordStore, session: Any, plan: TestingPublicationPlan
) -> tuple[tuple[str, str], ...]:
    old = plan.before.provider
    retained_context = (
        "lp-testing-retained-"
        + canonical_json_sha256(
            {
                "product": plan.candidate.product,
                "context": plan.candidate.context,
                "target": old.target_id,
            }
        )[:32]
    )
    retained = old.model_copy(
        update={
            "context": retained_context,
            "instance": "retained",
            "source_label": "service:testing-rebuild-retained",
        }
    )
    aliases: list[tuple[str, str]] = []
    for row in session.scalars(
        select(db.LaunchplaneProviderTargetRow).where(
            db.LaunchplaneProviderTargetRow.provider_id == old.provider_id,
            db.LaunchplaneProviderTargetRow.target_category == old.target_category,
            db.LaunchplaneProviderTargetRow.target_id == old.target_id,
        )
    ):
        if (row.context, row.instance) == (old.context, old.instance):
            continue
        if (row.context, row.instance) == (retained_context, "retained"):
            if not _same(row.payload, store._payload_dict(retained)):
                raise ValueError("Retained predecessor reservation changed.")
            aliases.append((row.context, row.instance))
            continue
        candidate = session.get(db.LaunchplaneTestingCandidateRow, row.context)
        allocation = (
            TestingCandidateAllocation.model_validate(candidate.payload) if candidate else None
        )
        if (
            row.instance != "testing-candidate"
            or allocation is None
            or (allocation.product, allocation.context, allocation.compose_id)
            != (plan.candidate.product, plan.candidate.context, old.target_id)
        ):
            raise ValueError("Predecessor physical custody has an unqualified alias.")
        aliases.append((row.context, row.instance))
    if aliases:
        return tuple(aliases)
    row = session.get(db.LaunchplaneProviderTargetRow, (retained_context, "retained"))
    if row is not None:
        if not _same(row.payload, store._payload_dict(retained)):
            raise ValueError("Retained predecessor reservation changed.")
    else:
        store._write_provider_target_with_expectation(
            session=session,
            write=ProviderTargetWrite(
                record=retained,
                expected_absent=True,
                allowed_conflicting_routes=((old.context, old.instance),),
            ),
        )
    return ((retained_context, "retained"),)


def publish(
    store: db.PostgresRecordStore,
    *,
    plan: TestingPublicationPlan,
    reservation: LaunchplaneIdempotencyRecord,
    now: datetime,
    observation: TestingRouteObservation | None = None,
    inverse: bool = False,
) -> TestingPublicationRecord:
    plan = TestingPublicationPlan.model_validate(plan.model_dump())
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        assert plan.after.binding is not None
        store._lock_route_binding_write(session, binding_key=plan.after.binding.binding_key)
        store._lock_landing_authority(session, "testing-publication:" + plan.key)
        _lease(store, session, plan, reservation)
        prior = _record(_row(store, session, plan), plan)
        if prior is None:
            raise ValueError("Publication must retain preimages before traffic changes.")
        if (inverse and prior.state == "reversed") or (not inverse and prior.state == "applied"):
            return prior
        if not inverse and prior.state == "reversed":
            raise ValueError("A reversed publication cannot be replayed forward.")
        if inverse and prior.state == "planned":
            # This row lock has waited for any original forward transaction.
            # A committed planned row proves it never adopted serving authority.
            result = prior.model_copy(update={"state": "reversed", "updated_at": now})
            _save(store, session, result)
            session.commit()
            return result
        _sources(store, session, plan, runtime_after=prior.state == "applied")
        expected = plan.before if prior.state == "planned" else effective_after(prior)
        current = _snapshot(store, session, plan.candidate.product, plan.candidate.context)
        if not serving_snapshot_matches(current, expected):
            raise ValueError("Serving authority changed since the original publication.")
        health = (
            prior.public_health
            if inverse
            else _public(plan, observation, now)
            if observation
            else None
        )
        if health is None:
            raise ValueError("Forward publication requires public readiness.")
        result = prior.model_copy(
            update={
                "state": "reversed" if inverse else "applied",
                "updated_at": now,
                "public_health": health,
            }
        )
        replacement = plan.before if inverse else effective_after(result)
        replacement = replacement.model_copy(
            update={
                "target": replacement.target.model_copy(
                    update={
                        "policies": replacement.target.policies.model_copy(
                            update={
                                "staff_testing_hold": current.target.policies.staff_testing_hold
                            }
                        ),
                    }
                )
            }
        )
        if replacement.binding is not None and current.binding is not None:
            replacement = replacement.model_copy(
                update={
                    "binding": replacement.binding.model_copy(
                        update={
                            "ingress": current.binding.ingress,
                            "tls": current.binding.tls,
                        }
                    )
                }
            )
        if (
            session.scalar(
                select(db.LaunchplaneDokployTargetIdRow)
                .where(
                    db.LaunchplaneDokployTargetIdRow.target_id == replacement.target_id.target_id,
                    or_(
                        db.LaunchplaneDokployTargetIdRow.context != plan.candidate.context,
                        db.LaunchplaneDokployTargetIdRow.instance != "testing",
                    ),
                )
                .limit(1)
            )
            is not None
        ):
            raise ValueError("Publication target acquired a compatibility lane alias.")
        aliases = _retain_old(store, session, plan)
        if not inverse:
            aliases = ((plan.candidate.namespace, "testing-candidate"),)
            custody = session.get(db.LaunchplaneProviderTargetRow, aliases[0], with_for_update=True)
            if custody is None or (
                custody.provider_id,
                custody.target_category,
                custody.target_id,
            ) != ("dokploy", "compose", plan.candidate.compose_id):
                raise ValueError("Candidate physical target reservation changed.")
        _write_serving_snapshot(store, session, plan, current, replacement, aliases)
        if not inverse:
            _write_candidate_deployment(store, session, plan, health)
        store._after_product_authority_bundle_step("testing_publication")
        _lease(store, session, plan, reservation)
        _save(store, session, result)
        session.commit()
        return result


def app_artifact_id(plan: TestingPublicationPlan) -> str:
    assert plan.candidate.application is not None
    return plan.candidate.application.plan.artifact.artifact_id


def effective_after(record: TestingPublicationRecord) -> TestingServingSnapshot:
    snapshot = record.plan.after
    assert snapshot.inventory is not None
    return snapshot.model_copy(
        update={
            "inventory": snapshot.inventory.model_copy(
                update={"destination_health": record.public_health}
            )
        }
    )


def read_publication(store: db.PostgresRecordStore, key: str) -> TestingPublicationRecord | None:
    """Read historical provenance. The service caller authorizes this read."""
    evidence = store.read_idempotency_record(
        scope=PUBLICATION_SCOPE, route_path=PUBLICATION_ROUTE, idempotency_key=key
    )
    if evidence is None:
        return None
    record = TestingPublicationRecord.model_validate(evidence.response_payload)
    if record.plan.key != key or record.plan.digest != evidence.request_fingerprint:
        raise ValueError("Stored testing publication provenance is inconsistent.")
    return record


def _write_serving_snapshot(
    store: db.PostgresRecordStore,
    session: Any,
    plan: TestingPublicationPlan,
    current: TestingServingSnapshot,
    replacement: TestingServingSnapshot,
    aliases: tuple[tuple[str, str], ...],
) -> None:
    runtime_writes = (
        ()
        if replacement.runtime is None
        else (
            RuntimeEnvironmentWrite(
                record=replacement.runtime,
                expected_record=current.runtime,
                expected_absent=current.runtime is None,
            ),
        )
    )
    bundle = ProductAuthorityBundle(
        required_context_owner=(plan.candidate.product, plan.candidate.context),
        expected_product_profiles=(
            LaunchplaneProductProfileRecord.model_validate(
                session.get(db.LaunchplaneProductProfileRow, plan.profile.product).payload
            ),
        ),
        dokploy_targets=(replacement.target,),
        dokploy_target_ids=(replacement.target_id,),
        provider_target_writes=(
            ProviderTargetWrite(
                record=replacement.provider,
                expected_record=current.provider,
                allowed_conflicting_routes=aliases,
            ),
        ),
        runtime_environment_writes=runtime_writes,
        environment_inventory=(replacement.inventory,) if replacement.inventory else (),
    )
    store._write_product_authority_bundle(
        bundle, write_session=session, commit=False, write_transaction_started=True
    )
    for orm, identity, replacement_row in (
        (
            db.LaunchplaneRuntimeEnvironmentRow,
            ("instance", plan.candidate.context, "testing"),
            store._runtime_environment_row(replacement.runtime) if replacement.runtime else None,
        ),
        (
            db.LaunchplaneInventoryRow,
            (plan.candidate.context, "testing"),
            store._environment_inventory_row(replacement.inventory)
            if replacement.inventory
            else None,
        ),
        (
            db.LaunchplaneFreshOdooTestingGenerationRow,
            (plan.candidate.product, plan.candidate.context),
            db.LaunchplaneFreshOdooTestingGenerationRow(
                product=plan.candidate.product,
                context=plan.candidate.context,
                payload=store._payload_dict(replacement.generation),
            )
            if replacement.generation
            else None,
        ),
        (
            db.LaunchplaneRouteBindingRow,
            (plan.candidate.product, plan.candidate.context, "testing"),
            store._route_binding_row(replacement.binding) if replacement.binding else None,
        ),
        (
            db.LaunchplaneOdooInstanceOverrideRow,
            (plan.candidate.context, "testing"),
            db.LaunchplaneOdooInstanceOverrideRow(
                context=plan.candidate.context,
                instance="testing",
                updated_at=replacement.overrides.updated_at,
                payload=store._payload_dict(replacement.overrides),
            )
            if replacement.overrides
            else None,
        ),
    ):
        if replacement_row is None:
            row = session.get(orm, identity)
            if row is not None:
                session.delete(row)
        elif isinstance(replacement_row, db.LaunchplaneOdooInstanceOverrideRow):
            store._merge_odoo_override_row(session, replacement_row)
        else:
            session.merge(replacement_row)


def _write_candidate_deployment(
    store: db.PostgresRecordStore,
    session: Any,
    plan: TestingPublicationPlan,
    health: HealthcheckEvidence,
) -> None:
    deployment = plan.deployment.model_copy(update={"destination_health": health})
    prior_deploy = session.get(db.LaunchplaneDeploymentRow, deployment.record_id)
    if prior_deploy is not None and not _same(
        prior_deploy.payload, store._payload_dict(deployment)
    ):
        raise ValueError("Candidate deployment receipt already belongs to another outcome.")
    session.merge(
        db.LaunchplaneDeploymentRow(
            record_id=deployment.record_id,
            context=deployment.context,
            instance="testing",
            artifact_id=app_artifact_id(plan),
            source_git_ref=deployment.source_git_ref,
            deploy_started_at=deployment.deploy.started_at,
            deploy_finished_at=deployment.deploy.finished_at,
            payload=store._payload_dict(deployment),
        )
    )
    store._invalidate_testing_non_use_in_session(session, context=plan.candidate.context)
