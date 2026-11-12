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


class _EmptyComposeDomainFixture(_DirectorTestingSetupFixture):
    def install_empty_compose(self, instance: str, target_id: str) -> None:
        self.target = DokployTargetRecord(
            context=self.context,
            instance=instance,
            target_type="compose",
            source_type="raw",
            updated_at=self.profile.updated_at,
        )
        self.target_id = DokployTargetIdRecord(
            context=self.context,
            instance=instance,
            target_id=target_id,
            source_label=CREATE_COMPOSE_SOURCE_LABEL,
            updated_at=self.profile.updated_at,
        )
        self.target_provider = ProviderTargetRecord.from_dokploy_records(
            target_record=self.target, target_id_record=self.target_id
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(self.target_provider)
        self.live: dict[str, Any] = {
            "composeId": self.target_id.target_id,
            "environmentId": "empty-environment",
            "serverId": f"{instance}-server",
            "appName": f"empty-{instance}",
            "name": f"Empty {instance}",
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

    def provider_request(self, **kwargs: Any) -> Any:
        if kwargs.get("method") == "POST":
            self.assertEqual(kwargs["path"], "/api/domain.create")
            self.writes.append(deepcopy(kwargs["payload"]))
            route = {"domainId": f"created-{kwargs['payload']['host']}", **kwargs["payload"]}
            self.routes.append(route)
            self.live["domains"] = deepcopy(self.routes)
            return route
        if kwargs["path"] == "/api/domain.byComposeId":
            return deepcopy(self.routes)
        return []

    async def plan_apply_and_replay(self) -> None:
        planned = await self.post(mode="dry-run")
        self.assertEqual(planned.status_code, 202, planned.text)
        self.assertEqual(self.writes, [])
        applied = await self.post()
        self.assertEqual(applied.status_code, 202, applied.text)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)

    def read_target(self) -> DokployTargetRecord:
        return self.store.read_dokploy_target_record(
            context_name=self.context, instance_name=self.target.instance
        )


class DirectorTestingDomainSetupTests(_EmptyComposeDomainFixture):
    def setUp(self) -> None:
        super().setUp()
        self.register_testing_lane()
        self.install_empty_compose("testing", "empty-testing-compose")
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

    async def test_director_adds_first_hostname_without_deploying_or_changing_production(
        self,
    ) -> None:
        await self.plan_apply_and_replay()
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


class _NeverLiveProductionDomainFixture(_EmptyComposeDomainFixture):
    def setUp(self) -> None:
        super().setUp()
        self.profile = self.profile.model_copy(update={"production_use": "unknown"})
        self.store.write_product_profile_record(self.profile)
        self.install_empty_compose("prod", "empty-production-compose")
        self.payload = {
            "mode": "apply",
            "operation": "reconcile-compose-domain",
            "context": self.context,
            "instance": "prod",
            "target_id": self.target_id.target_id,
            "domains": ["new.example.invalid", "example.invalid"],
            "runtime_port": self.profile.runtime_port,
            "reason": "Add the reviewed backend routes before the first release",
            "confirmation": "APPLY DOKPLOY TARGET SETUP",
        }

    def assert_no_effects(self, response: Any, status_code: int) -> None:
        self.assertEqual(response.status_code, status_code, response.text)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.read_target(), self.target)

    def assert_refused_before_provider(self, response: Any) -> None:
        self.assert_no_effects(response, 403)
        self.inventory.assert_not_called()

    def expected_routes(self) -> list[dict[str, Any]]:
        from control_plane.dokploy.compose import compose_web_domain_route_payload

        return [
            compose_web_domain_route_payload(
                compose_id=self.target_id.target_id,
                domain_host=domain,
                runtime_port=self.profile.runtime_port,
            )
            for domain in self.payload["domains"]
        ]


