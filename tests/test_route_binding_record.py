import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
    IngressRouteAuditStatus,
    IngressRouteTlsOwner,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingIngress,
    RouteBindingProviderTarget,
    RouteBindingSource,
    RouteBindingTls,
    build_route_binding_key,
    redacted_route_binding_record,
)
from control_plane.route_binding_backfill import (
    RouteBindingBackfillRequest,
    plan_route_binding_backfill,
)
from control_plane.http_app import (
    create_launchplane_fastapi_app,
    idempotency_request_fingerprint,
    idempotency_scope,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _get_route_binding_record,
    _get_route_binding_records,
)
from tests.support.auth import _StubVerifier, _identity


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _route_binding_record(
    *,
    product: str = "example-product",
    context: str = "example-testing",
    instance: str = "web",
    domain: str = "app.example.test",
) -> EnvironmentRouteBindingRecord:
    return EnvironmentRouteBindingRecord(
        product=product,
        context=context,
        instance=instance,
        provider_target=RouteBindingProviderTarget(
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            target_name="example-target",
            provider_evidence={"target_record": "example-testing:web"},
        ),
        ingress=RouteBindingIngress(
            provider="npmplus",
            endpoint_key="example-edge",
            termination_kind="edge",
            provider_evidence={"audit_record": "audit-1"},
        ),
        domains=(RouteBindingDomain(domain_name=domain, role="primary"),),
        tls=RouteBindingTls(
            owner="launchplane",
            provider_evidence={"audit_record": "audit-1"},
        ),
        source=RouteBindingSource(
            source_kind="operator",
            source_label="test",
            source_record_ids=("operator:test",),
            refreshed_at="2026-07-12T00:00:00Z",
            freshness_status="recorded",
        ),
        updated_at="2026-07-12T00:00:00Z",
    )


def _provider_target_record(
    *,
    context: str = "example-testing",
    instance: str = "web",
    provider_id: str = "dokploy",
    project_name: str = "example-server",
) -> ProviderTargetRecord:
    return ProviderTargetRecord(
        context=context,
        instance=instance,
        provider_id=provider_id,
        target_category="compose",
        target_id="compose-provider-id",
        display_name="example-target",
        provider_target_type="compose",
        provider_evidence={"project_name": project_name} if project_name else {},
        updated_at="2026-07-12T00:00:00Z",
        source_label="test",
    )


def _dokploy_target_record(
    *,
    context: str = "example-testing",
    instance: str = "web",
    domains: tuple[str, ...] = ("app.example.test",),
) -> DokployTargetRecord:
    return DokployTargetRecord(
        context=context,
        instance=instance,
        project_name="example-server",
        target_type="compose",
        target_name="example-target",
        domains=domains,
        updated_at="2026-07-12T00:00:00Z",
        source_label="test",
    )


def _dokploy_target_id_record(
    *,
    context: str = "example-testing",
    instance: str = "web",
    target_id: str = "compose-provider-id",
    updated_at: str = "2026-07-12T00:00:00Z",
) -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context=context,
        instance=instance,
        target_id=target_id,
        updated_at=updated_at,
        source_label="test",
    )


def _edge_endpoint_record(
    *,
    endpoint_key: str = "example-edge",
    server_name: str = "example-server",
) -> EdgeEndpointRecord:
    return EdgeEndpointRecord(
        endpoint_key=endpoint_key,
        provider="dokploy",
        server_name=server_name,
        upstream_host="192.0.2.10",
        upstream_host_kind="ip",
        upstream_scheme="https",
        upstream_port=443,
        status="active",
        updated_at="2026-07-12T00:00:00Z",
        source_label="test",
    )


