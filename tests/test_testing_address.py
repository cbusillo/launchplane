from datetime import UTC, datetime, timedelta
from copy import deepcopy
import json
import unittest
from typing import Any
from unittest.mock import patch
import asyncio
from collections.abc import Callable
from tests import test_testing_route_transport as transport_tests
from control_plane.provider_operations import DurableProviderOperationResult

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.testing_address import (
    TestingAddressPlan,
    address_binding,
    retained_address_custody_sha256,
)
from control_plane.contracts.disposable_lane import DisposableTestingSupersession
from control_plane.dokploy.testing_route import amend_testing_route_address
from control_plane.dokploy.testing_address_recipients import qualify_testing_address_recipients
from control_plane.storage import postgres as db
from control_plane.testing_generation_retirement import (
    RetainedTestingGeneration,
    TestingGenerationRetirementPlan,
)
from control_plane.odoo_testing_restore import generation_binding
from control_plane.testing_address_storage import (
    save_address_plan,
    read_address_plan,
    commit_testing_address,
    effective_testing_route,
    amended_testing_maintenance_overrides,
)
from control_plane.testing_address_qualification import exclude_address_executors
from control_plane.testing_serving_copy import published_testing_copy
from control_plane.testing_publication_storage import PUBLICATION_ROUTE, PUBLICATION_SCOPE
from tests import test_testing_publication as publication_tests
from tests.test_odoo_testing_restore import generation


