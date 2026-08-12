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
    route_binding_record_sha256,
)
from control_plane.route_binding_reconcile import (
    RouteBindingExpectedCurrent,
    RouteBindingReconcileRequest,
    plan_route_binding_reconcile,
)
from control_plane.http_app import (
    create_launchplane_fastapi_app,
    idempotency_request_fingerprint,
    idempotency_scope,
)
from control_plane.dokploy.api import JsonObject
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from control_plane.workflows.dokploy_target_adoption import (
    repair_dokploy_target_domain_authority,
)
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
)
from tests.support.auth import _StubVerifier, _identity
from tests.support.ingress import _get_route_binding_record, _get_route_binding_records


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _live_domains(*hosts: str) -> tuple[JsonObject, ...]:
    return tuple({"host": host} for host in hosts)


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


def _route_binding_mutation(
    *,
    idempotency_key: str,
    trace_id: str,
) -> DbOnlyMutationRequest:
    return DbOnlyMutationRequest(
        scope="github-actions:route-binding-test",
        route_path="/v1/route-bindings/reconcile",
        idempotency_key=idempotency_key,
        request_fingerprint=f"fingerprint:{idempotency_key}",
        lease_owner=trace_id,
        response_status_code=202,
        response_trace_id=trace_id,
        response_payload={"status": "accepted", "trace_id": trace_id},
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
    project_name: str = "example-server",
) -> DokployTargetRecord:
    return DokployTargetRecord(
        context=context,
        instance=instance,
        project_name=project_name,
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


class _RouteBindingReconcileFakeStore:
    def __init__(
        self,
        *,
        provider_target: ProviderTargetRecord | None = None,
        dokploy_target: DokployTargetRecord | None = None,
        dokploy_target_id: DokployTargetIdRecord | None = None,
        edge_endpoints: tuple[EdgeEndpointRecord, ...] = (),
        ingress_audits: tuple[IngressRouteAuditRecord, ...] = (),
        route_binding: EnvironmentRouteBindingRecord | None = None,
    ) -> None:
        self.provider_target = provider_target
        self.dokploy_target = dokploy_target
        self.dokploy_target_id = dokploy_target_id or (
            _dokploy_target_id_record() if dokploy_target is not None else None
        )
        self.edge_endpoints = edge_endpoints
        self.ingress_audits = ingress_audits
        self.route_binding = route_binding

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

    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord:
        if self.route_binding is None:
            raise FileNotFoundError("missing route binding")
        return self.route_binding


def _route_binding_expected_current(
    record: EnvironmentRouteBindingRecord | None,
) -> RouteBindingExpectedCurrent:
    if record is None:
        return RouteBindingExpectedCurrent(state="absent")
    return RouteBindingExpectedCurrent(
        state="present",
        record_sha256=route_binding_record_sha256(record),
    )


def _route_binding_reconcile_request(
    *,
    evaluated_at: str = "2026-07-20T00:00:00Z",
    current_record: EnvironmentRouteBindingRecord | None = None,
) -> RouteBindingReconcileRequest:
    return RouteBindingReconcileRequest(
        product="example-product",
        context="example-testing",
        instance="web",
        expected_current=_route_binding_expected_current(current_record),
        evaluated_at=evaluated_at,
    )


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

    def test_sqlite_route_binding_reconcile_creates_record_and_completion_atomically(
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
            mutation = _route_binding_mutation(
                idempotency_key="route-binding-create",
                trace_id="trace-route-binding-create",
            )

            result = store.reconcile_route_binding_record(
                expected_record=None,
                replacement_record=record,
                mutation=mutation,
            )
            loaded_record = store.read_route_binding_record(
                product=record.product,
                context_name=record.context,
                instance_name=record.instance,
            )
            stored_completion = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            store.close()

        self.assertEqual(result.status, "created")
        self.assertEqual(loaded_record, record)
        self.assertIsNotNone(stored_completion)
        assert stored_completion is not None
        self.assertEqual(stored_completion.state, "completed")
        self.assertEqual(stored_completion.response_trace_id, "trace-route-binding-create")

    def test_sqlite_route_binding_reconcile_completes_unchanged_noop(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _route_binding_record()
            store.write_route_binding_record(record)
            mutation = _route_binding_mutation(
                idempotency_key="route-binding-unchanged",
                trace_id="trace-route-binding-unchanged",
            )

            result = store.reconcile_route_binding_record(
                expected_record=record,
                replacement_record=record,
                mutation=mutation,
            )
            stored_completion = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            store.close()

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(result.current_record, record)
        self.assertIsNotNone(stored_completion)
        assert stored_completion is not None
        self.assertEqual(stored_completion.state, "completed")

    def test_sqlite_route_binding_reconcile_refreshes_matching_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            current = _route_binding_record()
            replacement = current.model_copy(
                update={
                    "source": current.source.model_copy(
                        update={
                            "refreshed_at": "2026-07-20T00:00:00Z",
                            "stale_after": "2026-07-21T00:00:00Z",
                        }
                    ),
                    "updated_at": "2026-07-20T00:00:00Z",
                }
            )
            store.write_route_binding_record(current)
            mutation = _route_binding_mutation(
                idempotency_key="route-binding-refresh",
                trace_id="trace-route-binding-refresh",
            )

            result = store.reconcile_route_binding_record(
                expected_record=current,
                replacement_record=replacement,
                mutation=mutation,
            )
            loaded = store.read_route_binding_record(
                product=current.product,
                context_name=current.context,
                instance_name=current.instance,
            )
            store.close()

        self.assertEqual(result.status, "refreshed")
        self.assertEqual(loaded, replacement)

    def test_sqlite_route_binding_reconcile_rejects_stale_expected_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            expected = _route_binding_record()
            current = _route_binding_record(domain="changed.example.test")
            store.write_route_binding_record(current)
            mutation = _route_binding_mutation(
                idempotency_key="route-binding-stale",
                trace_id="trace-route-binding-stale",
            )

            result = store.reconcile_route_binding_record(
                expected_record=expected,
                replacement_record=expected,
                mutation=mutation,
            )
            stored_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            store.close()

        self.assertEqual(result.status, "changed")
        self.assertEqual(result.current_record, current)
        self.assertIsNone(stored_reservation)

    def test_sqlite_route_binding_reconcile_rejects_missing_expected_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            expected = _route_binding_record()
            mutation = _route_binding_mutation(
                idempotency_key="route-binding-missing",
                trace_id="trace-route-binding-missing",
            )

            result = store.reconcile_route_binding_record(
                expected_record=expected,
                replacement_record=expected,
                mutation=mutation,
            )
            stored_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            store.close()

        self.assertEqual(result.status, "missing")
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
            mutation = _route_binding_mutation(
                idempotency_key="route-binding-fault",
                trace_id="trace-route-binding-fault",
            )

            with patch.object(
                store,
                "_sync_idempotency_row",
                side_effect=RuntimeError("injected route binding completion failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected route binding completion failure",
                ):
                    store.reconcile_route_binding_record(
                        expected_record=None,
                        replacement_record=record,
                        mutation=mutation,
                    )
            with self.assertRaises(FileNotFoundError):
                store.read_route_binding_record(
                    product=record.product,
                    context_name=record.context,
                    instance_name=record.instance,
                )
            stored_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            store.close()

        self.assertIsNone(stored_reservation)


class RouteBindingEvidenceTests(unittest.TestCase):
    def test_reconcile_derives_binding_from_unambiguous_launchplane_records(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(project_name=""),
                dokploy_target=_dokploy_target_record(project_name=""),
                edge_endpoints=(_edge_endpoint_record(server_name="shared-edge-server"),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingReconcileRequest(
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
        self.assertEqual(plan.record.source.stale_after, "2026-07-13T00:15:00Z")
        self.assertEqual(
            set(plan.record.source.source_versions),
            set(plan.record.source.source_record_ids),
        )

    def test_reconcile_fails_closed_when_ingress_edge_endpoint_is_missing(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(endpoint_key="different-edge"),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "ingress_edge_endpoint_missing")

    def test_reconcile_fails_closed_when_provider_target_projection_conflicts(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                dokploy_target_id=_dokploy_target_id_record(target_id="different-target-id"),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "provider_target_projection_conflict")

    def test_reconcile_fails_closed_when_edge_endpoint_evidence_is_ambiguous(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(
                    _edge_endpoint_record(server_name="edge-server-1"),
                    _edge_endpoint_record(server_name="edge-server-2"),
                ),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "ingress_edge_endpoint_ambiguous")

    def test_reconcile_fails_closed_when_edge_endpoint_scan_is_exhausted(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=tuple(
                    _edge_endpoint_record(endpoint_key=f"example-edge-{index}")
                    for index in range(1001)
                ),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "edge_endpoint_scan_limit_exceeded")

    def test_reconcile_reports_missing_ingress_audit_before_edge_resolution(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(project_name=""),
                dokploy_target=_dokploy_target_record(project_name=""),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual([finding.code for finding in plan.findings], ["ingress_audit_missing"])

    def test_reconcile_fails_closed_when_ingress_audit_is_ambiguous(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(
                    _ingress_audit_record(record_id="audit-1", endpoint_key="example-edge"),
                    _ingress_audit_record(record_id="audit-2", endpoint_key="different-edge"),
                ),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "ingress_audit_ambiguous")

    def test_reconcile_fails_closed_when_latest_ingress_audit_is_unresolved(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
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
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "ingress_audit_unresolved")

    def test_reconcile_fails_closed_when_latest_ingress_audit_has_no_edge_endpoint(
        self,
    ) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(
                    _ingress_audit_record(record_id="audit-linked"),
                    _ingress_audit_record(
                        record_id="audit-unlinked",
                        endpoint_key="",
                        recorded_at="2026-07-12T00:10:00Z",
                    ),
                ),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            plan.findings[0].code,
            "ingress_audit_edge_endpoint_missing",
        )

    def test_reconcile_fails_closed_when_tls_ownership_is_unknown(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
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
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "tls_ownership_unknown")

    def test_reconcile_accepts_old_authoritative_source_versions(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-13T00:00:01Z",
            ),
        )

        self.assertEqual(plan.status, "ready")
        assert plan.record is not None
        self.assertEqual(plan.record.source.stale_after, "2026-07-14T00:00:01Z")

    def test_reconcile_fails_closed_when_ingress_audit_scan_is_exhausted(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=tuple(
                    _ingress_audit_record(record_id=f"audit-{index}") for index in range(1001)
                ),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "ingress_audit_scan_limit_exceeded")

    def test_reconcile_fails_closed_when_tracked_domains_are_invalid(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_RouteBindingReconcileFakeStore(
                provider_target=_provider_target_record(),
                dokploy_target=_dokploy_target_record(domains=("https://app.example.test",)),
                edge_endpoints=(_edge_endpoint_record(),),
                ingress_audits=(_ingress_audit_record(),),
            ),
            request=RouteBindingReconcileRequest(
                product="example-product",
                context="example-testing",
                instance="web",
                evaluated_at="2026-07-12T00:15:00Z",
            ),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "domains_invalid")


def _ready_route_binding_reconcile_store(
    *,
    route_binding: EnvironmentRouteBindingRecord | None = None,
    dokploy_target: DokployTargetRecord | None = None,
    ingress_audits: tuple[IngressRouteAuditRecord, ...] | None = None,
) -> _RouteBindingReconcileFakeStore:
    return _RouteBindingReconcileFakeStore(
        provider_target=_provider_target_record(),
        dokploy_target=dokploy_target or _dokploy_target_record(),
        edge_endpoints=(_edge_endpoint_record(),),
        ingress_audits=ingress_audits or (_ingress_audit_record(),),
        route_binding=route_binding,
    )


class RouteBindingReconcileTests(unittest.TestCase):
    def test_reconcile_is_ready_after_domain_authority_repair(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            target_record = _dokploy_target_record(domains=("https://app.example.test",))
            target_id_record = _dokploy_target_id_record()
            provider_target_record = ProviderTargetRecord.from_dokploy_records(
                target_record=target_record,
                target_id_record=target_id_record,
            )
            store.write_dokploy_target_record(target_record)
            store.write_dokploy_target_id_record(target_id_record)
            store.write_provider_target_record(provider_target_record)
            store.write_edge_endpoint_record(_edge_endpoint_record())
            store.write_ingress_route_audit_record(_ingress_audit_record())

            repair_dokploy_target_domain_authority(
                record_store=store,
                host="synthetic-host",
                token="synthetic-token",
                context=target_record.context,
                instance=target_record.instance,
                target_type=target_record.target_type,
                target_id=target_id_record.target_id,
                domains=("app.example.test",),
                expected_current_provider_target=(
                    provider_target_record.to_deployed_target_reference()
                ),
                updated_at="2026-07-12T00:05:00Z",
                apply=True,
                fetch_target_payload=lambda *_args: {"composeId": target_id_record.target_id},
                fetch_target_domains=lambda *_args: _live_domains("app.example.test"),
            )
            plan = plan_route_binding_reconcile(
                record_store=store,
                request=_route_binding_reconcile_request(),
            )
            store.close()

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.operation, "create")

    def test_reconcile_plans_create_with_service_owned_freshness(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(),
            request=_route_binding_reconcile_request(),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.operation, "create")
        assert plan.record is not None
        self.assertEqual(plan.record.source.source_kind, "service")
        self.assertEqual(plan.record.source.stale_after, "2026-07-21T00:00:00Z")

    def test_reconcile_is_unchanged_before_refresh_half_life(self) -> None:
        created = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(),
            request=_route_binding_reconcile_request(),
        )
        assert created.record is not None

        plan = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(route_binding=created.record),
            request=_route_binding_reconcile_request(
                evaluated_at="2026-07-20T01:00:00Z",
                current_record=created.record,
            ),
        )

        self.assertEqual(plan.status, "unchanged")
        self.assertEqual(plan.operation, "none")
        self.assertEqual(
            plan.candidate_record_sha256,
            route_binding_record_sha256(created.record),
        )

    def test_reconcile_refreshes_at_half_life(self) -> None:
        created = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(),
            request=_route_binding_reconcile_request(),
        )
        assert created.record is not None

        plan = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(route_binding=created.record),
            request=_route_binding_reconcile_request(
                evaluated_at="2026-07-20T12:00:00Z",
                current_record=created.record,
            ),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.operation, "refresh")
        assert plan.record is not None
        self.assertEqual(plan.record.source.stale_after, "2026-07-21T12:00:00Z")

    def test_reconcile_refreshes_changed_evidence_without_changing_authority(self) -> None:
        created = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(),
            request=_route_binding_reconcile_request(),
        )
        assert created.record is not None
        refreshed_audit = _ingress_audit_record(
            record_id="audit-2",
            recorded_at="2026-07-20T02:00:00Z",
        )

        plan = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(
                route_binding=created.record,
                ingress_audits=(_ingress_audit_record(), refreshed_audit),
            ),
            request=_route_binding_reconcile_request(
                evaluated_at="2026-07-20T03:00:00Z",
                current_record=created.record,
            ),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.operation, "refresh")
        assert plan.record is not None
        self.assertIn("ingress-audit:audit-2", plan.record.source.source_record_ids)

    def test_reconcile_reports_authority_conflict_instead_of_overwrite(self) -> None:
        created = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(),
            request=_route_binding_reconcile_request(),
        )
        assert created.record is not None
        changed_domain = "changed.example.test"

        plan = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(
                route_binding=created.record,
                dokploy_target=_dokploy_target_record(domains=(changed_domain,)),
                ingress_audits=(_ingress_audit_record(domains=(changed_domain,)),),
            ),
            request=_route_binding_reconcile_request(current_record=created.record),
        )

        self.assertEqual(plan.status, "conflict")
        self.assertEqual(plan.findings[0].code, "route_binding_authority_conflict")

    def test_reconcile_reports_changed_expected_current(self) -> None:
        current = _route_binding_record()

        plan = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(route_binding=current),
            request=RouteBindingReconcileRequest(
                product=current.product,
                context=current.context,
                instance=current.instance,
                expected_current=RouteBindingExpectedCurrent(
                    state="present",
                    record_sha256="0" * 64,
                ),
                evaluated_at="2026-07-20T00:00:00Z",
            ),
        )

        self.assertEqual(plan.status, "conflict")
        self.assertEqual(plan.findings[0].code, "expected_current_changed")

    def test_reconcile_reports_present_and_missing_expected_current_conflicts(self) -> None:
        current = _route_binding_record()
        expected_present = RouteBindingExpectedCurrent(
            state="present",
            record_sha256=route_binding_record_sha256(current),
        )

        unexpected_present = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(route_binding=current),
            request=_route_binding_reconcile_request(),
        )
        unexpected_missing = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(),
            request=RouteBindingReconcileRequest(
                product=current.product,
                context=current.context,
                instance=current.instance,
                expected_current=expected_present,
                evaluated_at="2026-07-20T00:00:00Z",
            ),
        )

        self.assertEqual(unexpected_present.findings[0].code, "expected_current_present")
        self.assertEqual(unexpected_missing.findings[0].code, "expected_current_missing")

    def test_reconcile_refuses_operator_owned_record(self) -> None:
        current = _route_binding_record()

        plan = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(route_binding=current),
            request=_route_binding_reconcile_request(current_record=current),
        )

        self.assertEqual(plan.status, "conflict")
        self.assertEqual(plan.findings[0].code, "route_binding_ownership_conflict")

    def test_reconcile_accepts_explicitly_relinquished_external_authority(self) -> None:
        current = _route_binding_record().model_copy(
            update={
                "ingress": RouteBindingIngress(
                    provider="external",
                    endpoint_key="external:app.example.test",
                    termination_kind="edge",
                ),
                "tls": RouteBindingTls(owner="external"),
                "status": "disabled",
            }
        )

        plan = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(route_binding=current),
            request=_route_binding_reconcile_request(current_record=current),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.operation, "refresh")
        self.assertEqual(plan.findings[0].code, "external_route_authority_handoff")
        self.assertIsNotNone(plan.record)
        assert plan.record is not None
        self.assertEqual(plan.record.source.source_kind, "service")
        self.assertEqual(plan.record.ingress.provider, "npmplus")
        self.assertEqual(plan.record.status, "active")

    def test_reconcile_rejects_future_source_evidence(self) -> None:
        plan = plan_route_binding_reconcile(
            record_store=_ready_route_binding_reconcile_store(
                ingress_audits=(
                    _ingress_audit_record(),
                    _ingress_audit_record(
                        record_id="audit-future",
                        recorded_at="2026-07-21T00:00:00Z",
                    ),
                )
            ),
            request=_route_binding_reconcile_request(),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.findings[0].code, "evidence_timestamp_future")


