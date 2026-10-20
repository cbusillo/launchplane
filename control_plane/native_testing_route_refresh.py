"""Renew the lane-owned route from custody and a new managed-proxy observation."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import click
from sqlalchemy import select

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    route_binding_record_sha256,
)
from control_plane.contracts.testing_route_refresh import TestingRouteRefreshSources
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import (
    NativeSourceAuthorizationDenied,
    NativeSourceUnavailable,
    require_registered_remote_server,
)
from control_plane.dokploy.testing_route import observe_testing_route
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.route_binding_reconcile import (
    RouteBindingReconcileFinding,
    RouteBindingReconcilePlan,
    RouteBindingSourceChanged,
)
from control_plane.storage import postgres as db
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProductContextOwnershipError,
    require_bundle_context_owner,
)
from control_plane.testing_publication_storage import (
    PUBLICATION_SCOPE,
    PUBLICATION_ROUTE,
    _snapshot,
    read_snapshot,
)
from control_plane.testing_serving_copy import published_testing_copy


def is_native_testing_binding(record: EnvironmentRouteBindingRecord) -> bool:
    return record.instance == "testing" and (
        record.source.source_label == "service:testing-rebuild"
        or bool({"testing_publication", "testing_route"} & record.source.source_versions.keys())
    )


def plan_native_testing_route_refresh(
    store: db.PostgresRecordStore,
    *,
    root: Path,
    current: EnvironmentRouteBindingRecord,
    evaluated_at: str,
) -> RouteBindingReconcilePlan:
    digest = route_binding_record_sha256(current)
    try:
        publication = published_testing_copy(
            store, product=current.product, context=current.context
        )
        if (
            publication is None
            or current.source.source_kind != "service"
            or current.status != "active"
        ):
            raise ValueError("Native testing route has no applied serving publication.")
        snapshot = read_snapshot(store, product=current.product, context=current.context)
        if snapshot.binding != current:
            raise ValueError("Testing binding changed during observation.")
        profile = store.read_product_profile_record(current.product)
        candidate = publication.plan.candidate
        route = publication.plan.route
        if snapshot.inventory is None or snapshot.inventory.runtime_identity is None:
            raise ValueError("The current testing build identity is unavailable.")
        host, token = source.read_dokploy_config(
            control_plane_root=root, database_url=store.database_url
        )
        try:
            actual = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=candidate.compose_id
            )
        except api.DokployRequestFailed as error:
            if error.status_code in {401, 403}:
                raise NativeSourceAuthorizationDenied(
                    "The published testing target read was denied."
                ) from None
            raise
        if (actual.get("composeId"), actual.get("name"), actual.get("serverId")) != (
            candidate.compose_id,
            candidate.namespace,
            candidate.server_id,
        ) or not actual.get("appName"):
            raise ValueError("The published testing provider target changed.")
        transport = DokployTestingRouteTransport(
            host=host,
            token=token,
            server_id=require_registered_remote_server(candidate.server_id),
            old_project=str(actual["appName"]),
            product=current.product,
            context=current.context,
            public_host=route.host,
            entrypoints=route.public_entrypoints,
            old_runtime_identity=snapshot.inventory.runtime_identity,
        )
        started = datetime.now(UTC)
        frame = transport.read()
        observed_at = datetime.now(UTC)
        evaluation = datetime.fromisoformat(evaluated_at.replace("Z", "+00:00"))
        refreshed = datetime.fromisoformat(current.source.refreshed_at.replace("Z", "+00:00"))
        if evaluation.utcoffset() is None or refreshed.utcoffset() is None:
            raise ValueError("Native route evidence requires aware timestamps.")
        if not timedelta(0) <= observed_at - started <= timedelta(minutes=2) or not (
            timedelta(minutes=-2) <= observed_at - evaluation <= timedelta(minutes=2)
        ):
            raise ValueError("The current testing route observation is not fresh.")
        if (
            frame["owned_file"] is None
            or observe_testing_route(route, frame["raw"], json.loads(frame["owned_file"]))
            != route.new_backend
        ):
            raise ValueError("Testing route no longer serves its published copy.")
        sources = TestingRouteRefreshSources(
            product=current.product,
            context=current.context,
            namespace=candidate.namespace,
            target_id=candidate.compose_id,
            publication_key=publication.plan.key,
            publication_sha256=canonical_json_sha256(publication.model_dump(mode="json")),
            candidate_sha256=canonical_json_sha256(store._payload_dict(candidate)),
            snapshot_sha256=canonical_json_sha256(store._payload_dict(snapshot)),
            profile_sha256=canonical_json_sha256(store._payload_dict(profile)),
            observed_at=observed_at,
        )
        if (
            published_testing_copy(store, product=current.product, context=current.context)
            != publication
        ):
            raise ValueError("The testing publication changed during route observation.")
        # Detect source changes during provider IO as well as at the final write.
        with store._session_factory() as session:
            store._begin_serialized_write(session)
            store._lock_product_authority_bundle_write(session)
            store._lock_route_binding_write(session, binding_key=current.binding_key)
            check_native_testing_route_sources(store, session, sources)
        due = not current.source.stale_after or evaluation - refreshed >= timedelta(hours=12)
        if not due:
            return RouteBindingReconcilePlan(
                status="unchanged",
                current_record_sha256=digest,
                candidate_record_sha256=digest,
                current_record=current,
                record=current,
                native_review_sources=sources,
            )
        stamp = observed_at.isoformat()
        replacement = current.model_copy(
            update={
                "source": current.source.model_copy(
                    update={
                        "source_versions": {
                            **current.source.source_versions,
                            "testing_route_observation": canonical_json_sha256(
                                {
                                    "route": route.digest,
                                    "raw": frame["raw"],
                                    "owned_file": frame["owned_file"],
                                    "observed_at": stamp,
                                }
                            ),
                        },
                        "refreshed_at": stamp,
                        "freshness_status": "verified",
                        "stale_after": (observed_at + timedelta(hours=24)).isoformat(),
                    }
                ),
                "updated_at": stamp,
            }
        )
        return RouteBindingReconcilePlan(
            status="ready",
            operation="refresh",
            current_record_sha256=digest,
            candidate_record_sha256=route_binding_record_sha256(replacement),
            current_record=current,
            record=replacement,
            native_review_sources=sources,
        )
    except NativeSourceAuthorizationDenied:
        raise
    except (
        ValueError,
        FileNotFoundError,
        ProductContextOwnershipError,
        NativeSourceUnavailable,
        click.ClickException,
    ):
        # Provider details and runtime settings stay out of the public result.
        return RouteBindingReconcilePlan(
            status="blocked",
            current_record_sha256=digest,
            current_record=current,
            findings=(
                RouteBindingReconcileFinding(
                    code="native_testing_route_unverified",
                    detail="The published testing copy needs intact custody and a fresh matching managed-route observation.",
                ),
            ),
        )


def check_native_testing_route_sources(
    store: db.PostgresRecordStore, session: Any, sources: TestingRouteRefreshSources
) -> None:
    """Caller holds product/binding locks through a write; provider evidence stays bounded."""
    product, context = sources.product, sources.context
    now = datetime.now(UTC)
    if not timedelta(0) <= now - sources.observed_at <= timedelta(minutes=2):
        raise RouteBindingSourceChanged("Native testing route observation expired before commit.")
    require_bundle_context_owner(
        ProductAuthorityBundle(required_context_owner=(product, context)),
        (
            store._read_product_profile_payload(row.payload)
            for row in session.scalars(select(db.LaunchplaneProductProfileRow))
        ),
    )
    checks = (
        (db.LaunchplaneProductProfileRow, product, sources.profile_sha256),
        (db.LaunchplaneTestingCandidateRow, sources.namespace, sources.candidate_sha256),
    )
    for model, identity, expected in checks:
        row = session.get(model, identity, with_for_update=True)
        if row is None or canonical_json_sha256(row.payload) != expected:
            raise RouteBindingSourceChanged("Native testing route custody changed before commit.")
    if (
        canonical_json_sha256(store._payload_dict(_snapshot(store, session, product, context)))
        != sources.snapshot_sha256
    ):
        raise RouteBindingSourceChanged("Native testing serving metadata changed before commit.")
    evidence = session.scalar(
        store._idempotency_statement(
            scope=PUBLICATION_SCOPE,
            route_path=PUBLICATION_ROUTE,
            idempotency_key=sources.publication_key,
            for_update=True,
        )
    )
    if (
        evidence is None
        or canonical_json_sha256(evidence.payload["response_payload"]) != sources.publication_sha256
    ):
        raise RouteBindingSourceChanged("Native testing publication changed before commit.")
    aliases = session.scalars(
        select(db.LaunchplaneProviderTargetRow).where(
            db.LaunchplaneProviderTargetRow.provider_id == "dokploy",
            db.LaunchplaneProviderTargetRow.target_category == "compose",
            db.LaunchplaneProviderTargetRow.target_id == sources.target_id,
        )
    )
    if {(row.context, row.instance) for row in aliases} != {
        (context, "testing"),
        (sources.namespace, "testing-candidate"),
    }:
        raise RouteBindingSourceChanged("Native testing target acquired foreign physical custody.")
    if any(
        (row.context, row.instance) != (context, "testing")
        for row in session.scalars(
            select(db.LaunchplaneDokployTargetIdRow).where(
                db.LaunchplaneDokployTargetIdRow.target_id == sources.target_id
            )
        )
    ):
        raise RouteBindingSourceChanged("Native testing target acquired a foreign lane alias.")
