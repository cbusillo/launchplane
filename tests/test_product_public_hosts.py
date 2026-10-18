import copy
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import ClassVar, cast
from fastapi import FastAPI
from httpx2 import Response
from unittest.mock import patch
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, PublicHostEffect
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.dokploy import api
from control_plane.dokploy import source
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore, LaunchplaneIdempotencyRow
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    PublicHostAuthorityBusy,
)
from tests.support.auth import identity, StubVerifier
from tests.support.http import request
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url


PRODUCT = "odoo-tenant-cm-website"
CONTEXT = "cm_website"
HOSTS = ["cellmechanic.com", "www.cellmechanic.com"]


class StubProvider:
    def __init__(self) -> None:
        self.routes: list[api.JsonObject] = [
            {
                "domainId": "internal",
                "composeId": "cm-prod-compose",
                "host": "cm-website-prod",
                "serviceName": "web",
                "port": 8069,
                "path": "/",
                "internalPath": "/",
                "https": True,
                "certificateType": "none",
                "stripPath": False,
            }
        ]
        self.writes: list[str] = []
        self.fail_host = ""
        self.drop_writes = False
        self.lose_create_response = False

    def call(self, **kwargs: object) -> api.JsonValue:
        path = kwargs["path"]
        if path == "/api/domain.byComposeId":
            return cast(api.JsonValue, copy.deepcopy(self.routes))
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        self.writes.append(str(path))
        if payload.get("host") == self.fail_host:
            raise RuntimeError("provider disconnected")
        if path == "/api/domain.create":
            route = {"domainId": f"public-{len(self.routes)}", **payload}
            if not self.drop_writes:
                self.routes.append(route)
            if self.lose_create_response:
                raise RuntimeError("provider created the route then disconnected")
            return route
        if path == "/api/domain.update":
            for route in self.routes:
                if route["domainId"] == payload["domainId"] and not self.drop_writes:
                    route.update(payload)
            return payload
        if path == "/api/domain.delete":
            self.routes = [
                route for route in self.routes if route["domainId"] != payload["domainId"]
            ]
            return {}
        raise AssertionError(path)