class TestingAddressTests(unittest.TestCase):
    database_url = ""
    store: db.PostgresRecordStore

    def setUp(self) -> None:
        self.fixture = publication_tests.TestingPublicationTests()
        self.fixture.database_url = self.database_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.prepare()
        self.publication = self.fixture.publish()
        self.store = self.fixture.store
        self.product, self.context = self.fixture.candidate.product, self.fixture.candidate.context
        self.now = datetime.now(UTC)
        self.hostname = "canonical-testing.example.invalid"
        original = self.store.list_odoo_stable_target_replacement_operation_records(
            context_name=self.context, instance_name="testing"
        )[0]
        self.operation = original.model_copy(
            update={
                "operation_id": "address-original",
                "status": "superseded",
                "finished_at": self.now.isoformat(),
                "lease_owner": "",
                "lease_expires_at": "",
                "result": None,
                "cancellation": None,
                "disposable_supersession": DisposableTestingSupersession(
                    binding_sha256=canonical_json_sha256({"isolation": "verified"}),
                    retained_target_id="retained-target",
                    replacement_target_id=self.fixture.candidate.compose_id,
                    evidence_reference="fixture-isolated-successor",
                    recorded_at=self.now,
                ),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(self.operation)
        retained_generation = generation().model_copy(
            update={
                "product": self.product,
                "context": self.context,
                "operation_id": self.operation.operation_id,
                "target_id": "retained-target",
                "domain_host": self.hostname,
            }
        )
        retained = RetainedTestingGeneration(
            generation=retained_generation,
            operation=self.operation,
            target=self.fixture.before.target.model_copy(update={"domains": (self.hostname,)}),
            plan=TestingGenerationRetirementPlan(
                product=self.product,
                context=self.context,
                operation_id=self.operation.operation_id,
                plan_sha256=canonical_json_sha256({"retirement": "original"}),
                generation_binding_sha256=generation_binding(retained_generation),
                applied=True,
            ),
            retired_at=self.now.isoformat(),
            retired_by="fixture",
            reason="Preserve the original copy",
        )
        with self.store._session_factory() as session:
            session.add(
                db.LaunchplaneRetainedTestingGenerationRow(
                    operation_id=self.operation.operation_id,
                    generation_binding_sha256=generation_binding(retained_generation),
                    payload=self.store._payload_dict(retained),
                )
            )
            session.commit()
        before = self.fixture.current()
        assert before.binding is not None
        after = amend_testing_route_address(self.fixture.route, self.hostname)
        self.plan = TestingAddressPlan(
            product=self.product,
            context=self.context,
            retirement_operation_id=self.operation.operation_id,
            reason="Restore canonical testing hostname",
            publication_key=self.publication.plan.key,
            publication_sha256=canonical_json_sha256(self.publication.model_dump(mode="json")),
            profile=self.store.read_product_profile_record(self.product),
            before=before,
            route_before=self.fixture.route,
            route_after=after,
            binding_after=address_binding(before.binding, after),
            reservations=self.store.read_retained_testing_hostname_reservations(self.context),
            server_id="testing-server",
            provider_project=self.fixture.candidate.namespace,
            retained_projects=("retained-project",),
            recipient_sha256=canonical_json_sha256({"recipients": "stopped"}),
            domain_inventory_sha256=canonical_json_sha256({"inventory": "complete"}),
            operation_sha256={
                self.operation.operation_id: retained_address_custody_sha256(self.operation)
            },
            retirement_sha256=retained_address_custody_sha256(retained),
            created_at=self.now,
        )
        self.reservation = self.store.reserve_mutation(
            scope="fixture-address",
            route_path="fixture-address",
            idempotency_key="original-key",
            request_fingerprint=self.plan.digest,
            lease_owner="fixture",
            lease_seconds=300,
            reconciliation_key=self.plan.digest,
            provider_target_key="fixture-address-target",
        ).record
        self.health = tuple(
            self.fixture.health.model_copy(
                update={
                    "urls": ("https://" + host + "/launchplane/health",),
                    "runtime_identity_status": "match",
                }
            )
            for host in (self.hostname, self.plan.route_before.host)
        )

    def commit(self) -> object:
        with exclude_address_executors(self.store, tuple(self.plan.operation_sha256)):
            return commit_testing_address(
                self.store, self.plan, self.reservation, self.now, self.health
            )

    def test_canonical_amendment_preserves_copy_and_original_receipts(self) -> None:
        with self.store._session_factory() as session:
            profile = session.get(db.LaunchplaneProductProfileRow, self.product)
            operation = session.get(
                db.LaunchplaneOdooStableTargetReplacementOperationRow, self.operation.operation_id
            )
            retained = session.get(
                db.LaunchplaneRetainedTestingGenerationRow, self.operation.operation_id
            )
            publication = session.scalar(
                self.store._idempotency_statement(
                    scope=PUBLICATION_SCOPE,
                    route_path=PUBLICATION_ROUTE,
                    idempotency_key=self.plan.publication_key,
                )
            )
            assert profile is not None and operation is not None
            assert retained is not None and publication is not None
            profile_payload = deepcopy(profile.payload)
            profile_payload.pop("repository_owner_id")
            profile.payload = profile_payload
            operation_payload = deepcopy(operation.payload)
            operation_payload.pop("idempotency_scope")
            operation.payload = operation_payload
            retained_payload = deepcopy(retained.payload)
            retained_payload["operation"].pop("idempotency_scope")
            retained.payload = retained_payload
            publication_payload = deepcopy(publication.payload)
            publication_payload["response_payload"]["plan"]["before"].pop("generation")
            publication.payload = publication_payload
            session.commit()
        self.plan = self.plan.model_copy(
            update={
                "reservations": self.store.read_retained_testing_hostname_reservations(self.context)
            }
        )
        self.reservation = self.store.reserve_mutation(
            scope="fixture-address",
            route_path="fixture-address",
            idempotency_key="historical-original-key",
            request_fingerprint=self.plan.digest,
            lease_owner="fixture",
            reconciliation_key=self.plan.digest,
            provider_target_key="fixture-historical-address-target",
        ).record
        legacy_payload = self.plan.model_dump(mode="json")
        legacy_payload.pop("retained_automation", None)
        self.assertEqual(TestingAddressPlan.model_validate(legacy_payload).digest, self.plan.digest)
        for automation in ({"foreign-target": True}, {"retained-target": "true"}):
            with self.subTest(automation=automation), self.assertRaises(ValueError):
                TestingAddressPlan.model_validate(
                    {**legacy_payload, "retained_automation": automation}
                )
        save_address_plan(self.store, self.plan)
        self.assertEqual(read_address_plan(self.store, self.plan.digest), self.plan)
        original = self.fixture.current()
        receipt = self.commit()
        self.assertEqual(self.commit(), receipt)
        publication = published_testing_copy(self.store, product=self.product, context=self.context)
        self.assertEqual(publication, self.publication)
        assert publication is not None
        self.assertEqual(effective_testing_route(self.store, publication), self.plan.route_after)
        current = self.fixture.current()
        self.assertEqual(current.target, original.target)
        self.assertEqual(current.runtime, original.runtime)
        self.assertEqual(current.generation, original.generation)
        self.assertEqual(current.inventory, original.inventory)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )
        self.assertEqual(
            self.store.read_retained_testing_hostname_reservations(self.context),
            self.plan.reservations,
        )
        assert current.overrides is not None
        stale = current.overrides.model_copy(
            update={"website_bootstrap": current.overrides.website_bootstrap}
        )
        rendering = amended_testing_maintenance_overrides(self.store, self.context, stale)
        assert rendering is not None
        self.assertEqual(
            next(
                item.value.value
                for item in rendering.config_parameters
                if item.key == "web.base.url"
            ),
            "https://" + self.hostname,
        )

    def test_health_refusal_rolls_back_every_canonical_record(self) -> None:
        before = self.fixture.current()
        profile = self.store.read_product_profile_record(self.product)
        self.health = (
            self.health[0].model_copy(update={"runtime_identity_status": "mismatch"}),
            self.health[1],
        )
        with self.assertRaises(ValueError):
            self.commit()
        self.assertEqual(self.fixture.current(), before)
        self.assertEqual(self.store.read_product_profile_record(self.product), profile)

    def test_expiry_during_canonical_transaction_rolls_back_every_record(self) -> None:
        from control_plane.testing_address_storage import read_testing_address

        before = self.fixture.current()
        profile = self.store.read_product_profile_record(self.product)
        expired = (
            datetime.fromisoformat(self.reservation.lease_expires_at) + timedelta(seconds=1)
        ).isoformat()
        times = iter((self.now.isoformat(), expired))
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            side_effect=lambda _session: next(times, expired),
        ):
            with self.assertRaises(ValueError):
                self.commit()
        self.assertEqual(self.fixture.current(), before)
        self.assertEqual(self.store.read_product_profile_record(self.product), profile)
        self.assertIsNone(read_testing_address(self.store, self.plan.publication_key))

    def test_amended_copy_accepts_an_ordinary_later_testing_build(self) -> None:
        from tests.test_testing_rebuild_service import exercise_published_copy_maintenance

        self.commit()
        exercise_published_copy_maintenance(self, self.fixture)
        self.assertEqual(
            effective_testing_route(self.store, self.publication), self.plan.route_after
        )

    def test_http_plan_is_bound_and_scoped_callers_cannot_apply_it(self) -> None:
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request
        from tests.support.auth import scoped_agent_transport
        from control_plane.testing_address import ROUTE

        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.fixture.root,
        )
        body = {
            "context": self.context,
            "retirement_operation_id": self.operation.operation_id,
            "reservation_operation_id": self.operation.operation_id,
            "hostname": self.hostname,
            "reason": "Restore canonical testing hostname",
        }

        def post(**changes: object) -> Any:
            return asyncio.run(
                request(
                    app,
                    "POST",
                    ROUTE.format(product=self.product),
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "original-key",
                    },
                    payload={**body, **changes},
                )
            )

        with patch(
            "control_plane.http_routes.testing_address.plan_testing_address", return_value=self.plan
        ) as planner:
            response = post()
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["result"]["plan_sha256"], self.plan.digest)
            self.assertEqual(read_address_plan(self.store, self.plan.digest), self.plan)
            planner.reset_mock()
            response = post(instance="prod")
            self.assertEqual(response.status_code, 400, response.text)
            planner.assert_not_called()
            with scoped_agent_transport():
                response = post()
            self.assertEqual(response.status_code, 403, response.text)
            planner.assert_not_called()

    def test_http_refusal_logs_sanitized_cause_with_response_trace(self) -> None:
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
        from control_plane.testing_address_qualification import TestingAddressRecipientUnverified
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request
        from control_plane.testing_address import ROUTE

        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.fixture.root,
        )
        private = "private-provider-error-sentinel"
        failures = (
            (ValueError(private), 409),
            (TestingAddressRecipientUnverified(True, 1), 409),
            (
                NativeSourceAuthorizationDenied(private),
                403,
            ),
        )
        before = self.fixture.current()
        for failure, status in failures:
            with (
                self.subTest(status=status),
                patch(
                    "control_plane.http_routes.testing_address.plan_testing_address",
                    side_effect=failure,
                ),
                self.assertLogs("control_plane.http_error_evidence", level="WARNING") as captured,
            ):
                response = asyncio.run(
                    request(
                        app,
                        "POST",
                        ROUTE.format(product=self.product),
                        headers={"Authorization": "Bearer local-operator-token"},
                        payload={
                            "context": self.context,
                            "retirement_operation_id": self.operation.operation_id,
                            "reservation_operation_id": self.operation.operation_id,
                            "hostname": self.hostname,
                            "reason": "Restore canonical testing hostname",
                        },
                    )
                )
            self.assertEqual(response.status_code, status, response.text)
            if isinstance(failure, TestingAddressRecipientUnverified):
                self.assertEqual(
                    response.json()["error"]["code"], "testing_address_recipient_unverified"
                )
                self.assertIn(failure.auto_deploy, response.json()["error"]["message"])
                self.assertIn(str(failure.schedule_count), response.json()["error"]["message"])
            evidence = next(
                item
                for item in (json.loads(record.getMessage()) for record in captured.records)
                if item.get("trace_id") == response.json()["trace_id"]
            )
            self.assertEqual(evidence["trace_id"], response.json()["trace_id"])
            self.assertTrue(evidence["sites"])
            self.assertNotIn(private, response.text + " ".join(captured.output))
            self.assertEqual(self.fixture.current(), before)

    def test_changed_original_custody_refuses_without_releasing_it(self) -> None:
        before = self.fixture.current()
        with self.assertRaises(ValueError):
            commit_testing_address(self.store, self.plan, self.reservation, self.now, self.health)
        self.assertEqual(self.fixture.current(), before)
        self.store.write_odoo_stable_target_replacement_operation_record(
            self.operation.model_copy(
                update={"updated_at": (self.now + timedelta(seconds=1)).isoformat()}
            )
        )
        before = self.fixture.current()
        with self.assertRaises(ValueError):
            self.commit()
        self.assertEqual(self.fixture.current(), before)

    def test_raw_hostname_reservation_custody_refuses_representation_changes(self) -> None:
        with self.store._session_factory() as session:
            retained = session.get(
                db.LaunchplaneRetainedTestingGenerationRow, self.operation.operation_id
            )
            assert retained is not None
            payload = deepcopy(retained.payload)
            payload["plan"].pop("original_authority")
            retained.payload = payload
            session.commit()
        before = self.fixture.current()
        with self.assertRaises(ValueError):
            self.commit()
        self.assertEqual(self.fixture.current(), before)

    def test_amended_route_refresh_and_next_rebuild_retain_both_hosts(self) -> None:
        from tests.test_testing_route import TestingRouteTests
        from control_plane.dokploy.testing_route import route_document, compile_testing_route
        from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
        from control_plane.dokploy import api
        from control_plane.native_testing_route_refresh import plan_native_testing_route_refresh

        self.commit()
        route = self.plan.route_after
        routed = TestingRouteTests()
        routed.setUp()
        raw = routed.apply(route, route.new_backend, retire_old=True)
        owned = route_document(route, route.new_backend)
        identity = self.fixture.plan.deployment.runtime_identity
        assert identity is not None
        frame = {
            "old_identity": identity.model_dump(mode="json"),
            "old_endpoints": (route.new_backend,),
            "raw": raw,
            "owned_file": json.dumps(owned),
        }
        binding = self.fixture.current().binding
        assert binding is not None
        with (
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={
                    "composeId": self.fixture.candidate.compose_id,
                    "name": self.fixture.candidate.namespace,
                    "appName": self.plan.provider_project,
                    "serverId": self.plan.server_id,
                },
            ),
            patch.object(DokployTestingRouteTransport, "read", return_value=frame),
        ):
            refresh = plan_native_testing_route_refresh(
                self.store, root=self.fixture.fixture.root, current=binding
            )
        self.assertEqual(refresh.status, "unchanged")
        rebuilt = compile_testing_route(
            product=self.product,
            context=self.context,
            host=route.host,
            aliases=route.aliases,
            old_backend=route.new_backend,
            old_backend_addresses=(route.new_backend,),
            new_backend="http://lp-testing-" + "b" * 32 + ":8069",
            raw=raw,
            public_entrypoints=route.public_entrypoints,
            owned_document=owned,
            recorded_template=route.template,
        )
        self.assertEqual(rebuilt.aliases, route.aliases)
        self.assertFalse(rebuilt.address_only)

    def test_lost_metadata_reply_completes_from_committed_receipt(self) -> None:
        from unittest.mock import Mock
        from control_plane.testing_address import TestingAddressAdapter, ROUTE
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.provider_operations import (
            run_durable_provider_operation,
            ProviderEvidenceLease,
        )

        with patch("control_plane.testing_address.address_transport", return_value=Mock()):
            adapter = TestingAddressAdapter(
                self.fixture.fixture.root, self.store, self.plan, "fixture-address"
            )

        def committed_without_reply(lease: ProviderEvidenceLease) -> None:
            commit_testing_address(
                self.store, self.plan, lease.evidence_reservation(), self.now, self.health
            )
            raise NativeSourceUnavailable("The metadata transaction committed without its reply")

        def run() -> Any:
            return run_durable_provider_operation(
                store=self.store,
                scope="address-test",
                route_path=ROUTE.format(product=self.product),
                idempotency_key="original-key",
                request_fingerprint=self.plan.digest,
                lease_owner="fixture",
                response_trace_id="fixture-address",
                adapter=adapter,
                original_intent={"testing_address_plan": self.plan.model_dump(mode="json")},
            )

        with patch.object(adapter, "apply_original", side_effect=committed_without_reply) as apply:
            with self.assertRaises(NativeSourceUnavailable):
                run()
            apply.side_effect = AssertionError("The committed original must not dispatch again")
            result = run()
            self.assertEqual(result.status, "completed")
            self.assertTrue(result.response_payload["result"]["applied"])
            apply.assert_called_once()

    def native_address_execution(
        self,
    ) -> tuple[
        transport_tests.TestingRouteTransportTests, Callable[[bool], DurableProviderOperationResult]
    ]:
        from tests.test_testing_route_transport import TestingRouteTransportTests
        from control_plane.dokploy.testing_route import route_document
        from control_plane.testing_address import TestingAddressAdapter, ROUTE
        from control_plane.provider_operations import run_durable_provider_operation

        native = TestingRouteTransportTests()
        native.setUp(initialize_fence=False)
        self.addCleanup(native.doCleanups)
        native.plan = self.plan.route_after
        native.route_file = native.root / (native.plan.route_id + ".yml")
        native.route_file.write_text(
            json.dumps(route_document(self.plan.route_before, self.plan.route_before.new_backend))
        )
        native.raw = native.fixture.apply(
            self.plan.route_before, self.plan.route_before.new_backend, retire_old=True
        )
        identity = (
            self.plan.before.inventory.runtime_identity if self.plan.before.inventory else None
        )
        assert identity is not None
        namespace = self.fixture.candidate.namespace
        native.candidate_project = namespace + "-abc123"
        web = native.containers["new"]
        web["Name"] = "/" + namespace + "-web-1"
        web["Config"]["Labels"]["com.docker.compose.project"] = native.candidate_project
        web["Config"]["Env"] = [
            "PLATFORM_INSTANCE=testing",
            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + identity.model_dump_json(),
        ]
        web["NetworkSettings"]["Networks"]["dokploy-network"]["Aliases"] = [namespace]
        native.containers["retired"] = {
            "Id": "retired",
            "Config": {"Labels": {"com.docker.compose.project": "retained-project"}},
            "Mounts": [],
            "State": {"Status": "exited"},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        }
        client = native.client
        client.product, client.context = self.product, self.context
        client.public_aliases = self.plan.route_after.aliases
        client.old_runtime_identity = identity
        client.payload.update(
            route_id=native.plan.route_id,
            host=native.plan.host,
            aliases=native.plan.aliases,
            old_project=native.candidate_project,
        )
        client.payload["address_recipients"] = {
            "hostname": native.plan.host,
            "projects": self.plan.retained_projects,
            "entrypoints": native.plan.public_entrypoints,
            "priority": min(
                item["priority"] for item in native.plan.template["http"]["routers"].values()
            ),
        }
        digest = client.read()["address_recipients_sha256"]
        self.plan = self.plan.model_copy(
            update={"recipient_sha256": digest, "provider_project": native.candidate_project}
        )
        self.unrelated_inventory_churning = False

        def provider_check() -> None:
            if getattr(self, "retained_auto_deploy", False):
                raise ValueError("The retained recipient can still deploy automatically")
            if self.unrelated_inventory_churning:
                raise ValueError("An unrelated preview prevented a provider membership snapshot")

        def run(cancel: bool) -> DurableProviderOperationResult:
            with (
                patch.object(TestingAddressAdapter, "provider_guard", side_effect=provider_check),
                patch(
                    "control_plane.testing_rebuild_service.read_public_testing_health",
                    side_effect=lambda base, _path, _identity: next(
                        item for item in self.health if item.urls[0].startswith(base + "/")
                    ),
                ),
            ):
                return run_durable_provider_operation(
                    store=self.store,
                    scope="address-test",
                    route_path=ROUTE.format(product=self.product),
                    idempotency_key="original-key",
                    request_fingerprint=self.plan.digest,
                    lease_owner="fixture",
                    response_trace_id="fixture-address",
                    adapter=TestingAddressAdapter(
                        native.root, self.store, self.plan, "fixture-address", cancel=cancel
                    ),
                    original_intent={"testing_address_plan": self.plan.model_dump(mode="json")},
                )

        return native, run

    def retained_recipient_execution(
        self, *, lost_reply_applies: bool
    ) -> tuple[
        transport_tests.TestingRouteTransportTests,
        Callable[[bool], DurableProviderOperationResult],
        Any,
    ]:
        from control_plane.dokploy import api, source
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        native, run = self.native_address_execution()
        self.retained_auto_deploy = True
        self.lose_retained_reply = True
        self.retained_update_applies = lost_reply_applies
        self.plan = self.plan.model_copy(update={"retained_automation": {"retained-target": True}})

        def update(**kwargs: Any) -> None:
            self.assertEqual(kwargs["path"], "/api/compose.update")
            self.assertEqual(
                kwargs["payload"], {"composeId": "retained-target", "autoDeploy": False}
            )
            if self.retained_update_applies:
                self.retained_auto_deploy = False
            if self.lose_retained_reply:
                raise NativeSourceUnavailable("The provider update reply was lost")

        self.enterContext(
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                side_effect=lambda **_kwargs: {
                    "composeId": "retained-target",
                    "serverId": self.plan.server_id,
                    "appName": self.plan.retained_projects[0],
                    "autoDeploy": self.retained_auto_deploy,
                },
            )
        )
        self.enterContext(
            patch.object(
                source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
            )
        )
        send = self.enterContext(patch.object(api, "dokploy_request", side_effect=update))
        return native, run, send

    def test_retained_preparation_disables_only_reviewed_recipient_before_route_publication(
        self,
    ) -> None:
        native, run, send = self.retained_recipient_execution(lost_reply_applies=True)
        self.lose_retained_reply = False
        result = run(False)
        self.assertEqual(result.status, "completed")
        send.assert_called_once()
        self.assertFalse(self.retained_auto_deploy)
        self.assertIn("publish_v2", native.remote_modes)

    def test_retained_preparation_recovers_lost_reply_without_another_update(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        native, run, send = self.retained_recipient_execution(lost_reply_applies=True)
        original = self.fixture.current()
        with self.assertRaises(NativeSourceUnavailable):
            run(False)
        self.assertEqual(self.fixture.current(), original)
        self.assertNotIn("publish_v2", native.remote_modes)
        result = run(False)
        self.assertEqual(result.status, "completed")
        send.assert_called_once()
        self.assertFalse(self.retained_auto_deploy)
        self.assertEqual(
            effective_testing_route(self.store, self.publication), self.plan.route_after
        )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )

    def test_retained_preparation_reconciles_enabled_flag_under_original_key(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        native, run, send = self.retained_recipient_execution(lost_reply_applies=False)
        with self.assertRaises(NativeSourceUnavailable):
            run(False)
        send.assert_called_once()
        self.assertNotIn("publish_v2", native.remote_modes)
        self.retained_update_applies = True
        self.lose_retained_reply = False
        result = run(False)
        payload = result.response_payload["result"]
        assert isinstance(payload, dict)
        self.assertTrue(payload["applied"])
        self.assertEqual(send.call_count, 2)
        self.assertFalse(self.retained_auto_deploy)
        self.assertIn("publish_v2", native.remote_modes)

    def cancel_pending_preparation(self, *, denied: bool) -> None:
        from control_plane.dokploy import api
        from control_plane.dokploy.native_copy_stream import (
            NativeSourceAuthorizationDenied,
            NativeSourceUnavailable,
        )
        from control_plane.testing_address import ROUTE
        from control_plane.testing_address_preparation import PREPARATION_ROUTE
        from control_plane.contracts.testing_address import ADDRESS_SCOPE

        native, run, send = self.retained_recipient_execution(lost_reply_applies=False)
        if denied:
            send.side_effect = api.DokployRequestFailed(
                method="POST", path="/api/compose.update", detail="fixture denied", status_code=403
            )
        with self.assertRaises(
            NativeSourceAuthorizationDenied if denied else NativeSourceUnavailable
        ):
            run(False)
        child_key = self.plan.digest + ":retained-target"
        child = self.store.read_idempotency_record(
            scope=ADDRESS_SCOPE, route_path=PREPARATION_ROUTE, idempotency_key=child_key
        )
        assert child is not None
        profile = self.store.read_product_profile_record(self.product)
        changed = profile.model_copy(
            update={"updated_at": (self.now + timedelta(seconds=1)).isoformat()}
        )
        self.store.write_product_profile_record(changed)
        result = run(True)
        payload = result.response_payload["result"]
        assert isinstance(payload, dict)
        self.assertTrue(payload["cancelled"])
        self.assertEqual(payload["retained_preparation_fences_preserved"], ["retained-target"])
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=ADDRESS_SCOPE, route_path=PREPARATION_ROUTE, idempotency_key=child_key
            ),
            child,
        )
        self.assertEqual(
            self.store.list_held_provider_target_reservations(
                provider_target_key=child.provider_target_key
            ),
            (child,),
        )
        parent = self.store.read_idempotency_record(
            scope="address-test",
            route_path=ROUTE.format(product=self.product),
            idempotency_key="original-key",
        )
        assert parent is not None
        self.assertEqual(parent.state, "completed")
        self.assertEqual(
            self.store.list_held_provider_target_reservations(
                provider_target_key=parent.provider_target_key
            ),
            (),
        )
        self.assertEqual(self.store.read_product_profile_record(self.product), changed)
        self.assertNotIn("publish_v2", native.remote_modes)
        send.assert_called_once()
        from control_plane.testing_address import TestingAddressAdapter

        with self.assertRaises(ValueError):
            TestingAddressAdapter(
                native.root, self.store, self.plan, "fixture-forward"
            ).require_forward_operation()

    def test_cancel_keeps_unknown_preparation_fence_without_another_update(self) -> None:
        self.cancel_pending_preparation(denied=False)

    def test_cancel_after_denied_preparation_frees_only_serving_copy_fence(self) -> None:
        self.cancel_pending_preparation(denied=True)

    def test_preparation_read_failure_before_checkpoint_can_resume_original_request(self) -> None:
        from control_plane.dokploy import api

        _native, run, send = self.retained_recipient_execution(lost_reply_applies=True)
        self.lose_retained_reply = False
        with patch.object(
            api,
            "fetch_dokploy_target_payload",
            side_effect=api.DokployRequestFailed(
                method="GET",
                path="/api/compose.one",
                detail="temporary provider outage",
                status_code=503,
            ),
        ):
            with self.assertRaises(api.DokployRequestFailed):
                run(False)
        send.assert_not_called()
        result = run(False)
        self.assertEqual(result.status, "completed")
        send.assert_called_once()

    def test_recipient_preparation_refuses_shared_target_before_dispatch(self) -> None:
        from types import SimpleNamespace

        native, run, send = self.retained_recipient_execution(lost_reply_applies=True)
        original = self.fixture.current()
        with (
            patch.object(
                self.store,
                "list_physical_provider_target_records",
                return_value=[SimpleNamespace(provider_id="dokploy", target_id="retained-target")],
            ),
            self.assertRaises(ValueError),
        ):
            run(False)
        send.assert_not_called()
        self.assertNotIn("publish_v2", native.remote_modes)
        self.assertEqual(self.fixture.current(), original)

    def test_address_plan_reports_original_retained_target_hold_before_provider_reads(self) -> None:
        from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
        from control_plane.testing_address_qualification import (
            plan_testing_address,
            TestingAddressRecipientBusy,
        )
        from control_plane.dokploy import api

        origin = ProviderTargetOrigin(
            product=self.product,
            context=self.context,
            instance="testing",
            provider_id="dokploy",
            target_type="compose",
            target_id="retained-target",
        )
        held = self.store.reserve_mutation(
            scope="fixture-retained",
            route_path="fixture:retained-deploy",
            idempotency_key="original-retained-key",
            request_fingerprint=canonical_json_sha256({"retained": "original"}),
            lease_owner="fixture",
            reconciliation_key="original-retained-intent",
            provider_target_key=origin.target_key(),
            provider_target_origin=origin,
        ).record
        assert held is not None
        for reconcile in (False, True):
            if reconcile:
                self.store.mark_mutation_reconcile_required(
                    reservation=held, reconciliation_key=held.reconciliation_key
                )
                refreshed = self.store.read_idempotency_record(
                    scope=held.scope,
                    route_path=held.route_path,
                    idempotency_key=held.idempotency_key,
                )
                assert refreshed is not None
                held = refreshed
            with (
                self.subTest(reconcile=reconcile),
                patch.object(
                    api,
                    "dokploy_request",
                    side_effect=AssertionError("Held recipient cannot reach the provider"),
                ),
                self.assertRaises(TestingAddressRecipientBusy),
            ):
                plan_testing_address(
                    self.fixture.fixture.root,
                    self.store,
                    product=self.product,
                    context=self.context,
                    retirement_operation_id=self.operation.operation_id,
                    reservation_operation_id=self.operation.operation_id,
                    hostname=self.hostname,
                    reason=self.plan.reason,
                )
            self.assertEqual(
                self.store.list_held_provider_target_reservations(
                    provider_target_key=origin.target_key()
                ),
                (held,),
            )

    def test_lost_route_reply_recovers_original_attempt_before_canonical_publication(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        native, run = self.native_address_execution()
        original = self.fixture.current()
        native.before_publish = lambda: setattr(native, "lose_writer_reply", True)
        with self.assertRaises(NativeSourceUnavailable):
            run(False)
        self.assertEqual(self.fixture.current(), original)
        sends = native.remote_modes.count("publish_v2")
        native.before_publish = None
        native.lose_writer_reply = False
        self.unrelated_inventory_churning = True
        run(False)
        self.assertEqual(native.remote_modes.count("publish_v2"), sends)
        self.assertEqual(
            published_testing_copy(self.store, product=self.product, context=self.context),
            self.publication,
        )
        self.assertEqual(
            effective_testing_route(self.store, self.publication), self.plan.route_after
        )
        for router in json.loads(native.route_file.read_text())["http"]["routers"].values():
            if "tls" in router:
                self.assertEqual(
                    router["tls"]["domains"],
                    [{"main": self.plan.route_after.host}, {"main": self.plan.route_before.host}],
                )
        native.containers.pop("retired")
        from control_plane.testing_address import TestingAddressAdapter

        with patch(
            "control_plane.testing_rebuild_service.read_public_testing_health",
            side_effect=lambda base, _path, _identity: next(
                item for item in self.health if item.urls[0].startswith(base + "/")
            ),
        ):
            readback = TestingAddressAdapter(
                native.root, self.store, self.plan, "fixture-readback"
            ).readback()
        self.assertTrue(readback["applied"])

    def test_dispatch_requalifies_after_an_unsent_original_attempt(self) -> None:
        from control_plane.contracts.testing_address import ADDRESS_SCOPE
        from control_plane.testing_address import ADDRESS_EFFECT_ROUTE
        from control_plane.dokploy.testing_route import route_document

        native, run = self.native_address_execution()
        self.unrelated_inventory_churning = True
        with self.assertRaises(ValueError):
            run(False)
        self.assertIsNotNone(
            self.store.read_idempotency_record(
                scope=ADDRESS_SCOPE,
                route_path=ADDRESS_EFFECT_ROUTE,
                idempotency_key=self.plan.digest,
            )
        )
        self.assertEqual(
            json.loads(native.route_file.read_text()),
            route_document(self.plan.route_before, self.plan.route_before.new_backend),
        )
        with self.assertRaises(ValueError):
            run(False)
        self.unrelated_inventory_churning = False
        self.assertEqual(run(False).status, "completed")

    def test_original_key_cancellation_restores_route_after_profile_changes(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.dokploy.testing_route import route_document
        from control_plane.testing_address_storage import read_testing_address

        native, run = self.native_address_execution()
        self.health = (self.health[0].model_copy(update={"status": "pending"}), self.health[1])
        with self.assertRaises(NativeSourceUnavailable):
            run(False)
        self.assertIsNone(read_testing_address(self.store, self.plan.publication_key))
        profile = self.store.read_product_profile_record(self.product)
        changed = profile.model_copy(
            update={"updated_at": (self.now + timedelta(seconds=1)).isoformat()}
        )
        self.store.write_product_profile_record(changed)
        result = run(True)
        self.assertEqual(result.status, "completed")
        payload = result.response_payload["result"]
        assert isinstance(payload, dict)
        self.assertTrue(payload["cancelled"])
        self.assertEqual(
            json.loads(native.route_file.read_text()),
            route_document(self.plan.route_before, self.plan.route_before.new_backend),
        )
        self.assertEqual(self.store.read_product_profile_record(self.product), changed)
        self.assertIsNone(read_testing_address(self.store, self.plan.publication_key))

    def test_lost_inverse_reply_settles_before_cancellation_completes(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.testing_address_cancellation import INVERSE_ROUTE
        from control_plane.contracts.testing_address import ADDRESS_SCOPE

        native, run = self.native_address_execution()
        self.health = (self.health[0].model_copy(update={"status": "pending"}), self.health[1])
        with self.assertRaises(NativeSourceUnavailable):
            run(False)
        native.before_publish = lambda: setattr(native, "lose_writer_reply", True)
        with self.assertRaises(NativeSourceUnavailable):
            run(True)
        sends = native.remote_modes.count("publish_v2")
        with self.assertRaises(ValueError):
            run(False)
        from control_plane.testing_address_storage import read_testing_address

        self.assertIsNone(read_testing_address(self.store, self.plan.publication_key))
        native.before_publish = None
        native.lose_writer_reply = False
        result = run(True)
        self.assertEqual(result.status, "completed")
        self.assertEqual(native.remote_modes.count("publish_v2"), sends)
        inverse = self.store.read_idempotency_record(
            scope=ADDRESS_SCOPE, route_path=INVERSE_ROUTE, idempotency_key=self.plan.digest
        )
        assert inverse is not None
        self.assertEqual(inverse.state, "completed")

    def test_http_original_key_cancellation(self) -> None:
        if self.store.database_dialect_name != "postgresql":
            self.skipTest("The mutation HTTP boundary requires PostgreSQL fencing")
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
        from control_plane.testing_address import TestingAddressAdapter, ROUTE
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        native, _ = self.native_address_execution()
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="address-test-policy",
                revision=1,
                source="test:testing-address",
                updated_at=self.now.isoformat(),
                policy=LaunchplaneAuthzPolicy(),
            )
        )
        save_address_plan(self.store, self.plan)
        self.health = (self.health[0].model_copy(update={"status": "pending"}), self.health[1])
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=native.root,
        )
        body = {
            "context": self.context,
            "retirement_operation_id": self.operation.operation_id,
            "reservation_operation_id": self.operation.operation_id,
            "hostname": self.hostname,
            "reason": self.plan.reason,
            "mode": "apply",
            "reviewed_plan_sha256": self.plan.digest,
        }

        def post(**changes: object) -> Any:
            return asyncio.run(
                request(
                    app,
                    "POST",
                    ROUTE.format(product=self.product),
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "original-address-http",
                    },
                    payload={**body, **changes},
                )
            )

        with (
            patch.object(TestingAddressAdapter, "provider_guard"),
            patch(
                "control_plane.testing_rebuild_service.read_public_testing_health",
                side_effect=lambda base, _path, _identity: next(
                    item for item in self.health if item.urls[0].startswith(base + "/")
                ),
            ),
        ):
            response = post()
            self.assertEqual(response.status_code, 409, response.text)
            response = post(operation="cancel")
            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(response.json()["result"]["cancelled"])
            response = post(mode="readback", operation="cancel")
            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(response.json()["result"]["preimage_restored"])