def _ingress_audit_record(
    *,
    record_id: str = "audit-1",
    context: str = "example-testing",
    domains: tuple[str, ...] = ("app.example.test",),
    endpoint_key: str = "example-edge",
    provider: str = "npmplus",
    recorded_at: str = "2026-07-12T00:00:00Z",
    status: IngressRouteAuditStatus = "applied",
    tls_owner: IngressRouteTlsOwner = "provider",
    provider_certificate_ref: str = "101",
) -> IngressRouteAuditRecord:
    return IngressRouteAuditRecord(
        record_id=record_id,
        product="example-product",
        context=context,
        provider=provider,
        mode="apply",
        status=status,
        dry_run=False,
        requested_domains=domains,
        edge_endpoint_key=endpoint_key,
        tls_owner=tls_owner,
        provider_certificate_ref=provider_certificate_ref,
        expected_host_id=101,
        provider_host_id=101,
        operations=(
            IngressRouteAuditOperation(
                action="update",
                host_id=101,
                domain_names=domains,
                requires_apply=True,
                change_categories=("upstream",),
            ),
        ),
        trace_id="trace-route-binding",
        idempotency_key="route-binding-audit",
        reason="test",
        recorded_at=recorded_at,
    )


class _RouteBindingBackfillFakeStore:
    def __init__(
        self,
        *,
        provider_target: ProviderTargetRecord | None = None,
        dokploy_target: DokployTargetRecord | None = None,
        dokploy_target_id: DokployTargetIdRecord | None = None,
        edge_endpoints: tuple[EdgeEndpointRecord, ...] = (),
        ingress_audits: tuple[IngressRouteAuditRecord, ...] = (),
    ) -> None:
        self.provider_target = provider_target
        self.dokploy_target = dokploy_target
        self.dokploy_target_id = dokploy_target_id or (
            _dokploy_target_id_record() if dokploy_target is not None else None
        )
        self.edge_endpoints = edge_endpoints
        self.ingress_audits = ingress_audits

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord:
        if self.provider_target is None:
            raise FileNotFoundError("missing provider target")
        return self.provider_target

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        if self.dokploy_target is None:
            raise FileNotFoundError("missing Dokploy target")
        return self.dokploy_target

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        if self.dokploy_target_id is None:
            raise FileNotFoundError("missing Dokploy target id")
        return self.dokploy_target_id

    def list_edge_endpoint_records(
        self,
        *,
        provider: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EdgeEndpointRecord, ...]:
        records = tuple(
            endpoint
            for endpoint in self.edge_endpoints
            if (not provider or endpoint.provider == provider)
            and (not status or endpoint.status == status)
        )
        return records[:limit] if limit is not None else records

    def list_ingress_route_audit_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[IngressRouteAuditRecord, ...]:
        records = tuple(
            audit
            for audit in self.ingress_audits
            if (not product or audit.product == product)
            and (not context_name or audit.context == context_name)
        )
        return records[:limit] if limit is not None else records


class RouteBindingContractTests(unittest.TestCase):
    def test_route_binding_requires_provider_ids_as_evidence_not_identity(self) -> None:
        record = _route_binding_record()

        self.assertEqual(
            record.binding_key,
            '["example-product","example-testing","web"]',
        )
        self.assertEqual(record.provider_target.provider_id, "dokploy")
        self.assertEqual(
            record.provider_target.provider_evidence["target_record"], "example-testing:web"
        )

    def test_route_binding_accepts_provider_neutral_identifiers(self) -> None:
        provider_target = RouteBindingProviderTarget(
            provider_id="example-runtime",
            target_category="service",
            target_name="example-target",
        )
        ingress = RouteBindingIngress(
            provider="example-ingress",
            endpoint_key="example-edge",
            termination_kind="edge",
        )

        self.assertEqual(provider_target.provider_id, "example-runtime")
        self.assertEqual(ingress.provider, "example-ingress")

    def test_route_binding_key_is_unambiguous_when_identifiers_contain_delimiters(self) -> None:
        first_key = build_route_binding_key(product="a", context="b", instance="c|d")
        second_key = build_route_binding_key(product="a", context="b|c", instance="d")

        self.assertNotEqual(first_key, second_key)

    def test_route_binding_rejects_duplicate_domains_and_secret_shaped_evidence(self) -> None:
        payload = _route_binding_record().model_dump(mode="json")
        payload["domains"] = [
            {"domain_name": "App.Example.Test", "role": "primary"},
            {"domain_name": "app.example.test", "role": "alias"},
        ]
        with self.assertRaisesRegex(ValidationError, "domains must be unique"):
            EnvironmentRouteBindingRecord.model_validate(payload)

        with self.assertRaisesRegex(ValidationError, "secret-shaped"):
            RouteBindingIngress(
                provider="npmplus",
                endpoint_key="example-edge",
                termination_kind="edge",
                provider_evidence={"api_token": "redacted"},
            )

    def test_redacted_read_model_omits_provider_evidence(self) -> None:
        redacted = redacted_route_binding_record(_route_binding_record())
        payload = redacted.model_dump(mode="json")

        self.assertNotIn("provider_evidence", payload["provider_target"])
        self.assertNotIn("provider_evidence", payload["ingress"])
        self.assertNotIn("provider_evidence", payload["tls"])
        self.assertEqual(payload["domains"][0]["domain_name"], "app.example.test")