class ProductPublicHostsTests(unittest.IsolatedAsyncioTestCase):
    current_store: ClassVar[ContextVar[PostgresRecordStore]]
    app: ClassVar[FastAPI]

    @classmethod
    def setUpClass(cls) -> None:
        cls.current_store = ContextVar("public_hosts_store")
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/verireel",
                        "workflow_refs": [identity().workflow_ref],
                        "event_names": ["pull_request"],
                        "products": [PRODUCT],
                        "actions": ["product_config.plan", "product_config.apply"],
                    }
                ]
            }
        )
        cls.app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=policy,
            record_store_factory=lambda: cls.current_store.get(),
        )

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(self.temp.name) / "state.db")
        )
        self.store.ensure_schema()
        token = self.current_store.set(self.store)
        self.addCleanup(self.current_store.reset, token)
        payload = _odoo_preview_profile_payload()
        payload.update(
            product=PRODUCT,
            repository="example/cm-website",
            lanes=[{"context": CONTEXT, "instance": "prod"}],
        )
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(payload)
        )
        self.target = DokployTargetRecord(
            context=CONTEXT,
            instance="prod",
            target_type="compose",
            domains=("cm-website-prod",),
            updated_at="2026-10-08T22:00:00Z",
        )
        self.target_id = DokployTargetIdRecord(
            context=CONTEXT,
            instance="prod",
            target_id="cm-prod-compose",
            updated_at=self.target.updated_at,
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=self.target, target_id_record=self.target_id
            )
        )
        self.provider = StubProvider()
        self.enterContext(
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://provider.example", "private-token"),
            )
        )
        self.enterContext(
            patch("control_plane.dokploy.api.dokploy_request", side_effect=self.provider.call)
        )

    async def config(
        self, hosts: object = HOSTS, *, mode: str = "dry-run", key: str = "", **overrides: object
    ) -> Response:
        payload = {
            "mode": mode,
            "product": PRODUCT,
            "context": CONTEXT,
            "instance": "prod",
            "public_hosts": hosts,
            **overrides,
        }
        return await request(
            self.app,
            "POST",
            "/v1/product-config/apply",
            payload=payload,
            headers={"Authorization": "Bearer valid-token", "Idempotency-Key": key},
        )

    def recorded(self) -> DokployTargetRecord:
        return self.store.read_dokploy_target_record(context_name=CONTEXT, instance_name="prod")

    async def test_foreign_private_endpoint_reserves_hostname_before_provider_effect(self) -> None:
        dry = await self.config()
        self.assertEqual(dry.status_code, 202, dry.text)
        self.store.write_private_health_endpoint_record(
            PrivateHealthEndpointRecord(
                endpoint_key="private-testing",
                product="another-product",
                context="another-lane",
                instance="testing",
                url=f"https://{HOSTS[0]}/health",
                updated_at=self.target.updated_at,
            )
        )
        response = await self.config(mode="apply", key="private-host-conflict")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "public_hosts_target_changed")
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.recorded(), self.target)

    @contextmanager
    def failing_config_commit(self, key: str) -> Iterator[None]:
        def fail_commit(session: Session) -> None:
            if (
                session.scalar(
                    select(LaunchplaneIdempotencyRow).where(
                        LaunchplaneIdempotencyRow.idempotency_key == key
                    )
                )
                is not None
            ):
                raise RuntimeError("injected final storage commit failure")

        event.listen(self.store._session_factory, "before_commit", fail_commit)
        try:
            yield
        finally:
            event.remove(self.store._session_factory, "before_commit", fail_commit)

    async def test_cm_dry_run_apply_readback_replay_and_noop(self) -> None:
        dry = await self.config()
        self.assertEqual(dry.status_code, 202, dry.text)
        diff = dry.json()["result"]["public_hosts"]
        self.assertEqual(diff["added"], HOSTS)
        self.assertEqual(diff["removed"], [])
        self.assertFalse(diff["verified"])
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.recorded(), self.target)
        response = await self.config(mode="apply", key="first")
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()["result"]["public_hosts"]
        self.assertTrue(result["verified"])
        self.assertEqual(result["read_back_hosts"], HOSTS)
        self.assertEqual(self.recorded().public_hosts, tuple(HOSTS))
        self.assertEqual(self.recorded().domains, self.target.domains)
        self.assertEqual(self.provider.routes[0]["domainId"], "internal")
        for route in self.provider.routes[1:]:
            self.assertEqual(
                (route["serviceName"], route["port"], route["https"], route["certificateType"]),
                ("web", 8069, True, "none"),
            )
        writes = list(self.provider.writes)
        replay = await self.config(mode="apply", key="first")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.provider.writes, writes)
        await self.config()
        noop = await self.config(mode="apply", key="noop")
        self.assertEqual(noop.status_code, 202, noop.text)
        self.assertEqual(noop.json()["result"]["public_hosts"]["unchanged"], HOSTS)
        self.assertEqual(self.provider.writes, writes)

    async def test_busy_authority_is_retryable_after_a_fresh_dry_run(self) -> None:
        await self.config()
        with patch.object(
            self.store,
            "write_product_public_hosts_bundle",
            side_effect=PublicHostAuthorityBusy("Review a fresh dry-run."),
        ):
            response = await self.config(mode="apply", key="busy")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "public_hosts_authority_busy")
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.recorded(), self.target)
        await self.config()
        response = await self.config(mode="apply", key="retry")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.recorded().public_hosts, tuple(HOSTS))

    async def test_reordering_hosts_changes_base_url_without_route_writes(self) -> None:
        await self.config()
        await self.config(mode="apply", key="add")
        writes = list(self.provider.writes)
        hosts = list(reversed(HOSTS))
        dry = await self.config(hosts)
        diff = dry.json()["result"]["public_hosts"]
        self.assertEqual(diff["after"], hosts)
        response = await self.config(hosts, mode="apply", key="reorder")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.recorded().public_hosts, tuple(hosts))
        self.assertEqual(response.json()["result"]["public_hosts"]["read_back_hosts"], hosts)
        self.assertEqual(self.provider.writes, writes)

    async def test_drop_only_a_previously_managed_name_and_preserve_origin(self) -> None:
        await self.config()
        await self.config(mode="apply", key="add")
        dry = await self.config([HOSTS[0]])
        self.assertEqual(dry.json()["result"]["public_hosts"]["removed"], [HOSTS[1]])
        response = await self.config([HOSTS[0]], mode="apply", key="remove")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            {route["host"] for route in self.provider.routes}, {"cm-website-prod", HOSTS[0]}
        )
        await self.config([])
        response = await self.config([], mode="apply", key="clear")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.recorded().domains, ("cm-website-prod",))

    async def test_stale_provider_diff_requires_new_dry_run(self) -> None:
        await self.config()
        self.provider.routes[0]["port"] = 8080
        response = await self.config(mode="apply", key="stale")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.provider.writes, [])
        await self.config()
        response = await self.config(mode="apply", key="fresh")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(all(route["port"] == 8080 for route in self.provider.routes))

    async def test_partial_failure_keeps_records_and_success_receipt_uncommitted(self) -> None:
        await self.config()
        self.provider.fail_host = HOSTS[1]
        response = await self.config(mode="apply", key="partial")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.recorded().public_hosts, self.target.public_hosts)
        self.assertEqual(self.recorded().domains, self.target.domains)
        self.assertTrue(self.recorded().pending_public_host_effects[0].domain_id)
        self.provider.fail_host = ""
        response = await self.config(mode="apply", key="partial")
        self.assertEqual(response.status_code, 409, response.text)
        await self.config()
        response = await self.config(mode="apply", key="partial")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.provider.routes), 3)

    async def test_readback_mismatch_is_not_success(self) -> None:
        await self.config()
        self.provider.drop_writes = True
        response = await self.config(mode="apply", key="ignored")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.recorded().public_hosts, self.target.public_hosts)
        self.assertEqual(self.recorded().domains, self.target.domains)

    async def test_storage_failure_then_abandoned_addition_is_removed(self) -> None:
        unmanaged = {
            **self.provider.routes[0],
            "host": "unmanaged.example.com",
            "domainId": "unmanaged",
        }
        self.provider.routes.append(unmanaged)
        await self.config()
        with self.failing_config_commit("storage-failure"):
            with self.assertRaisesRegex(RuntimeError, "injected final storage"):
                await self.config(mode="apply", key="storage-failure")
        self.assertEqual(self.recorded().public_hosts, ())
        self.assertTrue(set(HOSTS).issubset({route["host"] for route in self.provider.routes}))
        # Resume through a fresh store, as after a service restart.
        self.store = PostgresRecordStore(database_url=self.store.database_url)
        token = self.current_store.set(self.store)
        self.addCleanup(self.current_store.reset, token)
        dry = await self.config([HOSTS[1]])
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(dry.json()["result"]["public_hosts"]["removed"], [HOSTS[0]])
        response = await self.config([HOSTS[1]], mode="apply", key="recover-revised")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            {route["host"] for route in self.provider.routes},
            {"cm-website-prod", "unmanaged.example.com", HOSTS[1]},
        )
        self.assertIn(unmanaged, self.provider.routes)
        self.assertEqual(self.recorded().pending_public_host_effects, ())
        writes = list(self.provider.writes)
        replay = await self.config([HOSTS[1]], mode="apply", key="recover-revised")
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.provider.writes, writes)

    async def test_lost_provider_identity_stays_tracked_without_adoption_or_deletion(self) -> None:
        await self.config()
        self.provider.lose_create_response = True
        response = await self.config(mode="apply", key="lost-response")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.recorded().public_hosts, ())
        self.assertTrue(self.recorded().pending_public_host_effects)
        writes = list(self.provider.writes)
        for hosts in (HOSTS, []):
            response = await self.config(hosts)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.provider.writes, writes)
        self.assertIn(HOSTS[0], {route["host"] for route in self.provider.routes})

    async def test_replaced_pending_route_identity_refuses_removal(self) -> None:
        await self.config()
        self.provider.fail_host = HOSTS[1]
        response = await self.config(mode="apply", key="partial-identity")
        self.assertEqual(response.status_code, 502, response.text)
        self.provider.routes[-1]["domainId"] = "foreign-replacement"
        writes = list(self.provider.writes)
        response = await self.config([])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.provider.writes, writes)

    async def test_preexisting_matching_public_route_is_not_adopted(self) -> None:
        self.provider.routes.append(
            {**self.provider.routes[0], "host": HOSTS[0], "domainId": "unmanaged"}
        )
        response = await self.config()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.recorded(), self.target)

    async def test_intent_commit_failure_has_no_provider_effect(self) -> None:
        await self.config()
        with patch.object(
            self.store,
            "_after_product_authority_bundle_step",
            side_effect=RuntimeError("intent unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "intent unavailable"):
                await self.config(mode="apply", key="intent-failure")
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.recorded(), self.target)

    async def test_checkpoint_failure_retains_intent_without_guessing_identity(self) -> None:
        await self.config()

        def fail_checkpoint(session: object) -> None:
            if self.provider.writes:
                raise RuntimeError("provider identity commit unavailable")

        event.listen(self.store._session_factory, "before_commit", fail_checkpoint)
        try:
            response = await self.config(mode="apply", key="checkpoint-failure")
        finally:
            event.remove(self.store._session_factory, "before_commit", fail_checkpoint)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertTrue(self.recorded().pending_public_host_effects)
        self.assertFalse(self.recorded().pending_public_host_effects[0].domain_id)
        writes = list(self.provider.writes)
        response = await self.config([])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.provider.writes, writes)

    async def test_writer_between_checkpoints_is_rechecked_before_more_effects(self) -> None:
        await self.config()
        changed = False
        other_store = PostgresRecordStore(database_url=self.store.database_url)

        def change_after_checkpoint(session: object) -> None:
            nonlocal changed
            if not changed and self.provider.writes:
                changed = True
                current = other_store.read_dokploy_target_record(
                    context_name=CONTEXT, instance_name="prod"
                )
                other_store.write_dokploy_target_record(
                    current.model_copy(update={"source_label": "concurrent-writer"})
                )

        event.listen(self.store._session_factory, "after_commit", change_after_checkpoint)
        try:
            response = await self.config(mode="apply", key="checkpoint-race")
        finally:
            event.remove(self.store._session_factory, "after_commit", change_after_checkpoint)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn(HOSTS[1], {route["host"] for route in self.provider.routes})
        self.assertEqual(self.recorded().public_hosts, ())
        self.assertEqual(self.recorded().source_label, "concurrent-writer")

    async def test_provider_change_between_checkpoints_is_not_overwritten(self) -> None:
        await self.config()
        changed = False
        foreign = {
            **self.provider.routes[0],
            "host": HOSTS[1],
            "domainId": "foreign",
            "serviceName": "admin",
        }

        def change_after_checkpoint(session: object) -> None:
            nonlocal changed
            if not changed and self.provider.writes:
                changed = True
                self.provider.routes.append(foreign)

        event.listen(self.store._session_factory, "after_commit", change_after_checkpoint)
        try:
            response = await self.config(mode="apply", key="checkpoint-provider-race")
        finally:
            event.remove(self.store._session_factory, "after_commit", change_after_checkpoint)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn(foreign, self.provider.routes)
        self.assertEqual(self.recorded().public_hosts, ())

    async def test_pending_route_that_became_an_origin_is_protected(self) -> None:
        await self.config()
        self.provider.fail_host = HOSTS[1]
        response = await self.config(mode="apply", key="pending-origin")
        self.assertEqual(response.status_code, 502, response.text)
        self.store.write_dokploy_target_record(
            self.recorded().model_copy(update={"domains": (*self.target.domains, HOSTS[0])})
        )
        writes = list(self.provider.writes)
        response = await self.config([])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.provider.writes, writes)

    async def test_pending_route_port_update_is_checkpointed_before_config_commit(self) -> None:
        await self.config()
        self.provider.fail_host = HOSTS[1]
        response = await self.config(mode="apply", key="partial-port")
        self.assertEqual(response.status_code, 502, response.text)
        self.provider.fail_host = ""
        self.provider.routes[0]["port"] = 8080
        await self.config([HOSTS[0]])
        with self.failing_config_commit("port-change"):
            with self.assertRaisesRegex(RuntimeError, "injected final storage"):
                await self.config([HOSTS[0]], mode="apply", key="port-change")
        effect = self.recorded().pending_public_host_effects[0]
        self.assertEqual(effect.runtime_port, self.provider.routes[0]["port"])
        response = await self.config([])
        self.assertEqual(response.status_code, 202, response.text)
        response = await self.config([], mode="apply", key="recover-port")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.provider.routes, [self.provider.routes[0]])

    async def test_binding_changed_at_commit_refuses_before_provider_write(self) -> None:
        await self.config()
        original = self.store.write_product_public_hosts_bundle

        def changed(
            bundle: ProductAuthorityBundle,
            *,
            expected_target: DokployTargetRecord,
            expected_target_id: DokployTargetIdRecord,
            expected_provider_target: ProviderTargetRecord,
            apply_provider: Callable[[Callable[[str, str, int], None]], None],
            pending_effects: tuple[PublicHostEffect, ...],
            verify_provider: Callable[[], None],
        ) -> None:
            self.store.write_dokploy_target_id_record(
                self.target_id.model_copy(update={"target_id": "replacement-compose"})
            )
            original(
                bundle,
                expected_target=expected_target,
                expected_target_id=expected_target_id,
                expected_provider_target=expected_provider_target,
                apply_provider=apply_provider,
                pending_effects=pending_effects,
                verify_provider=verify_provider,
            )

        with patch.object(self.store, "write_product_public_hosts_bundle", side_effect=changed):
            response = await self.config(mode="apply", key="binding-changed")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.recorded(), self.target)

    async def test_target_shared_with_another_lane_refuses_before_provider_write(self) -> None:
        await self.config()
        self.store.write_dokploy_target_id_record(
            self.target_id.model_copy(update={"context": "another-product"})
        )
        response = await self.config(mode="apply", key="shared")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.provider.writes, [])

    async def test_another_products_hostname_cannot_be_claimed(self) -> None:
        self.store.write_dokploy_target_record(
            self.target.model_copy(update={"context": "other", "domains": (HOSTS[0],)})
        )
        response = await self.config()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.provider.writes, [])

    async def test_record_definition_roundtrip_keeps_public_config_and_origin_health(self) -> None:
        await self.config()
        await self.config(mode="apply", key="roundtrip")
        definition = source.build_dokploy_source_of_truth_from_records(
            (self.recorded(),), (self.target_id,)
        ).targets[0]
        rebuilt = source.build_dokploy_target_record_from_definition(
            definition,
            updated_at=self.recorded().updated_at,
        )
        self.assertEqual(rebuilt.public_hosts, tuple(HOSTS))
        self.assertEqual(
            source.resolve_healthcheck_base_urls(
                target_definition=definition,
                environment_values={},
            ),
            ("https://cm-website-prod",),
        )

    async def test_removing_managed_host_after_origin_port_change(self) -> None:
        await self.config()
        await self.config(mode="apply", key="before-port-change")
        self.provider.routes[0]["port"] = 8080
        dry = await self.config([])
        self.assertEqual(dry.status_code, 202, dry.text)
        response = await self.config([], mode="apply", key="after-port-change")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual([route["host"] for route in self.provider.routes], ["cm-website-prod"])

    async def test_scope_validation_and_unreviewed_apply_never_write(self) -> None:
        for overrides in ({"instance": "testing"}, {"context": "foreign"}, {"context": ""}):
            with self.subTest(overrides=overrides):
                response = await self.config(**overrides)
                self.assertIn(response.status_code, (400, 403), response.text)
        for hosts in (
            ["https://cellmechanic.com"],
            ["*.cellmechanic.com"],
            ["x:80"],
            ["a.example", "A.example"],
            None,
        ):
            with self.subTest(hosts=hosts):
                response = await self.config(hosts)
                self.assertEqual(response.status_code, 400, response.text)
        response = await self.config(mode="apply", key="unreviewed")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.provider.writes, [])

    async def test_internal_name_cannot_become_managed_or_remove_foreign_service(self) -> None:
        self.store.write_dokploy_target_record(
            self.target.model_copy(update={"domains": ("origin.example",)})
        )
        self.provider.routes[0]["host"] = "origin.example"
        response = await self.config(["origin.example"])
        self.assertEqual(response.status_code, 400, response.text)
        self.provider.routes.append(
            {
                **self.provider.routes[0],
                "host": HOSTS[0],
                "domainId": "foreign",
                "serviceName": "admin",
            }
        )
        response = await self.config()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.provider.writes, [])
