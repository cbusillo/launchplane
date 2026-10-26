from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from httpx2 import Response

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.route_binding_reconcile import (
    plan_route_binding_reconcile,
    route_binding_authority_projection,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests.test_http_app_product import _product_health_monitoring_profile
from tests.test_route_binding_record import (
    _dokploy_target_id_record,
    _dokploy_target_record,
    _edge_endpoint_record,
    _ingress_audit_record,
    _provider_target_record,
    _route_binding_reconcile_payload,
    _route_binding_reconcile_request,
)


class RouteEvidenceRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory) / 'db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        profile = _product_health_monitoring_profile().model_dump(mode="json")
        profile.update(product="example-product", production_use="live")
        profile["lanes"] = [
            {**profile["lanes"][0], "context": "example-testing", "instance": "prod"}
        ]
        self.profile = LaunchplaneProductProfileRecord.model_validate(profile)
        self.store.write_product_profile_record(self.profile)
        self.store.write_provider_target_record(_provider_target_record(instance="prod"))
        self.store.write_dokploy_target_record(_dokploy_target_record(instance="prod"))
        self.store.write_dokploy_target_id_record(_dokploy_target_id_record(instance="prod"))
        self.store.write_edge_endpoint_record(_edge_endpoint_record())
        self.store.write_ingress_route_audit_record(_ingress_audit_record())
        plan_request = _route_binding_reconcile_request(evaluated_at="2026-07-19T00:00:00Z")
        plan = plan_route_binding_reconcile(
            record_store=self.store, request=plan_request.model_copy(update={"instance": "prod"})
        )
        assert plan.record is not None
        self.current = plan.record
        self.store.write_route_binding_record(self.current)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(schema_version=2),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
        )

    async def apply(
        self, payload: dict[str, Any] | None = None, *, key: str = "original-refresh-key"
    ) -> Response:
        with patch("control_plane.http_app.utc_now_timestamp", return_value="2026-07-20T00:00:00Z"):
            return await request(
                self.app,
                "POST",
                "/v1/route-bindings/reconcile",
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": key,
                },
                payload=payload
                or _route_binding_reconcile_payload(current_record=self.current, instance="prod"),
            )

    def read_binding(self) -> EnvironmentRouteBindingRecord:
        return self.store.read_route_binding_record(
            product=self.current.product,
            context_name=self.current.context,
            instance_name=self.current.instance,
        )

    async def test_director_refreshes_live_evidence_without_apply_grant_and_replays(self) -> None:
        response = await self.apply()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["records"]["route_binding_status"], "refreshed")
        refreshed = self.read_binding()
        self.assertNotEqual(refreshed.source, self.current.source)
        self.assertEqual(
            route_binding_authority_projection(refreshed),
            route_binding_authority_projection(self.current),
        )
        replay = await self.apply()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.read_binding(), refreshed)
        self.current = refreshed
        unchanged = await self.apply(key="unchanged-evidence")
        self.assertEqual(unchanged.status_code, 202, unchanged.text)
        self.assertEqual(unchanged.json()["records"]["route_binding_status"], "unchanged")
        self.assertEqual(self.read_binding(), refreshed)

    async def test_scoped_agent_cannot_inherit_director_refresh_admission(self) -> None:
        with scoped_agent_transport():
            response = await self.apply()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.read_binding(), self.current)

    async def test_director_cannot_create_or_change_route_authority(self) -> None:
        absent = await self.apply(_route_binding_reconcile_payload(instance="prod"))
        self.assertEqual(absent.status_code, 403, absent.text)
        original = self.current
        for field, value in (
            (
                "provider_target",
                self.current.provider_target.model_copy(update={"target_name": "other"}),
            ),
            (
                "domains",
                (self.current.domains[0].model_copy(update={"domain_name": "other.example.test"}),),
            ),
            ("ingress", self.current.ingress.model_copy(update={"endpoint_key": "other"})),
            ("tls", self.current.tls.model_copy(update={"owner": "external"})),
            ("source", self.current.source.model_copy(update={"source_kind": "operator"})),
            ("status", "disabled"),
        ):
            with self.subTest(field=field):
                self.current = original.model_copy(update={field: value})
                self.store.write_route_binding_record(self.current)
                response = await self.apply()
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(self.read_binding(), self.current)
        self.current = original.model_copy(
            update={
                "status": "disabled",
                "source": original.source.model_copy(update={"source_kind": "operator"}),
                "ingress": original.ingress.model_copy(update={"provider": "external"}),
                "tls": original.tls.model_copy(update={"owner": "external"}),
            }
        )
        self.store.write_route_binding_record(self.current)
        handoff = await self.apply()
        self.assertEqual(handoff.status_code, 403, handoff.text)
        self.assertEqual(self.read_binding(), self.current)

    async def test_commit_rechecks_sources_and_product_ownership(self) -> None:
        original_commit = self.store.reconcile_route_binding_record

        def change_source(**kwargs: Any) -> Any:
            self.store.write_dokploy_target_record(
                _dokploy_target_record(instance="prod", domains=("changed.example.test",))
            )
            return original_commit(**kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=change_source):
            response = await self.apply()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.read_binding(), self.current)
        self.store.write_dokploy_target_record(_dokploy_target_record(instance="prod"))

        def remove_lane(**kwargs: Any) -> Any:
            self.store.write_product_profile_record(self.profile.model_copy(update={"lanes": ()}))
            return original_commit(**kwargs)

        with patch.object(self.store, "reconcile_route_binding_record", side_effect=remove_lane):
            response = await self.apply()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.read_binding(), self.current)
