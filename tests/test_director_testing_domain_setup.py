from copy import deepcopy
from typing import Any
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.dokploy_target_setup_http import CREATE_COMPOSE_SOURCE_LABEL
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import record_safety
from tests.test_director_testing_target_creation import _DirectorTestingSetupFixture


class DirectorTestingDomainSetupTests(_DirectorTestingSetupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.register_testing_lane()
        self.target = DokployTargetRecord(
            context=self.context,
            instance="testing",
            target_type="compose",
            source_type="raw",
            updated_at=self.profile.updated_at,
        )
        self.target_id = DokployTargetIdRecord(
            context=self.context,
            instance="testing",
            target_id="empty-testing-compose",
            source_label=CREATE_COMPOSE_SOURCE_LABEL,
            updated_at=self.profile.updated_at,
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=self.target, target_id_record=self.target_id
            )
        )
        self.live: dict[str, Any] = {
            "composeId": self.target_id.target_id,
            "environmentId": "empty-environment",
            "serverId": "testing-server",
            "appName": "empty-testing",
            "name": "Empty testing",
            "sourceType": "raw",
            "composeType": "docker-compose",
            "composeStatus": "idle",
            "isolatedDeployment": False,
            "randomize": False,
            "serviceNetworks": [],
            "mounts": [],
            "domains": [],
            "autoDeploy": True,
            "env": "",
        }
        self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=lambda *_: deepcopy(self.live),
            )
        )
        self.routes: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.inventory = self.enterContext(
            patch("control_plane.dokploy.api.dokploy_request", side_effect=self.provider_request)
        )
        self.payload = {
            "mode": "apply",
            "operation": "reconcile-compose-domain",
            "context": self.context,
            "instance": "testing",
            "target_id": self.target_id.target_id,
            "domains": ["new-testing.example.invalid"],
            "runtime_port": self.profile.runtime_port,
            "reason": "Add the first testing hostname",
            "confirmation": "APPLY DOKPLOY TARGET SETUP",
        }

    def provider_request(self, **kwargs: Any) -> Any:
        if kwargs.get("method") == "POST":
            self.assertEqual(kwargs["path"], "/api/domain.create")
            self.writes.append(deepcopy(kwargs["payload"]))
            route = {"domainId": "created-domain", **kwargs["payload"]}
            self.routes.append(route)
            self.live["domains"] = deepcopy(self.routes)
            return route
        if kwargs["path"] == "/api/domain.byComposeId":
            return deepcopy(self.routes)
        return []

    async def test_director_adds_first_hostname_without_deploying_or_changing_production(
        self,
    ) -> None:
        dry = await self.post(mode="dry-run")
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.writes, [])
        applied = await self.post()
        self.assertEqual(applied.status_code, 202, applied.text)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0]["composeId"], self.target_id.target_id)
        self.assertEqual(self.writes[0]["host"], self.payload["domains"][0])
        self.assertEqual(self.writes[0]["port"], self.profile.runtime_port)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).domains,
            tuple(self.payload["domains"]),
        )
        self.assertEqual(self.store.list_deployment_records(), ())
        self.assert_production_unchanged()

    async def test_scoped_agent_production_and_other_target_stay_refused(self) -> None:
        with scoped_agent_transport():
            refused = await self.post()
        self.assertEqual(refused.status_code, 403, refused.text)
        for changes in ({"instance": "prod"}, {"target_id": "other-target"}, {"target_id": ""}):
            with self.subTest(changes=changes):
                response = await self.post(**changes)
                self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertEqual(self.writes, [])

    async def test_positive_protection_and_holds_prevent_hostname_effects(self) -> None:
        for changes in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
        ):
            with self.subTest(changes=changes):
                record_safety(self.store, self.profile.product, self.context, "testing", **changes)
                response = await self.post()
                self.assertGreaterEqual(response.status_code, 400, response.text)
                self.assertEqual(self.writes, [])

    async def test_nonempty_placement_and_provider_source_prevent_hostname_effects(self) -> None:
        baseline = deepcopy(self.live)
        for changes in (
            {"mounts": [{"type": "bind"}]},
            {"composeStatus": "running"},
            {"composeFile": "services: {}"},
            {"env": "PRIVATE_KEY=value"},
            {"serviceNetworks": [{"networkId": "shared"}]},
            {"customGitUrl": "https://example.invalid/source.git"},
            {"domains": [{"host": "existing.example.invalid"}]},
        ):
            with self.subTest(changes=changes):
                self.live = {**baseline, **changes}
                response = await self.post()
                self.assertGreaterEqual(response.status_code, 400, response.text)
                self.assertEqual(self.writes, [])

    async def test_existing_or_partial_routes_never_get_rewritten(self) -> None:
        self.routes = [{"domainId": "existing", "host": self.payload["domains"][0]}]
        response = await self.post()
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertEqual(self.writes, [])

    async def test_conflicting_hostname_port_or_extra_target_inputs_are_refused(self) -> None:
        for changes in (
            {"domains": ["example.invalid", "other.example.invalid"]},
            {"domains": ["*.example.invalid"]},
            {"runtime_port": self.profile.runtime_port + 1},
            {"project_id": "existing-project"},
            {"domains": ["production.example.invalid"]},
        ):
            with self.subTest(changes=changes):
                self.store.write_dokploy_target_record(
                    self.production.model_copy(update={"domains": ("production.example.invalid",)})
                )
                response = await self.post(**changes)
                self.assertGreaterEqual(response.status_code, 400, response.text)
                self.assertEqual(self.writes, [])

    async def test_unknown_or_active_provider_work_prevents_hostname_effects(self) -> None:
        original = self.provider_request
        for path, rows in (
            ("/api/deployment.allByType", [{"status": "done"}]),
            ("/api/deployment.queueList", {"items": [], "truncated": True}),
            ("/api/schedule.list", [{"enabled": False}]),
        ):
            with self.subTest(path=path):
                self.inventory.side_effect = lambda **kwargs: (
                    rows if kwargs["path"] == path else original(**kwargs)
                )
                response = await self.post()
                self.assertGreaterEqual(response.status_code, 400, response.text)
                self.assertEqual(self.writes, [])

    async def test_odoo_profile_uses_the_same_empty_compose_continuation(self) -> None:
        self.profile = self.profile.model_copy(update={"driver_id": "odoo"})
        self.store.write_product_profile_record(self.profile)
        applied = await self.post()
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(len(self.writes), 1)
        self.assert_production_unchanged()

    async def test_staff_and_isolation_holds_remain_protected(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        for purpose in ("staff_testing", "isolation_staging"):
            self.store.write_dokploy_target_record(
                self.target.model_copy(
                    update={
                        "policies": self.target.policies.model_copy(
                            update={
                                "staff_testing_hold": DokployTargetStaffTestingHold(
                                    reason="Fixture hold", purpose=purpose
                                )
                            }
                        )
                    }
                )
            )
            refused = await self.post()
            self.assertEqual(refused.status_code, 400, refused.text)
            self.assertEqual(self.writes, [])

    async def test_safety_change_during_provider_inventory_prevents_effect(self) -> None:
        original = self.provider_request

        def provider(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/schedule.list":
                record_safety(
                    self.store, self.profile.product, self.context, "testing", data="only_copy"
                )
            return original(**kwargs)

        self.inventory.side_effect = provider
        refused = await self.post()
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(self.writes, [])

    async def test_lost_provider_response_keeps_records_and_prevents_duplicate_effects(
        self,
    ) -> None:
        original = self.provider_request

        def provider(**kwargs: Any) -> Any:
            result = original(**kwargs)
            if kwargs.get("method") == "POST":
                raise RuntimeError("Lost response after route creation")
            return result

        self.inventory.side_effect = provider
        response = await self.post()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["code"], "dokploy_domain_partial_outcome")
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            self.target,
        )
        for key in ("first-testing-setup", "new-key"):
            refused = await self.post(idempotency_key=key)
            self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(len(self.writes), 1)

    async def test_active_testing_executor_prevents_hostname_effects(self) -> None:
        from contextlib import nullcontext

        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.writes, [])
