import asyncio
import json
import os
import unittest
from typing import Any
from unittest.mock import Mock, patch

from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy import api
from control_plane.dokploy.testing_route import route_document
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.storage import postgres as db
from control_plane.testing_address_storage import effective_testing_route
from control_plane.testing_copy_route import publish_testing_copy_route
from control_plane.testing_lane_aliases import (
    ROUTE,
    TestingLaneAliasesPlan,
    commit_lane_aliases,
    plan_lane_aliases,
    read_lane_aliases_plan,
    save_lane_aliases_plan,
)
from control_plane.testing_route_effects import TestingRouteFenceController
from control_plane.testing_serving_copy import published_testing_copy
from tests import test_testing_publication as publication_tests
from tests import test_testing_route_transport as transport_tests
from tests.test_testing_rebuild_service import (
    _NATIVE_FENCE_ENSURE,
    _NATIVE_ROUTE_IO,
    exercise_published_copy_maintenance,
)

NAMES = ("agent-one-testing.example.invalid", "agent-two-testing.example.invalid")


class TestingLaneAliasesTests(unittest.TestCase):
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

    def plan(self, names: tuple[str, ...]) -> TestingLaneAliasesPlan:
        plan = plan_lane_aliases(
            self.store,
            product=self.product,
            context=self.context,
            hostnames=names,
            reason="Agent mockup addresses",
        )
        save_lane_aliases_plan(self.store, plan)
        self.assertEqual(read_lane_aliases_plan(self.store, plan.digest), plan)
        return plan

    def test_extra_names_join_the_route_and_survive_the_next_build(self) -> None:
        original = effective_testing_route(self.store, self.publication)
        plan = self.plan(NAMES)
        self.assertEqual(plan.route_after.host, original.host)
        self.assertEqual(plan.route_after.new_backend, original.new_backend)
        commit_lane_aliases(self.store, plan)
        commit_lane_aliases(self.store, plan)

        self.assertEqual(
            published_testing_copy(self.store, product=self.product, context=self.context),
            self.publication,
        )
        self.assertEqual(effective_testing_route(self.store, self.publication).aliases, NAMES)
        exercise_published_copy_maintenance(self, self.fixture)
        self.assertEqual(effective_testing_route(self.store, self.publication).aliases, NAMES)

    def test_empty_list_restores_the_original_route(self) -> None:
        original = effective_testing_route(self.store, self.publication)
        binding = self.fixture.current().binding
        commit_lane_aliases(self.store, self.plan(NAMES))

        removal = self.plan(())
        self.assertEqual(removal.route_after, original)
        commit_lane_aliases(self.store, removal)

        self.assertEqual(effective_testing_route(self.store, self.publication), original)
        current = self.fixture.current().binding
        assert binding is not None and current is not None
        self.assertEqual(current.source.source_versions, binding.source.source_versions)

    def test_refuses_the_canonical_host_and_an_unchanged_route(self) -> None:
        host = effective_testing_route(self.store, self.publication).host
        with self.assertRaises(ValueError):
            self.plan((host,))
        with self.assertRaises(ValueError):
            self.plan(())
        commit_lane_aliases(self.store, self.plan(NAMES))
        with self.assertRaises(ValueError):
            self.plan(NAMES)

    def test_a_changed_binding_refuses_the_reviewed_commit(self) -> None:
        stale = self.plan(NAMES[:1])
        commit_lane_aliases(self.store, self.plan(NAMES))
        with self.assertRaises(ValueError):
            commit_lane_aliases(self.store, stale)
        self.assertEqual(effective_testing_route(self.store, self.publication).aliases, NAMES)

    def publish(self, plan: TestingLaneAliasesPlan, foreign: bool = False) -> object:
        candidate = self.fixture.candidate
        provider = self.fixture.fixture.provider_state
        native = transport_tests.TestingRouteTransportTests()
        native.setUp(initialize_fence=False)
        self.addCleanup(native.doCleanups)
        native.plan = plan.route_current
        native.route_file = native.root / (native.plan.route_id + ".yml")
        native.route_file.write_text(
            json.dumps(route_document(native.plan, native.plan.new_backend))
        )
        native.raw = {"routers": {}, "services": {}, "middlewares": {}}
        native.apply_document()
        if foreign:
            native.raw["routers"]["foreign@docker"] = {
                "rule": "Host(`" + NAMES[0] + "`)",
                "service": "foreign@docker",
                "entryPoints": list(plan.route_current.public_entrypoints),
                "status": "enabled",
            }
            native.raw["services"]["foreign@docker"] = {"status": "enabled"}
        web = native.containers["new"]
        web["Config"]["Labels"]["com.docker.compose.project"] = provider["appName"]
        web["Name"] = "/" + str(provider["appName"]) + "-web-1"
        web["NetworkSettings"]["Networks"]["dokploy-network"]["Aliases"] = [candidate.namespace]
        snapshot = self.fixture.current()
        assert snapshot.inventory is not None and snapshot.inventory.runtime_identity is not None
        identity = snapshot.inventory.runtime_identity
        web["Config"]["Env"] = [
            "PLATFORM_INSTANCE=testing",
            "ODOO_WORKERS=0",
            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + identity.model_dump_json(),
        ]
        with (
            patch.multiple(DokployTestingRouteTransport, **_NATIVE_ROUTE_IO),
            patch.object(TestingRouteFenceController, "ensure", _NATIVE_FENCE_ENSURE),
            patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": self.store.database_url}),
            patch.object(api, "fetch_dokploy_target_payload", return_value=dict(provider)),
            patch.object(
                dokploy_source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
            ),
        ):
            publish_testing_copy_route(
                root=self.fixture.fixture.root,
                store=self.store,
                identity=identity,
                checkpoint=Mock(),
                operation_id="testing-aliases:" + plan.digest,
                lease_owner="fixture-aliases",
                route=plan.route_after,
                preimage=plan.route_current,
            )
        return json.loads(native.route_file.read_text())

    def test_reviewed_route_is_published_over_the_served_preimage(self) -> None:
        plan = self.plan(NAMES)
        self.assertEqual(
            self.publish(plan), route_document(plan.route_after, plan.route_after.new_backend)
        )
        commit_lane_aliases(self.store, plan)
        self.assertEqual(effective_testing_route(self.store, self.publication), plan.route_after)

    def test_a_name_another_router_serves_is_refused_before_the_write(self) -> None:
        plan = self.plan(NAMES)
        with self.assertRaises(ValueError):
            self.publish(plan, foreign=True)

    def test_http_writer_is_director_only_and_applies_only_the_reviewed_names(self) -> None:
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.auth import scoped_agent_transport
        from tests.support.http import request

        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.fixture.root,
        )
        body = {"context": self.context, "hostnames": list(NAMES), "reason": "Agent mockups"}

        def post(**changes: object) -> Any:
            return asyncio.run(
                request(
                    app,
                    "POST",
                    ROUTE.format(product=self.product),
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "aliases-key",
                    },
                    payload={**body, **changes},
                )
            )

        response = post()
        self.assertEqual(response.status_code, 202, response.text)
        digest = response.json()["result"]["plan_sha256"]
        self.assertEqual(response.json()["result"]["hostnames_after"], list(NAMES))
        before = self.fixture.current().binding
        response = post(mode="apply", reviewed_plan_sha256=digest, hostnames=list(NAMES[:1]))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.fixture.current().binding, before)
        with scoped_agent_transport():
            response = post()
        self.assertEqual(response.status_code, 403, response.text)

    def test_removal_is_published_over_the_served_names(self) -> None:
        added = self.plan(NAMES)
        self.publish(added)
        commit_lane_aliases(self.store, added)
        removal = self.plan(())
        self.assertEqual(
            self.publish(removal),
            route_document(removal.route_after, removal.route_after.new_backend),
        )
        commit_lane_aliases(self.store, removal)
        self.assertEqual(effective_testing_route(self.store, self.publication).aliases, ())

    def test_a_hold_placed_after_review_refuses_the_commit(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        plan = self.plan(NAMES)
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="Staff trial"
                            )
                        }
                    )
                }
            )
        )
        with self.assertRaises(ValueError):
            commit_lane_aliases(self.store, plan)
        self.assertEqual(effective_testing_route(self.store, self.publication).aliases, ())