def _route_binding_policy(
    *,
    action: str = "route_binding.read",
    instances: tuple[str, ...] = ("web",),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["example-product"],
                    "contexts": ["example-testing"],
                    "instances": list(instances),
                    "actions": [action],
                }
            ],
        }
    )


def _route_binding_reconcile_payload(
    *,
    mode: str = "apply",
    current_record: EnvironmentRouteBindingRecord | None = None,
    instance: str = "web",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "mode": mode,
        "product": "example-product",
        "context": "example-testing",
        "instance": instance,
        "expected_current": _route_binding_expected_current(current_record).model_dump(mode="json"),
    }
    if mode == "apply":
        payload.update(
            {
                "reason": "Reconcile reviewed environment route binding.",
                "confirmation": "APPLY LAUNCHPLANE ROUTE BINDING RECONCILE",
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
            list_response = await _get_route_binding_records(app, instance="web")

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(list_response.status_code, 200)
        read_payload = read_response.json()
        list_payload = list_response.json()
        self.assertEqual(read_payload["record"]["product"], "example-product")
        self.assertEqual(
            read_payload["record"]["record_sha256"],
            route_binding_record_sha256(_route_binding_record()),
        )
        self.assertNotIn("provider_evidence", read_payload["record"]["provider_target"])
        self.assertNotIn("provider_evidence", read_payload["record"]["ingress"])
        self.assertEqual(list_payload["count"], 1)
        self.assertNotIn("provider_evidence", list_payload["records"][0]["tls"])

    async def test_route_binding_reads_require_scoped_authz(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(action="edge_endpoint.read", instances=()),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _get_route_binding_record(app)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_route_binding_reads_require_instance_authority(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _get_route_binding_record(app, instance="other")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_route_binding_context_list_uses_context_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_route_binding_record(_route_binding_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_route_binding_policy(instances=()),
                record_store_factory=lambda: store,
            )

            list_response = await _get_route_binding_records(app)
            current_response = await _get_route_binding_record(app)

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(current_response.status_code, 403)
        self.assertEqual(current_response.json()["error"]["code"], "authorization_denied")

    async def test_route_binding_reconcile_apply_requires_scoped_authz(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/route-bindings/reconcile",
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "route-binding-reconcile-denied",
            },
            payload=_route_binding_reconcile_payload(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_route_binding_reconcile_apply_requires_instance_authority(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(action="route_binding.apply"),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/route-bindings/reconcile",
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "route-binding-reconcile-wrong-instance",
            },
            payload=_route_binding_reconcile_payload(instance="other"),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_route_binding_reconcile_apply_requires_idempotency_key(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(action="route_binding.apply"),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/route-bindings/reconcile",
            headers={"Authorization": "Bearer valid-token"},
            payload=_route_binding_reconcile_payload(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_route_binding_reconcile_apply_requires_database_mutation_store(self) -> None:
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
                "/v1/route-bindings/reconcile",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "route-binding-reconcile-filesystem",
                },
                payload=_route_binding_reconcile_payload(),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_route_binding_reconcile_apply_reports_running_reservation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            identity = _identity()
            payload = _route_binding_reconcile_payload()
            route_path = "/v1/route-bindings/reconcile"
            idempotency_key = "route-binding-reconcile-running"
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

    async def test_route_binding_reconcile_apply_releases_expired_reservation(self) -> None:
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
            identity = _identity()
            payload = _route_binding_reconcile_payload()
            route_path = "/v1/route-bindings/reconcile"
            idempotency_key = "route-binding-reconcile-expired"
            with patch.object(
                store,
                "_database_mutation_timestamp",
                return_value="2026-07-20T00:00:00Z",
            ):
                store.reserve_mutation(
                    scope=idempotency_scope(identity),
                    route_path=route_path,
                    idempotency_key=idempotency_key,
                    request_fingerprint=idempotency_request_fingerprint(
                        route_path=route_path,
                        payload=payload,
                    ),
                    lease_owner="expired-worker",
                    lease_seconds=1,
                )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=_route_binding_policy(action="route_binding.apply"),
                record_store_factory=lambda: store,
            )

            with (
                patch.object(
                    store,
                    "_database_mutation_timestamp",
                    return_value="2026-07-20T00:00:02Z",
                ),
                patch(
                    "control_plane.http_app.utc_now_timestamp",
                    return_value="2026-07-12T00:15:00Z",
                ),
            ):
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
            loaded = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="web",
            )
            stored_reservation = store.read_idempotency_record(
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=idempotency_key,
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"]["route_binding_status"], "created")
        self.assertIsNotNone(loaded)
        assert stored_reservation is not None
        self.assertEqual(stored_reservation.state, "completed")
        self.assertEqual(stored_reservation.lease_owner, response.json()["trace_id"])

    async def test_route_binding_reconcile_apply_writes_record_without_provider_evidence_response(
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
                "Idempotency-Key": "route-binding-reconcile-1",
            }
            request_payload = _route_binding_reconcile_payload()
            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-12T00:15:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/reconcile",
                    headers=request_headers,
                    payload=request_payload,
                )
                replayed_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/reconcile",
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
                route_path="/v1/route-bindings/reconcile",
                idempotency_key="route-binding-reconcile-1",
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(replayed_response.status_code, 202)
        replayed_payload = replayed_response.json()
        self.assertTrue(replayed_payload["replayed"])
        self.assertEqual(replayed_payload["original_trace_id"], payload["trace_id"])
        self.assertEqual(replayed_payload["result"], payload["result"])
        self.assertEqual(payload["records"]["route_binding_status"], "created")
        self.assertNotIn("provider_evidence", payload["result"]["record"]["provider_target"])
        self.assertEqual(loaded.ingress.endpoint_key, "example-edge")
        self.assertIsNotNone(stored_reservation)
        assert stored_reservation is not None
        self.assertEqual(stored_reservation.state, "completed")

    async def test_route_binding_reconcile_apply_refreshes_existing_evidence(self) -> None:
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
            current_plan = plan_route_binding_reconcile(
                record_store=store,
                request=_route_binding_reconcile_request(evaluated_at="2026-07-19T00:00:00Z"),
            )
            assert current_plan.record is not None
            current_record = current_plan.record
            store.write_route_binding_record(current_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_route_binding_policy(action="route_binding.apply"),
                record_store_factory=lambda: store,
            )

            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-20T00:00:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/reconcile",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "route-binding-refresh-1",
                    },
                    payload=_route_binding_reconcile_payload(current_record=current_record),
                )
            refreshed_record = store.read_route_binding_record(
                product=current_record.product,
                context_name=current_record.context,
                instance_name=current_record.instance,
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"]["route_binding_status"], "refreshed")
        self.assertEqual(payload["result"]["operation"], "refresh")
        self.assertEqual(
            refreshed_record.source.stale_after,
            "2026-07-21T00:00:00Z",
        )
        self.assertNotEqual(
            route_binding_record_sha256(current_record),
            route_binding_record_sha256(refreshed_record),
        )

    async def test_route_binding_reconcile_dry_run_reports_blockers_without_writing(self) -> None:
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
                authz_policy=_route_binding_policy(action="route_binding.read"),
                record_store_factory=lambda: store,
            )

            with patch(
                "control_plane.http_app.utc_now_timestamp",
                return_value="2026-07-12T00:15:00Z",
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/route-bindings/reconcile",
                    headers={"Authorization": "Bearer valid-token"},
                    payload=_route_binding_reconcile_payload(mode="dry-run"),
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

    async def test_route_binding_reconcile_refuses_operator_owned_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            existing_record = _route_binding_record(domain="existing.example.test")
            store.write_route_binding_record(existing_record)
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

            response = await _asgi_request(
                app,
                "POST",
                "/v1/route-bindings/reconcile",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "route-binding-reconcile-existing",
                },
                payload=_route_binding_reconcile_payload(current_record=existing_record),
            )
            loaded = store.read_route_binding_record(
                product="example-product",
                context_name="example-testing",
                instance_name="web",
            )
            stored_reservation = store.read_idempotency_record(
                scope=idempotency_scope(_identity()),
                route_path="/v1/route-bindings/reconcile",
                idempotency_key="route-binding-reconcile-existing",
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"]["route_binding_status"], "conflict")
        self.assertEqual(
            payload["result"]["findings"][0]["code"],
            "route_binding_ownership_conflict",
        )
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
            openapi["paths"]["/v1/route-bindings/reconcile"]["post"]["operationId"],
            "reconcile_route_binding",
        )
        self.assertNotIn("/v1/route-bindings/backfill/apply", openapi["paths"])
        self.assertEqual(
            openapi["paths"]["/v1/route-bindings/reconcile"]["post"]["responses"]["202"]["content"][
                "application/json"
            ]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertEqual(
            openapi["components"]["schemas"]["RouteBindingRecordsResponse"]["additionalProperties"],
            False,
        )
        self.assertIn("RouteBindingRecordResponse", openapi["components"]["schemas"])
        self.assertIn("RouteBindingReconcileEnvelope", openapi["components"]["schemas"])
        self.assertIn("RouteBindingExpectedCurrent", openapi["components"]["schemas"])
        self.assertIn("AcceptedEvidenceResponse", openapi["components"]["schemas"])


if __name__ == "__main__":
    unittest.main()
