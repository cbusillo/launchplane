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

    async def test_conflicting_or_partial_routes_never_get_rewritten(self) -> None:
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
            (
                "/api/deployment.allByType",
                [
                    {
                        "composeId": self.target_id.target_id,
                        "deploymentId": "finished",
                        "status": "done",
                        "startedAt": self.profile.updated_at,
                        "finishedAt": self.profile.updated_at,
                    }
                ],
            ),
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
        read_safety = self.store.read_disposable_lane_safety
        record_safety(self.store, self.profile.product, self.context, "testing")
        inspected = False

        def provider(**kwargs: Any) -> Any:
            nonlocal inspected
            if kwargs["path"] == "/api/schedule.list":
                inspected = True
            return original(**kwargs)

        def safety(*args: Any, **kwargs: Any) -> Any:
            observed = read_safety(*args, **kwargs)
            if observed is not None and inspected:
                return observed.model_copy(update={"data": "only_copy"})
            return observed

        self.inventory.side_effect = provider
        with patch.object(self.store, "read_disposable_lane_safety", side_effect=safety):
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
        recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).domains,
            tuple(self.payload["domains"]),
        )
        self.assertEqual(len(self.writes), 1)

    async def test_active_testing_executor_prevents_hostname_effects(self) -> None:
        from contextlib import nullcontext

        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.writes, [])

    async def test_established_testing_domain_dry_run_remains_available(self) -> None:
        self.store.write_dokploy_target_record(
            self.target.model_copy(update={"domains": ("old.example.invalid",)})
        )
        planned = await self.post(mode="dry-run")
        self.assertEqual(planned.status_code, 202, planned.text)
        self.assertEqual(self.writes, [])

    async def test_profile_hostname_claim_prevents_effect_without_a_target_claim(self) -> None:
        self.profile = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"base_url": "https://" + self.payload["domains"][0]})
                    if lane.instance == "prod"
                    else lane
                    for lane in self.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        refused = await self.post()
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("ingress authority", refused.json()["error"]["message"])
        self.assertEqual(self.writes, [])

    async def test_route_binding_hostname_claim_prevents_effect_without_a_target_claim(
        self,
    ) -> None:
        from tests.test_route_binding_record import _route_binding_record

        self.store.write_route_binding_record(
            _route_binding_record(domain=self.payload["domains"][0])
        )
        refused = await self.post()
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("ingress authority", refused.json()["error"]["message"])
        self.assertEqual(self.writes, [])

    async def test_terminal_agent_uses_the_bounded_continuation(self) -> None:
        from tests.http_app_test_support import _local_operator_bearer_config

        config = _local_operator_bearer_config().model_copy(
            update={
                "local_operator_token": "",
                "terminal_agent_token": "local-operator-token",
                "terminal_agent_subject": "fixture-director",
                "terminal_agent_token_label": "fixture-terminal",
            }
        )
        self.app = self.make_app(config)
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.writes), 1)

    async def test_existing_service_scoped_operator_keeps_general_reconciliation(self) -> None:
        from tests.http_app_test_support import _local_operator_bearer_config
        from tests.support.auth import local_operator_policy

        self.app = self.make_app(
            _local_operator_bearer_config(),
            local_operator_policy(
                actions=("dokploy_target.setup",),
                token_label="local-owner-read",
            ),
        )
        self.store.write_dokploy_target_record(
            self.target.model_copy(update={"domains": ("old.example.invalid",)})
        )
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.writes), 1)
        self.assertIn(
            "old.example.invalid",
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).domains,
        )

    async def test_id_only_alias_prevents_hostname_effects(self) -> None:
        self.store.write_dokploy_target_id_record(
            self.target_id.model_copy(update={"context": "other", "instance": "prod"})
        )
        refused = await self.post()
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.writes, [])

    async def test_near_matching_customized_routes_are_never_adopted(self) -> None:
        from control_plane.dokploy.compose import compose_web_domain_route_payload

        baseline = {
            "domainId": "existing",
            **compose_web_domain_route_payload(
                compose_id=self.target_id.target_id,
                domain_host=self.payload["domains"][0],
                runtime_port=self.profile.runtime_port,
            ),
        }
        for changes in ({"port": self.profile.runtime_port + 1}, {"middlewares": ["foreign"]}):
            self.routes = [{**baseline, **changes}]
            self.live["domains"] = deepcopy(self.routes)
            refused = await self.post()
            self.assertEqual(refused.status_code, 400, refused.text)
            self.assertEqual(self.writes, [])
            self.assertEqual(
                self.store.read_dokploy_target_record(
                    context_name=self.context, instance_name="testing"
                ),
                self.target,
            )

    async def test_inventory_transport_failure_refuses_before_hostname_effects(self) -> None:
        original = self.provider_request

        def provider(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/deployment.allByType":
                raise OSError("Fixture connection reset")
            return original(**kwargs)

        self.inventory.side_effect = provider
        refused = await self.post()
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(self.writes, [])

    async def test_record_only_commit_failure_preserves_domain_recovery(self) -> None:
        from control_plane.dokploy.compose import compose_web_domain_route_payload
        from control_plane.dokploy.target_source_setup import DokployComposeSourcePartialError

        self.routes = [
            {
                "domainId": "existing",
                **compose_web_domain_route_payload(
                    compose_id=self.target_id.target_id,
                    domain_host=self.payload["domains"][0],
                    runtime_port=self.profile.runtime_port,
                ),
            }
        ]
        self.live["domains"] = deepcopy(self.routes)

        def fail_commit(**kwargs: Any) -> None:
            kwargs["apply_provider"]()
            raise DokployComposeSourcePartialError("Fixture record commit failed")

        with patch.object(self.store, "complete_dokploy_compose_setup", side_effect=fail_commit):
            failed = await self.post()
        self.assertEqual(failed.status_code, 502, failed.text)
        self.assertEqual(failed.json()["error"]["code"], "dokploy_domain_partial_outcome")
        recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(self.writes, [])

    async def test_missing_receipt_recovers_the_already_recorded_exact_hostname(self) -> None:
        with patch.object(
            self.store,
            "write_idempotency_record",
            side_effect=RuntimeError("Fixture lost receipt storage"),
        ):
            with self.assertRaises(RuntimeError):
                await self.post()
        self.assertEqual(len(self.writes), 1)
        changed = await self.post(domains=["different.example.invalid"])
        self.assertEqual(changed.status_code, 400, changed.text)
        recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(len(self.writes), 1)