class RouteBindingStorageTests(unittest.TestCase):
    def test_filesystem_store_round_trips_route_binding_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record = _route_binding_record()

            store.write_route_binding_record(record)
            loaded = store.read_route_binding_record(
                product=record.product,
                context_name=record.context,
                instance_name=record.instance,
            )
            records = store.list_route_binding_records(
                product=record.product,
                context_name=record.context,
            )

        self.assertEqual(loaded.binding_key, record.binding_key)
        self.assertEqual(records, (record,))

    def test_sqlite_backed_postgres_store_round_trips_route_binding_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _route_binding_record(instance="api", domain="api.example.test")

            store.write_route_binding_record(record)
            loaded = store.read_route_binding_record(
                product=record.product,
                context_name=record.context,
                instance_name=record.instance,
            )
            records = store.list_route_binding_records(
                product=record.product,
                context_name=record.context,
                status="active",
            )
            store.close()

        self.assertEqual(
            loaded.binding_key,
            '["example-product","example-testing","api"]',
        )
        self.assertEqual(records, (record,))

    def test_sqlite_route_binding_mutation_commits_record_and_completion_atomically(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _route_binding_record()
            reservation = store.reserve_mutation(
                scope="github-actions:route-binding-test",
                route_path="/v1/route-bindings/backfill/apply",
                idempotency_key="route-binding-create",
                request_fingerprint="route-binding-fingerprint",
                lease_owner="trace-route-binding-create",
            ).record

            result = store.create_route_binding_record_with_mutation(
                record=record,
                reservation=reservation,
                response_status_code=202,
                response_trace_id="trace-route-binding-create",
                response_payload={"status": "accepted"},
            )
            loaded_record = store.read_route_binding_record(
                product=record.product,
                context_name=record.context,
                instance_name=record.instance,
            )
            stored_completion = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            store.close()

        self.assertEqual(result.status, "created")
        self.assertEqual(loaded_record, record)
        self.assertIsNotNone(stored_completion)
        assert stored_completion is not None
        self.assertEqual(stored_completion.state, "completed")
        self.assertEqual(stored_completion.response_trace_id, "trace-route-binding-create")

    def test_sqlite_route_binding_existing_record_releases_reservation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _route_binding_record()
            store.write_route_binding_record(record)
            reservation = store.reserve_mutation(
                scope="github-actions:route-binding-test",
                route_path="/v1/route-bindings/backfill/apply",
                idempotency_key="route-binding-existing",
                request_fingerprint="route-binding-existing-fingerprint",
                lease_owner="trace-route-binding-existing",
            ).record

            result = store.create_route_binding_record_with_mutation(
                record=record,
                reservation=reservation,
                response_status_code=202,
                response_trace_id="trace-route-binding-existing",
                response_payload={"status": "accepted"},
            )
            stored_reservation = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            store.close()

        self.assertEqual(result.status, "exists")
        self.assertEqual(result.route_binding, record)
        self.assertIsNone(stored_reservation)

    def test_sqlite_route_binding_completion_failure_rolls_back_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _route_binding_record()
            reservation = store.reserve_mutation(
                scope="github-actions:route-binding-test",
                route_path="/v1/route-bindings/backfill/apply",
                idempotency_key="route-binding-fault",
                request_fingerprint="route-binding-fault-fingerprint",
                lease_owner="trace-route-binding-fault",
            ).record

            with patch.object(
                store,
                "_sync_idempotency_row",
                side_effect=RuntimeError("injected route binding completion failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected route binding completion failure",
                ):
                    store.create_route_binding_record_with_mutation(
                        record=record,
                        reservation=reservation,
                        response_status_code=202,
                        response_trace_id="trace-route-binding-fault",
                        response_payload={"status": "accepted"},
                    )
            with self.assertRaises(FileNotFoundError):
                store.read_route_binding_record(
                    product=record.product,
                    context_name=record.context,
                    instance_name=record.instance,
                )
            stored_reservation = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            store.close()

        self.assertIsNotNone(stored_reservation)
        assert stored_reservation is not None
        self.assertEqual(stored_reservation.state, "running")


class RouteBindingBackfillTests(unittest.TestCase):
    def test_backfill_plans_route_binding_from_unambiguous_launchplane_records(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "ready")
        self.assertIsNotNone(plan.record)
        assert plan.record is not None
        self.assertEqual(plan.record.ingress.endpoint_key, "example-edge")
        self.assertEqual(plan.record.tls.owner, "provider")
        self.assertEqual(plan.record.source.freshness_status, "recorded")
        self.assertEqual(plan.record.source.stale_after, "2026-07-13T00:00:00Z")
        self.assertEqual(
            set(plan.record.source.source_versions),
            set(plan.record.source.source_record_ids),
        )

    def test_backfill_fails_closed_when_edge_endpoint_evidence_conflicts(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(server_name="different-server"),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "edge_endpoint_conflict")

    def test_backfill_fails_closed_when_provider_target_projection_conflicts(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                dokploy_target_id=_dokploy_target_id_record(target_id="different-target-id"),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "provider_target_projection_conflict")

    def test_backfill_fails_closed_when_edge_endpoint_evidence_is_ambiguous(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(
                    _edge_endpoint_record(endpoint_key="example-edge-1"),
                    _edge_endpoint_record(endpoint_key="example-edge-2"),
                ),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "edge_endpoint_ambiguous")

    def test_backfill_fails_closed_when_edge_endpoint_scan_is_exhausted(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=tuple(
                    _edge_endpoint_record(endpoint_key=f"example-edge-{index}")
                    for index in range(1001)
                ),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "edge_endpoint_scan_limit_exceeded")

    def test_backfill_fails_closed_when_ingress_endpoint_conflicts(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(_ingress_audit_record(endpoint_key="different-edge"),),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "ingress_edge_endpoint_conflict")

    def test_backfill_fails_closed_when_ingress_audit_is_ambiguous(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(
                    _ingress_audit_record(record_id="audit-1", endpoint_key="example-edge"),
                    _ingress_audit_record(record_id="audit-2", endpoint_key="different-edge"),
                ),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "ingress_audit_ambiguous")

    def test_backfill_fails_closed_when_latest_ingress_audit_is_unresolved(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(
                    _ingress_audit_record(record_id="audit-applied"),
                    _ingress_audit_record(
                        record_id="audit-pending",
                        status="pending",
                        recorded_at="2026-07-12T00:10:00Z",
                    ),
                ),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "ingress_audit_unresolved")

    def test_backfill_fails_closed_when_tls_ownership_is_unknown(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(
                    _ingress_audit_record(
                        tls_owner="unknown",
                        provider_certificate_ref="",
                    ),
                ),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "tls_ownership_unknown")

    def test_backfill_fails_closed_when_source_evidence_is_stale(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-13T00:00:01Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "evidence_stale")

    def test_backfill_fails_closed_when_ingress_audit_scan_is_exhausted(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=tuple(
                    _ingress_audit_record(record_id=f"audit-{index}") for index in range(1001)
                ),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "ingress_audit_scan_limit_exceeded")

    def test_backfill_fails_closed_when_tracked_domains_are_invalid(self) -> None:
        plan = plan_route_binding_backfill(
            record_store=_RouteBindingBackfillFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(domains=("https://app.example.test",)),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingBackfillRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "domains_invalid")


def _route_binding_policy(*, action: str = "route_binding.read") -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["example-product"],
                    "contexts": ["example-testing"],
                    "actions": [action],
                }
            ]
        }
    )