class TestingAddressRecipientTests(unittest.TestCase):
    def test_matching_tcp_routes_are_refused_but_other_names_and_private_listeners_are_ignored(
        self,
    ) -> None:
        from control_plane.dokploy.testing_address_recipients import (
            qualify_testing_address_tcp_routes,
        )

        hostname = "canonical-testing.example.invalid"
        raw: dict[str, Any] = {
            "tcpRouters": {"other@file": {"rule": "HostSNI(`elsewhere.example.invalid`)"}}
        }
        qualify_testing_address_tcp_routes(raw, hostname, ("websecure",))
        raw["tcpRouters"]["other@file"] = {"rule": "HostSNI(`*`)", "entryPoints": ["database"]}
        qualify_testing_address_tcp_routes(raw, hostname, ("websecure",))
        raw["tcpRouters"]["other@file"]["entryPoints"] = ["websecure"]
        with self.assertRaises(ValueError):
            qualify_testing_address_tcp_routes(raw, hostname, ("websecure",))

    def test_domain_census_refuses_another_recipient_and_preserves_denials(self) -> None:
        from control_plane import testing_address_qualification as qualification
        from control_plane.dokploy_inventory import InventoryPage
        from control_plane.dokploy import api
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

        hostname = "canonical-testing.example.invalid"
        targets = [
            {
                "target_id": name,
                "category": "compose",
                "name": name,
                "placement": {
                    "kind": "server",
                    "server_id": "testing-server",
                    "server_name": "Testing",
                },
            }
            for name in ("retained", "current", "foreign")
        ]
        page = InventoryPage.model_validate(
            {
                "inventory_digest": "fixture-membership",
                "total_projects": 1,
                "total_environments": 1,
                "total_targets": len(targets),
                "offset": 0,
                "limit": 1,
                "next_offset": None,
                "projects": [
                    {
                        "project_id": "project",
                        "name": "Testing",
                        "environments": [
                            {"environment_id": "environment", "name": "Testing", "targets": targets}
                        ],
                    }
                ],
            }
        )
        payloads: dict[str, Any] = {
            name: {
                "composeId": name,
                "serverId": "testing-server",
                "domains": [{"domainId": "original-domain", "host": hostname}]
                if name == "retained"
                else [],
                "autoDeploy": False,
                "appName": name + "-project",
            }
            for name in ("retained", "current", "foreign")
        }
        arguments: dict[str, Any] = {
            "host": "https://provider.invalid",
            "token": "fixture",
            "hostname": hostname,
            "retained_ids": frozenset({"retained"}),
            "server_id": "testing-server",
        }
        with (
            patch.object(qualification, "read_dokploy_inventory", return_value=page) as inventory,
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                side_effect=lambda **kwargs: payloads[kwargs["target_id"]],
            ),
            patch.object(api, "list_dokploy_schedules", return_value=()) as schedules,
        ):
            digest, projects = qualification.qualify_address_domains(**arguments)
            self.assertTrue(digest)
            self.assertEqual(projects, ("retained-project",))
            page.inventory_digest = "unrelated-preview-membership"
            page.projects[0].name = "Renamed unrelated project metadata"
            self.assertEqual(qualification.qualify_address_domains(**arguments), (digest, projects))
            payloads["retained"]["autoDeploy"] = True
            preparation = qualification.inspect_address_domains(**arguments)
            self.assertEqual(preparation.digest, digest)
            self.assertEqual(preparation.projects, projects)
            self.assertEqual(preparation.automation, {"retained": True})
            for flag, rows, expected_state in (
                (True, (), "enabled"),
                (None, ({"scheduleId": "job"},), "unknown"),
                (False, ({"scheduleId": "job"},), "disabled"),
            ):
                with self.subTest(flag=flag, rows=rows):
                    payloads["retained"]["autoDeploy"] = flag
                    schedules.return_value = rows
                    schedules.reset_mock()
                    with self.assertRaises(
                        qualification.TestingAddressRecipientUnverified
                    ) as caught:
                        qualification.qualify_address_domains(**arguments)
                    schedules.assert_called_once()
                    self.assertEqual(caught.exception.auto_deploy, expected_state)
                    self.assertEqual(caught.exception.schedule_count, len(rows))
                    if flag is not True or rows:
                        with self.assertRaises(qualification.TestingAddressRecipientUnverified):
                            qualification.inspect_address_domains(**arguments)
            payloads["retained"]["autoDeploy"] = False
            schedules.return_value = ()
            payloads["foreign"]["domains"] = [{"domainId": "foreign-domain", "host": hostname}]
            with self.assertRaises(ValueError):
                qualification.qualify_address_domains(**arguments)
            inventory.side_effect = api.DokployRequestFailed(
                method="GET", path="/api/project.all", detail="fixture denied", status_code=403
            )
            with self.assertRaises(NativeSourceAuthorizationDenied):
                qualification.qualify_address_domains(**arguments)

    def test_retained_recipients_need_stopped_processes_and_strict_route_priority(self) -> None:
        hostname = "canonical-testing.example.invalid"
        spec = {
            "hostname": hostname,
            "projects": ["retained"],
            "entrypoints": ["websecure"],
            "priority": 100,
        }
        container: dict[str, Any] = {
            "Id": "retained-web",
            "State": {"Status": "exited"},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "retained",
                    "traefik.http.routers.old.rule": f"Host(`{hostname}`)",
                    "traefik.http.routers.old.entrypoints": "websecure",
                }
            },
        }
        self.assertTrue(qualify_testing_address_recipients([container], spec))
        container["State"]["Status"] = "running"
        with self.assertRaises(ValueError):
            qualify_testing_address_recipients([container], spec)
        container["State"]["Status"] = "exited"
        container["HostConfig"]["RestartPolicy"]["Name"] = "always"
        with self.assertRaises(ValueError):
            qualify_testing_address_recipients([container], spec)
        container["HostConfig"]["RestartPolicy"]["Name"] = "no"
        container["Config"]["Labels"]["traefik.http.routers.old.priority"] = "100"
        with self.assertRaises(ValueError):
            qualify_testing_address_recipients([container], spec)