class DirectorNeverLiveProductionDomainSetupTests(_NeverLiveProductionDomainFixture):
    async def test_owned_never_live_production_gets_only_its_backend_routes(self) -> None:
        await self.plan_apply_and_replay()
        self.assertEqual(self.writes, self.expected_routes())
        recorded = self.read_target()
        self.assertEqual(recorded.domains, tuple(self.payload["domains"]))
        self.assertEqual(
            recorded.model_dump(exclude={"domains", "updated_at", "source_label"}),
            self.target.model_dump(exclude={"domains", "updated_at", "source_label"}),
        )
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        self.assertEqual(
            self.store.read_provider_target_record(context_name=self.context, instance_name="prod"),
            self.target_provider,
        )
        self.assertEqual(self.store.list_deployment_records(), ())
        self.assertEqual(self.store.list_promotion_records(), ())

    async def test_refused_original_key_continues_only_with_the_same_request(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"production_use": "live"})
        )
        refused = await self.post()
        self.assert_refused_before_provider(refused)
        self.store.write_product_profile_record(self.profile)
        continued = await self.post()
        self.assertEqual(continued.status_code, 202, continued.text)
        self.assertEqual(self.writes, self.expected_routes())
        changed = await self.post(reason="A different request under the original key")
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(self.writes, self.expected_routes())

    async def test_live_owner_is_refused_before_provider_contact(self) -> None:
        self.profile = self.profile.model_copy(update={"production_use": "live"})
        self.store.write_product_profile_record(self.profile)
        self.assert_refused_before_provider(await self.post())

    async def test_unproved_caller_or_shared_context_is_refused_before_provider_contact(
        self,
    ) -> None:
        with scoped_agent_transport():
            self.assert_refused_before_provider(await self.post())
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"product": "foreign-product"})
        )
        self.assert_refused_before_provider(await self.post())

    async def test_caller_supplied_authority_fields_are_rejected(self) -> None:
        for changes in ({"director_agent": True}, {"production_use": "prelaunch"}):
            with self.subTest(changes=changes):
                response = await self.post(**changes)
                self.assert_no_effects(response, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_request")
                self.inventory.assert_not_called()

    async def test_recorded_use_or_data_protection_prevents_route_effects(self) -> None:
        for changes in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
        ):
            with self.subTest(changes=changes):
                record_safety(self.store, self.profile.product, self.context, "prod", **changes)
                self.assert_no_effects(await self.post(), 400)

    async def test_deployment_history_prevents_route_effects(self) -> None:
        from tests.test_evidence_ingestion import _deployment_record

        self.store.write_deployment_record(_deployment_record(context=self.context))
        self.assert_no_effects(await self.post(), 400)

    async def test_served_inventory_prevents_route_effects(self) -> None:
        from tests.http_app_test_support import _environment_inventory_read_record

        self.store.write_environment_inventory(
            _environment_inventory_read_record().model_copy(
                update={"context": self.context, "instance": "prod"}
            )
        )
        self.assert_no_effects(await self.post(), 400)

    async def test_unowned_or_changed_target_binding_prevents_route_effects(self) -> None:
        for target, target_id in (
            (self.target.model_copy(update={"env": {"PRIVATE": "value"}}), self.target_id),
            (
                self.target.model_copy(update={"domains": ("other.example.invalid",)}),
                self.target_id,
            ),
            (
                self.target,
                self.target_id.model_copy(update={"source_label": "cli:dokploy-targets:adopt"}),
            ),
        ):
            with self.subTest(target=target, target_id=target_id):
                self.store.write_dokploy_target_record(target)
                self.store.write_dokploy_target_id_record(target_id)
                response = await self.post()
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.writes, [])
                self.assertEqual(self.read_target(), target)
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.assert_no_effects(await self.post(target_id="reviewed-but-replaced-compose"), 400)

    async def test_target_alias_of_another_lane_prevents_route_effects(self) -> None:
        self.store.write_dokploy_target_id_record(
            self.target_id.model_copy(update={"context": "other", "instance": "testing"})
        )
        self.assert_no_effects(await self.post(), 409)

    async def test_changed_provider_facts_prevent_route_effects(self) -> None:
        baseline = deepcopy(self.live)
        for changes in (
            {"env": "PRIVATE_KEY=value"},
            {"mounts": [{"type": "bind"}]},
            {"composeFile": "services: {}"},
            {"customGitUrl": "https://example.invalid/source.git"},
            {"composeStatus": "running"},
            {"domains": [{"domainId": "foreign", "host": "foreign.example.invalid"}]},
        ):
            with self.subTest(changes=changes):
                self.live = {**baseline, **changes}
                self.assert_no_effects(await self.post(), 400)

    async def test_provider_deployment_history_prevents_route_effects(self) -> None:
        original = self.provider_request
        self.inventory.side_effect = lambda **kwargs: (
            [{"composeId": self.target_id.target_id, "deploymentId": "earlier", "status": "done"}]
            if kwargs["path"] == "/api/deployment.allByType"
            else original(**kwargs)
        )
        self.assert_no_effects(await self.post(), 400)

    async def test_broader_or_mismatched_inputs_prevent_route_effects(self) -> None:
        for changes in (
            {"project_id": "existing-project"},
            {"environment_id": "existing-environment"},
            {"server_id": "existing-server"},
            {"runtime_port": self.profile.runtime_port + 1},
            {"domains": ["*.example.invalid"]},
            {"domains": ["Example.invalid"]},
        ):
            with self.subTest(changes=changes):
                response = await self.post(**changes)
                self.assertGreaterEqual(response.status_code, 400, response.text)
                self.assertEqual(self.writes, [])
                self.assertEqual(self.read_target(), self.target)

    async def test_hostname_claimed_elsewhere_prevents_route_effects(self) -> None:
        from tests.test_route_binding_record import _route_binding_record

        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="other",
                instance="prod",
                domains=(self.payload["domains"][1],),
                updated_at=self.profile.updated_at,
            )
        )
        self.assert_no_effects(await self.post(), 400)
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="other", instance="prod", updated_at=self.profile.updated_at
            )
        )
        self.store.write_route_binding_record(
            _route_binding_record(domain=self.payload["domains"][0])
        )
        self.assert_no_effects(await self.post(), 400)

    async def test_unresolved_original_setup_keeps_custody(self) -> None:
        from control_plane.dokploy_target_setup_http import first_production_target_lock_name

        original = self.store.reserve_mutation(
            scope="original-owner",
            route_path="/v1/dokploy-targets/setup",
            idempotency_key="original-setup",
            request_fingerprint="original-setup",
            lease_owner="original-executor",
            lease_seconds=300,
            reconciliation_key="original-setup-binding",
            provider_target_key=first_production_target_lock_name(self.context),
        ).record
        assert original is not None
        self.assert_no_effects(await self.post(), 400)

    async def test_active_production_executor_prevents_route_effects(self) -> None:
        from contextlib import nullcontext

        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            self.assert_no_effects(await self.post(), 400)

    async def test_missing_receipt_recovers_only_the_recorded_exact_hostnames(self) -> None:
        with patch.object(
            self.store,
            "write_idempotency_record",
            side_effect=RuntimeError("Fixture lost receipt storage"),
        ):
            with self.assertRaises(RuntimeError):
                await self.post()
        expanded = await self.post(domains=[*self.payload["domains"], "www.example.invalid"])
        self.assertEqual(expanded.status_code, 400, expanded.text)
        recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(self.writes, self.expected_routes())

    async def test_lost_second_route_response_recovers_without_duplicate_routes(self) -> None:
        original = self.provider_request

        def provider(**kwargs: Any) -> Any:
            result = original(**kwargs)
            if kwargs.get("method") == "POST" and len(self.writes) == 2:
                raise RuntimeError("Lost response after route creation")
            return result

        self.inventory.side_effect = provider
        lost = await self.post()
        self.assertEqual(lost.status_code, 502, lost.text)
        self.assertEqual(lost.json()["error"]["code"], "dokploy_domain_partial_outcome")
        self.assertEqual(self.read_target(), self.target)
        recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(self.writes, self.expected_routes())
        self.assertEqual(self.read_target().domains, tuple(self.payload["domains"]))