def _route_binding_backfill_payload(*, mode: str = "apply") -> dict[str, object]:
    payload: dict[str, object] = {
        "mode": mode,
        "product": "example-product",
        "context": "example-testing",
        "instance": "web",
    }
    if mode == "apply":
        payload.update(
            {
                "reason": "Backfill reviewed environment route binding.",
                "confirmation": "APPLY LAUNCHPLANE ROUTE BINDING",
            }
        )
    return payload


class RouteBindingHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_binding_reads_redact_provider_evidence(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_route_binding_record(_route_binding_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_route_binding_policy(),
                record_store_factory=lambda: store,
            )

            read_response = await _get_route_binding_record(app)
            list_response = await _get_route_binding_records(app)

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(list_response.status_code, 200)
        read_payload = read_response.json()
        list_payload = list_response.json()
        self.assertEqual(read_payload["record"]["product"], "example-product")
        self.assertNotIn("provider_evidence", read_payload["record"]["provider_target"])
        self.assertNotIn("provider_evidence", read_payload["record"]["ingress"])
        self.assertEqual(list_payload["count"], 1)
        self.assertNotIn("provider_evidence", list_payload["records"][0]["tls"])

    async def test_route_binding_reads_require_scoped_authz(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(action="edge_endpoint.read"),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _get_route_binding_record(app)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_route_binding_backfill_apply_requires_scoped_authz(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/route-bindings/backfill/apply",
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "route-binding-backfill-denied",
            },
            payload=_route_binding_backfill_payload(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_route_binding_backfill_apply_requires_idempotency_key(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(action="route_binding.apply"),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/route-bindings/backfill/apply",
            headers={"Authorization": "Bearer valid-token"},
            payload=_route_binding_backfill_payload(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_route_binding_backfill_apply_requires_database_mutation_store(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_route_binding_policy(action="route_binding.apply"),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/route-bindings/backfill/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "route-binding-backfill-filesystem",
                },
                payload=_route_binding_backfill_payload(),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_route_binding_backfill_apply_reports_running_reservation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            identity = _identity()
            payload = _route_binding_backfill_payload()
            route_path = "/v1/route-bindings/backfill/apply"
            idempotency_key = "route-binding-backfill-running"
            store.reserve_mutation(
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=idempotency_key,
                request_fingerprint=idempotency_request_fingerprint(
                    route_path=route_path,
                    payload=payload,
                ),
                lease_owner="running-worker",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=_route_binding_policy(action="route_binding.apply"),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                route_path,
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": idempotency_key,
                },
                payload=payload,
            )
            store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "mutation_in_progress")

    async def test_route_binding_backfill_apply_writes_record_without_provider_evidence_response(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_provider_target_record(_provider_target_record())
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())
            store.write_edge_endpoint_record(_edge_endpoint_record())
            store.write_ingress_route_audit_record(_ingress_audit_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_route_binding_policy(action="route_binding.apply"),
                record_store_factory=lambda: store,
            )

            request_headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "route-binding-backfill-1",
            }
            request_payload = _route_binding_backfill_payload()
            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-12T00:15:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/backfill/apply",
                    headers=request_headers,
                    payload=request_payload,
                )
                replayed_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/backfill/apply",
                    headers=request_headers,
                    payload=request_payload,
                )
            loaded = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="web",
            )
            stored_reservation = store.read_idempotency_record(
                scope=idempotency_scope(_identity()),
                route_path="/v1/route-bindings/backfill/apply",
                idempotency_key="route-binding-backfill-1",
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(replayed_response.status_code, 202)
        replayed_payload = replayed_response.json()
        self.assertTrue(replayed_payload["replayed"])
        self.assertEqual(replayed_payload["original_trace_id"], payload["trace_id"])
        self.assertEqual(replayed_payload["result"], payload["result"])
        self.assertEqual(payload["records"]["route_binding_status"], "applied")
        self.assertNotIn("provider_evidence", payload["result"]["record"]["provider_target"])
        self.assertEqual(loaded.ingress.endpoint_key, "example-edge")
        self.assertIsNotNone(stored_reservation)
        assert stored_reservation is not None
        self.assertEqual(stored_reservation.state, "completed")

    async def test_route_binding_backfill_dry_run_reports_blockers_without_writing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_provider_target_record(_provider_target_record())
            store.write_dokploy_target_record(_dokploy_target_record())
            store.write_dokploy_target_id_record(_dokploy_target_id_record())
            store.write_edge_endpoint_record(_edge_endpoint_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_route_binding_policy(action="route_binding.apply"),
                record_store_factory=lambda: store,
            )

            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-12T00:15:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/backfill/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload=_route_binding_backfill_payload(mode="dry-run"),
                )
            with self.assertRaises(FileNotFoundError):
                store.read_route_binding_record(
                    product="example-product",
                    context_name="example-testing",
                    instance_name="web",
                )
            store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"]["route_binding_status"], "blocked")
        self.assertEqual(payload["result"]["findings"][0]["code"], "ingress_audit_missing")

    async def test_route_binding_backfill_refuses_to_overwrite_existing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            existing_record = _route_binding_record(domain="existing.example.test")
            store.write_route_binding_record(existing_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_route_binding_policy(action="route_binding.apply"),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/route-bindings/backfill/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "route-binding-backfill-existing",
                },
                payload=_route_binding_backfill_payload(),
            )
            loaded = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="web",
            )
            stored_reservation = store.read_idempotency_record(
                scope=idempotency_scope(_identity()),
                route_path="/v1/route-bindings/backfill/apply",
                idempotency_key="route-binding-backfill-existing",
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"]["route_binding_status"], "blocked")
        self.assertEqual(payload["result"]["findings"][0]["code"], "route_binding_exists")
        self.assertEqual(loaded, existing_record)
        self.assertIsNone(stored_reservation)

    async def test_openapi_includes_route_binding_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        self.assertEqual(
            openapi["paths"]["/v1/route-bindings/records"]["get"]["operationId"],
            "list_route_binding_records",
        )
        self.assertEqual(
            openapi["paths"]["/v1/route-bindings/records/current"]["get"]["operationId"],
            "read_route_binding_record",
        )
        self.assertEqual(
            openapi["paths"]["/v1/route-bindings/backfill/apply"]["post"]["operationId"],
            "apply_route_binding_backfill",
        )
        self.assertEqual(
            openapi["paths"]["/v1/route-bindings/backfill/apply"]["post"]["responses"]["202"][
                "content"
            ]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertEqual(
            openapi["components"]["schemas"]["RouteBindingRecordsResponse"]["additionalProperties"],
            False,
        )
        self.assertIn("RouteBindingRecordResponse", openapi["components"]["schemas"])
        self.assertIn("AcceptedEvidenceResponse", openapi["components"]["schemas"])


if __name__ == "__main__":
    unittest.main()
