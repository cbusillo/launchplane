"""Extra host names beside a native testing lane's canonical host, carried by its own route."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
from control_plane.contracts.public_hosts import normalize_public_hosts, retained_hostname_identity
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.contracts.testing_publication import TestingPublicationRecord
from control_plane.dokploy.testing_route import TestingLaneRoute, amend_testing_route_aliases
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore

ROUTE = "/v1/products/{product}/testing-aliases"
ALIASES_SCOPE = "launchplane:testing-aliases:v1"
ALIASES_PLAN_ROUTE = "service:testing-aliases:plan"
ALIASES_RECORD_ROUTE = "service:testing-aliases"


class TestingLaneAliasesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    hostnames: tuple[str, ...] = Field(
        description="Every extra host name the lane serves besides its canonical host; empty removes them."
    )
    mode: Literal["dry-run", "apply"] = "dry-run"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    reason: str = Field(min_length=1)


def lane_aliases_binding(
    current: EnvironmentRouteBindingRecord,
    route_before: TestingLaneRoute,
    route_after: TestingLaneRoute,
    publication_key: str,
) -> EnvironmentRouteBindingRecord:
    versions = {
        key: value
        for key, value in current.source.source_versions.items()
        if key not in {"testing_aliases", "testing_route_observation"}
    }
    versions["testing_route"] = route_after.digest
    if route_after != route_before:
        versions["testing_aliases"] = lane_aliases_key(publication_key, route_after)
    return current.model_copy(
        update={"source": current.source.model_copy(update={"source_versions": versions})}
    )


def lane_aliases_key(publication_key: str, route: TestingLaneRoute) -> str:
    return canonical_json_sha256({"publication": publication_key, "route": route.digest})


class TestingLaneAliasesPlan(FrozenRecord):
    product: str
    context: str
    reason: str = Field(min_length=1)
    publication_key: str
    publication_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    route_current: TestingLaneRoute
    route_before: TestingLaneRoute
    route_after: TestingLaneRoute
    binding_before: EnvironmentRouteBindingRecord
    binding_after: EnvironmentRouteBindingRecord
    created_at: datetime

    @property
    def digest(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))

    @property
    def key(self) -> str:
        return lane_aliases_key(self.publication_key, self.route_after)

    @model_validator(mode="after")
    def validate_amendment(self) -> Self:
        before, after = self.route_before, self.route_after
        if (before.product, before.context) != (self.product, self.context):
            raise ValueError("Extra host names belong to their own testing lane.")
        if after != before and after != amend_testing_route_aliases(before, after.aliases):
            raise ValueError("Extra host names cannot change the canonical host, copy or ingress.")
        if after == self.route_current:
            raise ValueError("The testing lane already serves exactly these host names.")
        if self.binding_after != lane_aliases_binding(
            self.binding_before, before, after, self.publication_key
        ):
            raise ValueError("Extra host names must preserve the lane's route binding.")
        return self


def _save(
    store: PostgresRecordStore, session: Any, route: str, key: str, plan: TestingLaneAliasesPlan
) -> None:
    stamp = plan.created_at.isoformat()
    session.merge(
        store._idempotency_row(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_mutation_reservation_id(
                    scope=ALIASES_SCOPE, route_path=route, idempotency_key=key
                ),
                scope=ALIASES_SCOPE,
                route_path=route,
                idempotency_key=key,
                request_fingerprint=plan.digest,
                created_at=stamp,
                updated_at=stamp,
                recorded_at=stamp,
                response_status_code=200,
                response_trace_id="testing-aliases:" + key,
                response_payload=plan.model_dump(mode="json"),
            )
        )
    )


def _read(store: PostgresRecordStore, route: str, key: str) -> TestingLaneAliasesPlan | None:
    evidence = store.read_idempotency_record(
        scope=ALIASES_SCOPE, route_path=route, idempotency_key=key
    )
    if evidence is None:
        return None
    plan = TestingLaneAliasesPlan.model_validate(evidence.response_payload)
    if evidence.request_fingerprint != plan.digest:
        raise ValueError("The testing host names record changed.")
    return plan


def save_lane_aliases_plan(store: PostgresRecordStore, plan: TestingLaneAliasesPlan) -> None:
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        _save(store, session, ALIASES_PLAN_ROUTE, plan.digest, plan)
        session.commit()


def read_lane_aliases_plan(store: PostgresRecordStore, digest: str) -> TestingLaneAliasesPlan:
    plan = _read(store, ALIASES_PLAN_ROUTE, digest)
    if plan is None or plan.digest != digest:
        raise ValueError("The reviewed testing host names plan is unavailable.")
    return plan


def read_lane_aliases(store: PostgresRecordStore, key: str) -> TestingLaneAliasesPlan | None:
    plan = _read(store, ALIASES_RECORD_ROUTE, key)
    if plan is not None and plan.key != key:
        raise ValueError("The testing host names record belongs to another route.")
    return plan


def _current_authority(
    store: PostgresRecordStore, product: str, context: str
) -> tuple[
    TestingPublicationRecord, EnvironmentRouteBindingRecord, TestingLaneRoute, TestingLaneRoute
]:
    from control_plane.testing_address_storage import effective_testing_route
    from control_plane.testing_publication_storage import read_snapshot
    from control_plane.testing_serving_copy import published_testing_copy

    publication = published_testing_copy(store, product=product, context=context)
    if publication is None:
        raise ValueError("Extra host names require the lane's published native testing copy.")
    snapshot = read_snapshot(store, product=product, context=context)
    binding = snapshot.binding
    if snapshot.target.policies.staff_testing_hold is not None or binding is None:
        raise ValueError("The testing copy is held or has no route binding.")
    current = effective_testing_route(store, publication, binding=binding)
    key = binding.source.source_versions.get("testing_aliases")
    applied = read_lane_aliases(store, key) if key else None
    if key and applied is None:
        raise ValueError("The applied testing host names record is unavailable.")
    base = applied.route_before if applied is not None else current
    return publication, binding, current, base


def require_names_available(
    store: PostgresRecordStore, *, product: str, context: str, names: set[str]
) -> None:
    """Refuse a name another lane, its extra names or retained custody already owns."""
    from control_plane.dokploy_testing_domain import require_testing_hostname_claims_available

    if not names:
        return
    target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
    if target.policies.staff_testing_hold is not None:
        raise ValueError("The testing copy is held.")
    bindings = store.list_route_binding_records()
    claimed = {retained_hostname_identity(host) for host in store.retained_disposable_hostnames()}
    for other in bindings:
        key = other.source.source_versions.get("testing_aliases")
        if key and (other.product, other.context) != (product, context):
            record = read_lane_aliases(store, key)
            claimed.update(record.route_after.aliases if record else ())
    for name in names:
        if name in claimed:
            raise ValueError("Another lane or retained custody already holds this hostname.")
        require_testing_hostname_claims_available(
            product=product,
            target=target,
            domain=name,
            targets=store.list_dokploy_target_records(),
            profiles=store.list_product_profile_records(),
            bindings=bindings,
            allow_current_binding=True,
        )


def plan_lane_aliases(
    store: PostgresRecordStore,
    *,
    product: str,
    context: str,
    hostnames: tuple[str, ...],
    reason: str,
) -> TestingLaneAliasesPlan:
    from control_plane.testing_address_storage import read_testing_address

    publication, binding, current, base = _current_authority(store, product, context)
    names = normalize_public_hosts(list(hostnames))
    if base.host in names:
        raise ValueError("The canonical host is not an extra host name.")
    address_key = binding.source.source_versions.get("testing_address")
    address = read_testing_address(store, address_key) if address_key else None
    if address is not None and address.plan.route_before.host not in names:
        raise ValueError("The original hostname kept by the address change must stay.")
    require_names_available(
        store, product=product, context=context, names=set(names) - set(current.aliases)
    )
    after = base if names == base.aliases else amend_testing_route_aliases(base, names)
    return TestingLaneAliasesPlan(
        product=product,
        context=context,
        reason=reason,
        publication_key=publication.plan.key,
        publication_sha256=canonical_json_sha256(publication.model_dump(mode="json")),
        route_current=current,
        route_before=base,
        route_after=after,
        binding_before=binding,
        binding_after=lane_aliases_binding(binding, base, after, publication.plan.key),
        created_at=datetime.now(UTC),
    )


def lane_aliases_result(plan: TestingLaneAliasesPlan, *, applied: bool) -> dict[str, object]:
    return {
        "plan_sha256": plan.digest,
        "product": plan.product,
        "context": plan.context,
        "instance": "testing",
        "hostname": plan.route_after.host,
        "hostnames_before": list(plan.route_current.aliases),
        "hostnames_after": list(plan.route_after.aliases),
        "applied": applied,
    }


def commit_lane_aliases(store: PostgresRecordStore, plan: TestingLaneAliasesPlan) -> None:
    """Move the binding to the published route; a repeat after success changes nothing."""
    from control_plane.testing_publication_storage import (
        PUBLICATION_ROUTE,
        PUBLICATION_SCOPE,
        testing_binding_authority,
    )

    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        store._lock_route_binding_write(session, binding_key=plan.binding_before.binding_key)
        current = store.read_route_binding_record(
            product=plan.product, context_name=plan.context, instance_name="testing"
        )
        if testing_binding_authority(current) == testing_binding_authority(plan.binding_after):
            return
        publication = session.scalar(
            store._idempotency_statement(
                scope=PUBLICATION_SCOPE,
                route_path=PUBLICATION_ROUTE,
                idempotency_key=plan.publication_key,
                for_update=True,
            )
        )
        if (
            testing_binding_authority(current) != testing_binding_authority(plan.binding_before)
            or publication is None
            or canonical_json_sha256(
                TestingPublicationRecord.model_validate(
                    publication.payload.get("response_payload")
                ).model_dump(mode="json")
            )
            != plan.publication_sha256
        ):
            raise ValueError("The testing route changed before its host names were recorded.")
        require_names_available(
            store,
            product=plan.product,
            context=plan.context,
            names=set(plan.route_after.aliases) - set(plan.route_current.aliases),
        )
        if plan.route_after != plan.route_before:
            _save(store, session, ALIASES_RECORD_ROUTE, plan.key, plan)
        stamp = datetime.now(UTC).isoformat()
        session.merge(
            store._route_binding_row(plan.binding_after.model_copy(update={"updated_at": stamp}))
        )
        session.commit()


class TestingLaneAliasesAdapter:
    def __init__(
        self, root: Path, store: PostgresRecordStore, plan: TestingLaneAliasesPlan, trace_id: str
    ) -> None:
        self.root, self.store, self.plan, self.trace_id = root, store, plan, trace_id

    def provider_target_origin(self) -> ProviderTargetOrigin:
        from control_plane.testing_serving_copy import published_testing_copy

        publication = published_testing_copy(
            self.store, product=self.plan.product, context=self.plan.context
        )
        if publication is None:
            raise ValueError("Extra host names require the lane's published native testing copy.")
        return ProviderTargetOrigin(
            product=self.plan.product,
            context=self.plan.context,
            instance="testing",
            provider_id="dokploy",
            target_type="compose",
            target_id=publication.plan.candidate.compose_id,
        )

    def target_key(self) -> str:
        return self.provider_target_origin().target_key()

    def reconciliation_key(self) -> str:
        return self.plan.digest

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.plan.digest:
            return ProviderObservation("unknown")
        # Publication and commit both settle from their own evidence on a repeat.
        return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        from control_plane.testing_copy_route import publish_testing_copy_route
        from control_plane.testing_publication_storage import read_snapshot

        publication, _binding, current, _base = _current_authority(
            self.store, self.plan.product, self.plan.context
        )
        if publication.plan.key != self.plan.publication_key or current not in (
            self.plan.route_current,
            self.plan.route_after,
        ):
            raise ValueError("The testing route changed since its host names were reviewed.")
        snapshot = read_snapshot(self.store, product=self.plan.product, context=self.plan.context)
        identity = snapshot.inventory.runtime_identity if snapshot.inventory else None
        if identity is None:
            raise ValueError("The testing runtime identity is unavailable.")
        assert isinstance(lease, ProviderEvidenceLease)
        # Hold the lane's own lock so no deploy or rebuild sees the file and record apart.
        with self.store.testing_verification_lane_guard(
            product=self.plan.product, context=self.plan.context
        ) as lane_checkpoint:
            publish_testing_copy_route(
                root=self.root,
                store=self.store,
                identity=identity,
                checkpoint=lease.checkpoint_effect,
                operation_id="testing-aliases:" + self.plan.digest,
                lease_owner=lease.evidence_reservation().lease_owner,
                route=self.plan.route_after,
                preimage=self.plan.route_current,
            )
            lane_checkpoint()
            lease.assert_current()
            commit_lane_aliases(self.store, self.plan)
        return ProviderMutationOutcome(
            202,
            provider_operation_response_payload(
                trace_id=self.trace_id,
                records={"testing_aliases": self.plan.digest},
                result=lane_aliases_result(self.plan, applied=True),
            ),
            provider_effect_performed=True,
        )