CREATED_HOSTNAME = "seeded.example.invalid"


class DirectorCreatedHostnameProductionDomainTests(_NeverLiveProductionDomainFixture):
    def setUp(self) -> None:
        super().setUp()
        self.target = self.target.model_copy(
            update={"domains": (CREATED_HOSTNAME,), "source_label": CREATE_COMPOSE_SOURCE_LABEL}
        )
        self.store.write_dokploy_target_record(self.target)

    def record_creation_receipt(
        self,
        *,
        created: DokployTargetRecord | None = None,
        created_id: DokployTargetIdRecord | None = None,
        trace_id: str = "fixture-original-create",
        **result_changes: Any,
    ) -> None:
        from control_plane.contracts.idempotency_record import (
            LaunchplaneIdempotencyRecord,
            build_launchplane_idempotency_record_id,
        )
        from control_plane.dokploy_compose_allocation import ROUTE
        from control_plane.dokploy_target_setup_http import dokploy_target_setup_result_payload
        from control_plane.provider_operations import provider_operation_response_payload
        from control_plane.workflows.dokploy_target_adoption import DokployTargetAdoptionResult

        setup = dokploy_target_setup_result_payload(
            DokployTargetAdoptionResult(
                applied=True,
                target_record=created or self.target,
                target_id_record=created_id or self.target_id,
                provider_target_record=self.target_provider,
            )
        )
        result = {
            "mode": "apply",
            "operation": "create-compose",
            "context": self.context,
            "instance": "prod",
            "applied": True,
            "route_domain_ids": [],
            "setup": setup,
            **result_changes,
        }
        self.store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(response_trace_id=trace_id),
                scope="fixture-original-caller",
                route_path=ROUTE,
                idempotency_key=trace_id,
                request_fingerprint=trace_id,
                response_status_code=202,
                response_trace_id=trace_id,
                recorded_at=self.profile.updated_at,
                response_payload=provider_operation_response_payload(
                    trace_id=trace_id, records={}, result=result
                ),
            )
        )

    def assert_original_refusal(self, response: Any) -> None:
        self.assert_no_effects(response, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_dokploy_target_setup")
        self.inventory.assert_not_called()

    async def test_unrouted_created_hostname_gives_way_to_only_the_reviewed_names(self) -> None:
        self.record_creation_receipt()
        await self.plan_apply_and_replay()
        self.assertEqual(self.writes, self.expected_routes())
        recorded = self.read_target()
        self.assertEqual(recorded.domains, tuple(self.payload["domains"]))
        self.assertNotIn(CREATED_HOSTNAME, recorded.hostname_claims)
        self.assertEqual(
            recorded.model_dump(exclude={"domains", "updated_at", "source_label"}),
            self.target.model_dump(exclude={"domains", "updated_at", "source_label"}),
        )
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        self.assertEqual(
            self.store.read_provider_target_record(context_name=self.context, instance_name="prod"),
            self.target_provider,
        )
        self.assertEqual(self.store.list_deployment_records(), ())
        self.assertEqual(self.store.list_promotion_records(), ())

    async def test_tracked_hostname_without_creation_receipt_keeps_the_record_refusal(
        self,
    ) -> None:
        self.assert_original_refusal(await self.post())

    async def test_receipt_that_does_not_bind_the_unrouted_records_keeps_the_refusal(
        self,
    ) -> None:
        cases: tuple[dict[str, Any], ...] = (
            {"route_domain_ids": ["created-route"]},
            {"operation": "adopt"},
            {"mode": "dry-run"},
            {"applied": False},
            {"instance": "testing"},
            {"context": "other-context"},
            {"created": self.target.model_copy(update={"updated_at": "2020-01-01T00:00:00Z"})},
            {"created": self.target.model_copy(update={"domains": ("other.example.invalid",)})},
            {"created": self.target.model_copy(update={"source_label": "cli:adopt"})},
            {"created_id": self.target_id.model_copy(update={"target_id": "replaced-compose"})},
            {
                "created_id": self.target_id.model_copy(
                    update={"updated_at": "2020-01-01T00:00:00Z"}
                )
            },
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                self.record_creation_receipt(trace_id=f"unbound-receipt-{index}", **changes)
                self.assert_original_refusal(await self.post())

    async def test_extra_or_foreign_tracked_names_keep_the_refusal(self) -> None:
        self.record_creation_receipt()
        for domains in (
            (CREATED_HOSTNAME, "extra.example.invalid"),
            ("foreign.example.invalid",),
        ):
            with self.subTest(domains=domains):
                self.target = self.target.model_copy(update={"domains": domains})
                self.store.write_dokploy_target_record(self.target)
                self.assert_original_refusal(await self.post())

    async def test_routed_created_hostname_prevents_route_effects(self) -> None:
        from control_plane.dokploy.compose import compose_web_domain_route_payload

        self.record_creation_receipt()
        self.routes = [
            {
                "domainId": "seeded-route",
                **compose_web_domain_route_payload(
                    compose_id=self.target_id.target_id,
                    domain_host=CREATED_HOSTNAME,
                    runtime_port=self.profile.runtime_port,
                ),
            }
        ]
        self.live["domains"] = deepcopy(self.routes)
        self.assert_no_effects(await self.post(), 400)

    async def test_created_hostname_in_use_elsewhere_prevents_route_effects(self) -> None:
        from tests.test_route_binding_record import _route_binding_record

        self.record_creation_receipt()
        own_lane_url = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"base_url": f"https://{CREATED_HOSTNAME}"})
                    if (lane.context, lane.instance) == (self.context, "prod")
                    else lane
                    for lane in self.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(own_lane_url)
        self.assert_no_effects(await self.post(), 400)
        self.store.write_product_profile_record(self.profile)
        self.store.write_route_binding_record(_route_binding_record(domain=CREATED_HOSTNAME))
        self.assert_no_effects(await self.post(), 400)

    async def test_created_hostname_keeps_live_use_and_custody_protections(self) -> None:
        from control_plane.dokploy_target_setup_http import first_production_target_lock_name
        from tests.test_evidence_ingestion import _deployment_record

        self.record_creation_receipt()
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"production_use": "live"})
        )
        self.assert_refused_before_provider(await self.post())
        self.store.write_product_profile_record(self.profile)
        record_safety(self.store, self.profile.product, self.context, "prod", usage="in_use")
        self.assert_no_effects(await self.post(), 400)
        record_safety(self.store, self.profile.product, self.context, "prod")
        self.store.reserve_mutation(
            scope="original-owner",
            route_path="/v1/dokploy-targets/setup",
            idempotency_key="original-setup",
            request_fingerprint="original-setup",
            lease_owner="original-executor",
            lease_seconds=300,
            reconciliation_key="original-setup-binding",
            provider_target_key=first_production_target_lock_name(self.context),
        )
        self.assert_no_effects(await self.post(), 400)
        self.store.write_deployment_record(_deployment_record(context=self.context))
        self.assert_no_effects(await self.post(), 400)

    async def test_lost_second_route_recovers_without_keeping_the_created_hostname(
        self,
    ) -> None:
        self.record_creation_receipt()
        original = self.provider_request

        def provider(**kwargs: Any) -> Any:
            result = original(**kwargs)
            if kwargs.get("method") == "POST" and len(self.writes) == 2:
                raise RuntimeError("Lost response after route creation")
            return result

        self.inventory.side_effect = provider
        lost = await self.post()
        self.assertEqual(lost.status_code, 502, lost.text)
        self.assertEqual(lost.json()["error"]["code"], "dokploy_domain_partial_outcome")
        self.assertEqual(self.read_target(), self.target)
        changed = await self.post(domains=["new.example.invalid"])
        self.assertEqual(changed.status_code, 400, changed.text)
        recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(self.writes, self.expected_routes())
        self.assertEqual(self.read_target().domains, tuple(self.payload["domains"]))

    async def test_lost_receipt_continues_only_the_same_reviewed_request(self) -> None:
        self.record_creation_receipt()
        with patch.object(
            self.store,
            "write_idempotency_record",
            side_effect=RuntimeError("Fixture lost receipt storage"),
        ):
            with self.assertRaises(RuntimeError):
                await self.post()
        with_created = await self.post(domains=[*self.payload["domains"], CREATED_HOSTNAME])
        self.assertEqual(with_created.status_code, 400, with_created.text)
        recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(self.writes, self.expected_routes())
        self.assertEqual(self.read_target().domains, tuple(self.payload["domains"]))
