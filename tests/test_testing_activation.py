"""Qualified testing activation preserves production and the original unknown outcome."""

import base64
from copy import deepcopy
import json
import unittest
from typing import Any
from collections.abc import Callable
from unittest.mock import patch

from control_plane.contracts.testing_activation import (
    TestingActivationRequest,
    TestingActivationRecord,
    TestingActivationRefusal,
    TestingActivationRefusalCode,
)
from control_plane.integration_allowances import target_record_sha256
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.testing_activation import (
    ROUTE,
    TestingActivationAdapter,
    activation_record,
    configuration_digest,
    retained_fence_is_separated,
    runtime_values as resolve_activation_runtime,
)
from control_plane.testing_activation_compose import (
    interpolate,
    qualify_compose,
    read_compose_source,
)
from tests import test_testing_separation_resources as preparation
from control_plane.testing_separation_resources import preparation_binding


COMPOSE = {
    "services": {
        "web": {
            "image": "${DOCKER_IMAGE_REFERENCE:?required}",
            "environment": {"DB_HOST": "database", "DB_PASSWORD": "${DB_PASSWORD}"},
            "ports": ["${HTTP_PORT}:8000"],
            "volumes": ["config:/config"],
        },
        "database": {
            "image": "mariadb:fixture",
            "environment": {"MYSQL_PASSWORD": "${DB_PASSWORD}"},
            "ports": ["${DB_PORT}:3306"],
            "volumes": ["database:/var/lib/mysql"],
        },
    },
    "volumes": {"database": {"name": "${DB_VOLUME}"}, "config": {"name": "${CONFIG_VOLUME}"}},
}


def activation_hold_policy(product: str, context: str) -> Any:
    from control_plane.service_auth import LaunchplaneAuthzPolicy

    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-write"],
                    "products": [product],
                    "contexts": [context],
                    "instances": ["testing"],
                    "actions": ["product_config.plan", "product_config.apply"],
                }
            ],
        }
    )


class ComposeQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {
            "DOCKER_IMAGE_REFERENCE": "example@sha256:" + "a" * 64,
            "DB_PASSWORD": "safe-test-password",
            "HTTP_PORT": "19212",
            "DB_PORT": "19213",
            "DB_VOLUME": "new-db",
            "CONFIG_VOLUME": "new-config",
        }
        self.document = interpolate(COMPOSE, self.values)

    def qualify(self) -> None:
        qualify_compose(
            self.document,
            volumes={"new-db", "new-config"},
            ports={19212, 19213},
            credential_values={"safe-test-password"},
        )

    def test_valid_render_and_product_local_include(self) -> None:
        files = {"deploy/compose.yml": {"include": ["../service.yml"]}, "service.yml": COMPOSE}

        def read(path: str) -> Any:
            name = path.split("/contents/", 1)[1].split("?", 1)[0]
            return {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(json.dumps(files[name]).encode()).decode(),
            }

        source = read_compose_source(
            repository="example/site", commit="a" * 40, path="deploy/compose.yml", read=read
        )
        self.assertEqual(json.loads(source), COMPOSE)
        self.qualify()

    def test_production_storage_ports_credentials_and_host_capabilities_refuse(self) -> None:
        mutations: list[Callable[[Any], None]] = [
            lambda d: d["volumes"]["database"].update(name="production-db"),
            lambda d: d["volumes"]["database"].update(driver_opts={"device": "/production"}),
            lambda d: d["services"]["database"].update(volumes=["/production:/var/lib/mysql"]),
            lambda d: d["services"]["web"].update(ports=["3307:8000"]),
            lambda d: d["services"]["web"].update(network_mode="host"),
            lambda d: d["services"]["web"].update(privileged=True),
            lambda d: d["services"]["web"]["environment"].update(DB_PASSWORD="production-password"),
            lambda d: d["services"]["web"]["environment"].update(DB_HOST="production.internal"),
            lambda d: d.update(networks={"default": {"external": True}}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.document = interpolate(COMPOSE, self.values)
                mutation(self.document)
                with self.assertRaises(ValueError):
                    self.qualify()


class ActivationTests(unittest.TestCase):
    database_url = ""
    private_product = True

    def setUp(self) -> None:
        self.preparation = preparation.SeparationResourceTests()
        self.preparation.database_url = self.database_url
        self.preparation.setUp()
        self.addCleanup(self.preparation.doCleanups)
        fixture = self.preparation
        self.store, self.root, self.actor = fixture.store, fixture.root, fixture.actor
        # Reconciliation must never inherit the old recipient's unsafe resource
        # overrides, even when the independently managed lane values are safe.
        fixture.retained.env.update(
            DB_PORT="3307", CONFIG_VOLUME="production-config", OLD_RECIPIENT_ONLY="old"
        )
        fixture.retained.domains = ()
        retained_origin = "old-origin.example.invalid"
        if self.private_product:
            fixture.retained.domains = (retained_origin,)
            profile = fixture.fixture.profile.model_dump()
            profile["public_website"] = "none"
            for lane in profile["lanes"]:
                lane.update(base_url="", health_url="")
            fixture.fixture.profile = type(fixture.fixture.profile).model_validate(profile)
            self.store.write_product_profile_record(fixture.fixture.profile)
        self.store.write_dokploy_target_record(fixture.retained)
        self.product, self.context, self.scope = (
            fixture.product,
            fixture.context,
            self.caller_scope(),
        )
        self.seed_runtime_fixture()
        binding, _ = preparation_binding(
            self.store, fixture.product, fixture.context, fixture.actor, fixture.root
        )
        fixture.body.reviewed_plan_sha256 = binding
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=fixture.prepare,
        ):
            from control_plane.testing_separation_resources import ROUTE as preparation_route

            prepared = run_durable_provider_operation(
                store=self.store,
                scope=self.scope,
                route_path=preparation_route.format(product=self.product),
                idempotency_key="prepare",
                request_fingerprint="a" * 64,
                lease_owner="preparation-worker",
                response_trace_id="preparation-trace",
                adapter=fixture.adapter,
            )
            self.assertEqual(prepared.response_status_code, 202)
        self.request = TestingActivationRequest(
            context=self.context,
            preparation_key="prepare",
            interlock_record_sha256=target_record_sha256(fixture.retained),
            source_commit="a" * 40,
            volume_keys={"database": "DB_VOLUME", "config": "CONFIG_VOLUME"},
            port_keys=("DB_PORT", "HTTP_PORT"),
            reason="Explicitly continue the retained staging interlock onto isolated testing.",
        )
        self.payloads = {
            fixture.fixture.target.resolved_target.target_id: deepcopy(fixture.fixture.payload)
        }
        if self.private_product:
            self.payloads[fixture.fixture.target.resolved_target.target_id]["domains"] = [
                {"domainId": "retained-origin", "host": retained_origin}
            ]
        self.writes: list[tuple[str, Any]] = []
        self.projects: list[dict[str, Any]] = []
        self.inspections: list[bool] = []
        self.enterContext(
            patch(
                "control_plane.testing_activation.read_product_compose",
                return_value=json.dumps(COMPOSE),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.verified_product_image",
                return_value="ghcr.io/example/site@sha256:" + "a" * 64,
            )
        )
        self.enterContext(
            patch("control_plane.testing_activation.runtime_values", side_effect=self.values)
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.api.fetch_dokploy_target_payload",
                side_effect=lambda **kw: deepcopy(self.payloads[kw["target_id"]]),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.api.fetch_dokploy_projects",
                side_effect=lambda **kwargs: deepcopy(self.projects),
            )
        )
        self.enterContext(
            patch("control_plane.testing_activation.api.dokploy_request", side_effect=self.provider)
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.inspect_new_resources", side_effect=self.inspect
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.compose.fetch_dokploy_converted_compose_file",
                side_effect=self.converted,
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_activation.inspect_generic_web_testing_exclusion",
                return_value=type("Exclusion", (), {"status": "excluded"})(),
            )
        )
        self.adapter = TestingActivationAdapter(
            self.root,
            self.store,
            self.product,
            self.request,
            self.actor,
            self.scope,
            "launchplane_req_fixture",
        )
        record = self.adapter.current()
        self.planned_record = record
        self.request = self.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": record.plan_sha256}
        )
        self.adapter.request = self.request

    def seed_runtime_fixture(self) -> None:
        from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
        from control_plane.contracts.secret_record import SecretRecord, SecretBinding, SecretVersion
        from control_plane.contracts.runtime_key_safety_policy import (
            RuntimeKeySafetyPolicyRecord,
            RuntimeSecretSafetyRule,
        )

        stamp = self.preparation.fixture.now()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=self.context,
                instance="testing",
                env={
                    "DB_PORT": "19213",
                    "HTTP_PORT": "19212",
                    "DB_VOLUME": "lane-db",
                    "CONFIG_VOLUME": "lane-config",
                },
                updated_at=stamp,
            )
        )
        self.store.write_secret_record(
            SecretRecord(
                secret_id="testing-password",
                scope="context_instance",
                integration="runtime_environment",
                name="DB_PASSWORD",
                context=self.context,
                instance="testing",
                current_version_id="testing-version",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        self.store.write_secret_binding(
            SecretBinding(
                binding_id="testing-password",
                secret_id="testing-password",
                integration="runtime_environment",
                binding_key="DB_PASSWORD",
                context=self.context,
                instance="testing",
                declared_secret_class="testing",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        self.store.write_secret_version(
            SecretVersion(
                version_id="testing-version",
                secret_id="testing-password",
                created_at=stamp,
                ciphertext="testing-ciphertext",
            )
        )
        # Site delivery excludes global/platform secrets. Legacy all-bindings
        # safety must not accidentally pull them back into this app's plan.
        self.store.write_secret_record(
            SecretRecord(
                secret_id="global-platform-password",
                scope="global",
                integration="runtime_environment",
                name="GLOBAL_PLATFORM_PASSWORD",
                current_version_id="global-platform-version",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        self.store.write_secret_binding(
            SecretBinding(
                binding_id="global-platform-password",
                secret_id="global-platform-password",
                integration="runtime_environment",
                binding_key="GLOBAL_PLATFORM_PASSWORD",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        self.store.write_secret_version(
            SecretVersion(
                version_id="global-platform-version",
                secret_id="global-platform-password",
                created_at=stamp,
                ciphertext="global-platform-ciphertext",
            )
        )
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="testing-policy",
                source="fixture",
                updated_at=stamp,
                rules=(RuntimeSecretSafetyRule(binding_key="DB_PASSWORD", secret_class="testing"),),
            )
        )

    @staticmethod
    def caller_scope() -> str:
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.service_auth import LocalOperatorIdentity

        return idempotency_scope(
            LocalOperatorIdentity(subject="local-owner-agent", token_label="local-owner-write")
        )

    def values(self, store: Any, root: Any, record: Any, resources: Any) -> dict[str, str]:
        return {
            "DOCKER_IMAGE_REFERENCE": "ghcr.io/example/site@sha256:" + "a" * 64,
            "DB_PASSWORD": "safe-test-password",
            "DB_VOLUME": resources.volumes["database"],
            "CONFIG_VOLUME": resources.volumes["config"],
            "DB_PORT": "19213",
            "HTTP_PORT": "19212",
        }

    def inspect(self, root: Any, resources: Any, ports: Any, before_effect: Any) -> None:
        before_effect("inspect_new_resources")
        self.inspections.append(any(path == "/api/compose.update" for path, _ in self.writes))

    def converted(self, **kwargs: Any) -> str:
        payload = self.payloads[kwargs["compose_id"]]
        from control_plane.dokploy.api import parse_dokploy_env_text

        return json.dumps(
            interpolate(json.loads(payload["composeFile"]), parse_dokploy_env_text(payload["env"]))
        )

    def provider(self, **kwargs: Any) -> Any:
        path, body = kwargs["path"], kwargs.get("payload")
        assert isinstance(path, str)
        if kwargs.get("method", "GET") != "GET":
            assert isinstance(body, dict)
        if kwargs.get("method", "GET") == "GET":
            return self.preparation.fixture.responses.get(path)
        assert isinstance(body, dict)
        self.writes.append((path, deepcopy(body)))
        if path == "/api/project.create":
            self.projects.append({**body, "projectId": "new-project", "environments": []})
            return {
                "project": {**body, "projectId": "new-project"},
                "environment": {"environmentId": "default-environment"},
            }
        if path == "/api/environment.create":
            self.projects[0]["environments"].append(
                {**body, "environmentId": "new-environment", "compose": []}
            )
            return {"environmentId": "new-environment"}
        if path == "/api/compose.create":
            self.projects[0]["environments"][0]["compose"].append(
                {**body, "composeId": "new-compose"}
            )
            self.payloads["new-compose"] = {
                **body,
                "composeId": "new-compose",
                "env": "",
                "serverId": "server",
                "deployments": [],
                "composeStatus": "idle",
            }
            return {"composeId": "new-compose"}
        if path == "/api/compose.update":
            self.payloads[body["composeId"]].update(body)
            return {}
        self.fail(f"Unexpected provider effect: {path}")

    def run_activation(self, key: str = "activate") -> Any:
        return run_durable_provider_operation(
            store=self.store,
            scope=self.scope,
            route_path=ROUTE.format(product=self.product),
            idempotency_key=key,
            request_fingerprint="b" * 64,
            lease_owner="fixture-worker",
            response_trace_id="launchplane_req_fixture",
            adapter=self.adapter,
        )

    def test_activation_and_original_key_replay_preserve_production_old_target_hold_and_fence(
        self,
    ) -> None:
        production = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="prod"
        )
        old_provider = deepcopy(self.payloads)
        result = self.run_activation()
        self.assertEqual(result.response_status_code, 202)
        writes = deepcopy(self.writes)
        self.assertEqual(self.run_activation().response_status_code, 202)
        self.assertEqual(self.writes, writes)
        current = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.assertEqual(
            current.policies.staff_testing_hold,
            self.preparation.retained.policies.staff_testing_hold,
        )
        self.assertEqual(
            {int(current.env[key]) for key in self.request.port_keys},
            set(self.planned_record.host_ports),
        )
        self.assertNotIn("OLD_RECIPIENT_ONLY", current.env)
        self.assertEqual(current.domains, ())
        self.assertEqual(current.public_hosts, ())
        self.assertEqual(current.pending_public_host_effects, ())
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name=self.context, instance_name="prod"),
            production,
        )
        self.assertEqual(
            self.store.list_held_provider_target_reservations(), (self.preparation.original,)
        )
        self.assertTrue(retained_fence_is_separated(self.store, self.preparation.original))
        self.assertEqual(
            self.payloads[self.preparation.fixture.target.resolved_target.target_id],
            old_provider[self.preparation.fixture.target.resolved_target.target_id],
        )
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assertIn(False, self.inspections)
        self.assertTrue(
            self.inspections[-1], "Storage and ports must be checked after configuration"
        )
        record = activation_record(self.store, self.product, self.context)
        assert record is not None
        self.assertEqual(record.retained_target, self.preparation.retained)
        self.assertTrue(record.published)

    def check_lost_creation_reply(self, lost_path: str) -> None:
        original = self.provider

        def lost(**kwargs: Any) -> Any:
            value = original(**kwargs)
            if kwargs["path"] == lost_path:
                raise OSError("Lost creation response")
            return value

        with patch("control_plane.testing_activation.api.dokploy_request", side_effect=lost):
            self.assertEqual(self.run_activation().response_status_code, 409)
            self.assertEqual(self.run_activation("different-key").response_status_code, 409)
            self.assertEqual(self.run_activation().response_status_code, 202)
        self.assertEqual(sum(path == "/api/compose.create" for path, _ in self.writes), 1)
        self.assertEqual(sum(path == lost_path for path, _ in self.writes), 1)
        self.assertIn(
            self.preparation.original, self.store.list_held_provider_target_reservations()
        )

    def test_lost_project_creation_reply_adopts_without_duplicate(self) -> None:
        self.check_lost_creation_reply("/api/project.create")

    def test_lost_environment_creation_reply_adopts_without_duplicate(self) -> None:
        self.check_lost_creation_reply("/api/environment.create")

    def test_lost_recipient_creation_reply_adopts_without_duplicate(self) -> None:
        self.check_lost_creation_reply("/api/compose.create")

    def test_unknown_absent_creation_is_never_reissued(self) -> None:
        original = self.provider
        attempted: list[str] = []

        def unknown(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/project.create":
                attempted.append(kwargs["path"])
                raise OSError("Uncertain request; no creation visible yet")
            return original(**kwargs)

        with patch("control_plane.testing_activation.api.dokploy_request", side_effect=unknown):
            self.assertEqual(self.run_activation().response_status_code, 409)
            initial = list(attempted)
            self.assertEqual(self.run_activation().response_status_code, 409)
            self.assertEqual(attempted, initial)
        self.assertEqual(self.writes, [])
        self.preparation.assert_retained()

    def test_ambiguous_original_namespace_stays_fenced(self) -> None:
        original = self.provider

        def lost(**kwargs: Any) -> Any:
            result = original(**kwargs)
            if kwargs["path"] == "/api/project.create":
                raise OSError("Lost creation reply")
            return result

        with patch("control_plane.testing_activation.api.dokploy_request", side_effect=lost):
            self.assertEqual(self.run_activation().response_status_code, 409)
        self.projects.append({**deepcopy(self.projects[0]), "projectId": "ambiguous-project"})
        before = deepcopy(self.writes)
        self.assertEqual(self.run_activation().response_status_code, 409)
        self.assertEqual(self.writes, before)
        self.preparation.assert_retained()

    def test_configuration_not_written_can_resume_only_unchanged_unused_recipient(self) -> None:
        original = self.provider

        def before_configuration(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/compose.update":
                raise OSError("Transport failed before configuration")
            return original(**kwargs)

        with patch(
            "control_plane.testing_activation.api.dokploy_request", side_effect=before_configuration
        ):
            self.assertEqual(self.run_activation().response_status_code, 409)
        self.assertEqual(self.run_activation().response_status_code, 202)
        self.assertEqual(sum(path == "/api/compose.create" for path, _ in self.writes), 1)

    def test_unknown_port_or_storage_readback_keeps_old_recipient(self) -> None:
        with patch(
            "control_plane.testing_activation.inspect_new_resources",
            side_effect=ValueError("Storage/port overlap"),
        ):
            with self.assertRaises(ValueError):
                self.run_activation()
        self.assertEqual(self.writes, [])
        self.preparation.assert_retained()
        self.assertIsNone(activation_record(self.store, self.product, self.context))
        self.assertEqual(self.run_activation().response_status_code, 202)

    def test_interrupted_inspection_retries_without_stranding_or_duplicating_recipient(
        self,
    ) -> None:
        def interrupted(root: Any, resources: Any, ports: Any, checkpoint: Any) -> None:
            checkpoint("inspect_new_resources")
            raise OSError("Inspection reply lost")

        with patch(
            "control_plane.testing_activation.inspect_new_resources", side_effect=interrupted
        ):
            self.assertEqual(self.run_activation().response_status_code, 409)
        self.assertIsNone(activation_record(self.store, self.product, self.context))
        self.assertEqual(self.writes, [])
        self.preparation.assert_retained()
        self.assertEqual(self.run_activation().response_status_code, 202)
        self.assertEqual(sum(path == "/api/compose.create" for path, _ in self.writes), 1)

    def test_custody_prepared_before_any_creation_can_continue_original_key(self) -> None:
        self.store.write_testing_activation(
            expected=None, replacement=self.planned_record.model_dump(mode="json")
        )
        self.assertEqual(self.run_activation().response_status_code, 202)
        self.assertEqual(sum(path == "/api/compose.create" for path, _ in self.writes), 1)

    def test_declared_public_host_refuses_without_provider_effects_or_custody(self) -> None:
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target = target.model_copy(update={"public_hosts": ("testing.example.invalid",)})
        self.store.write_dokploy_target_record(target)
        self.adapter.request = self.request.model_copy(
            update={"interlock_record_sha256": target_record_sha256(target)}
        )
        with self.assertRaises(ValueError):
            self.adapter.current()
        self.assertEqual(self.writes, [])
        self.assertIsNone(activation_record(self.store, self.product, self.context))

    def test_public_product_origin_domain_requires_accepted_cutover(self) -> None:
        profile = self.store.read_product_profile_record(self.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"public_website": "required"})
        )
        with self.assertRaises(TestingActivationRefusal) as refused:
            self.adapter.current()
        self.assertEqual(
            refused.exception.code, TestingActivationRefusalCode.ROUTES_REQUIRE_CUTOVER
        )
        self.assertEqual(self.writes, [])
        self.assertIsNone(activation_record(self.store, self.product, self.context))

    def test_public_website_authority_change_after_review_refuses_before_effects(self) -> None:
        profile = self.store.read_product_profile_record(self.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"public_website": "required"})
        )
        with self.assertRaises(TestingActivationRefusal) as refused:
            self.adapter.require_unchanged(self.planned_record)
        self.assertEqual(refused.exception.code, TestingActivationRefusalCode.AUTHORITY_CHANGED)
        self.assertEqual(self.writes, [])

    def public_activation_fixture(self) -> "ActivationTests":
        public = ActivationTests()
        public.private_product = False
        if self.database_url:
            from tests.test_postgres_integration import _head_postgres_database

            database = _head_postgres_database()
            public.database_url = database.__enter__()
            self.addCleanup(database.__exit__, None, None, None)
        self.addCleanup(public.doCleanups)
        public.setUp()
        return public

    def test_new_public_product_without_routes_activates(self) -> None:
        public = self.public_activation_fixture()
        self.assertEqual(public.run_activation().response_status_code, 202)
        current = public.adapter.current()
        public.adapter.require_unchanged(current)
        public.adapter.verify_provider(current)
        self.assertTrue(current.published)

    def test_public_product_without_routes_resumes_pre_website_digest_custody(self) -> None:
        from control_plane.contracts.canonical_json import canonical_json_sha256

        public = self.public_activation_fixture()
        payload = public.planned_record.model_dump(exclude={"configuration_version"})
        payload["configuration_sha256"] = configuration_digest(
            public.store, public.product, public.context, version=1
        )
        record = TestingActivationRecord.model_validate(payload)
        record.plan_sha256 = canonical_json_sha256(record.model_dump(exclude={"plan_sha256"}))
        public.store.write_testing_activation(
            expected=None, replacement=record.model_dump(mode="json")
        )
        public.adapter.request = public.request.model_copy(
            update={"reviewed_plan_sha256": record.plan_sha256}
        )
        self.assertEqual(public.run_activation().response_status_code, 202)
        current = public.adapter.current()
        public.adapter.require_unchanged(current)
        public.adapter.verify_provider(current)
        self.assertTrue(current.published)

    def test_configuration_drift_after_effect_keeps_fences_and_never_publishes(self) -> None:
        original = self.inspect

        def drift(*args: Any) -> None:
            original(*args)
            target = self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            )
            target.env["CHANGED"] = "new authority"
            self.store.write_dokploy_target_record(target)

        with patch("control_plane.testing_activation.inspect_new_resources", side_effect=drift):
            self.assertEqual(self.run_activation().response_status_code, 409)
        self.assertEqual(self.writes, [])
        self.assertIn(
            self.preparation.original, self.store.list_held_provider_target_reservations()
        )

    def test_plain_repoint_cannot_hide_an_old_incident_or_release_its_lane_fence(self) -> None:
        self.assertEqual(self.run_activation().response_status_code, 202)
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target_id.model_copy(update={"target_id": "unproved-replacement"})
        )
        self.assertFalse(retained_fence_is_separated(self.store, self.preparation.original))

    def test_real_environment_composition_agrees_before_and_after_publication(self) -> None:
        from control_plane.dokploy.api import parse_dokploy_env_text
        from control_plane.testing_activation import require_activation_hold_lift
        from control_plane.testing_lane_hold import TestingHoldApplyRequest
        from control_plane.workflows.generic_web_deploy_provider import (
            _resolve_store_runtime_environment_values,
        )

        with (
            patch(
                "control_plane.testing_activation.runtime_values",
                side_effect=resolve_activation_runtime,
            ),
            patch(
                "control_plane.secrets._decrypt_secret_value",
                side_effect=lambda ciphertext, key_id: (
                    "safe-test-password"
                    if ciphertext == "testing-ciphertext"
                    else self.fail("Activation must never decrypt unrelated global credentials")
                ),
            ),
        ):
            self.assertEqual(self.run_activation().response_status_code, 202)
            record = activation_record(self.store, self.product, self.context)
            assert record is not None
            self.adapter.verify_provider(record)
            self.assertTrue(
                require_activation_hold_lift(
                    self.store,
                    TestingHoldApplyRequest(
                        product=self.product,
                        context=self.context,
                        instance="testing",
                        hold=False,
                        reason="Separation proven",
                        activation_plan_sha256=record.plan_sha256,
                    ),
                    self.actor,
                    self.root,
                )
            )
            target = self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            )
            with patch(
                "control_plane.workflows.generic_web_deploy_provider.control_plane_secrets.overlay_runtime_environment_secret_values_from_store",
                side_effect=lambda **kwargs: (
                    kwargs["environment_values"] | {"DB_PASSWORD": "safe-test-password"}
                ),
            ):
                deployed_values = _resolve_store_runtime_environment_values(
                    record_store=self.store,
                    context_name=self.context,
                    instance_name="testing",
                    target_environment=target.env,
                )
            configured_values = parse_dokploy_env_text(self.payloads[record.target_id]["env"])
            configured_values.pop("DOCKER_IMAGE_REFERENCE")
            self.assertEqual(configured_values, deployed_values)
            self.assertNotIn("OLD_RECIPIENT_ONLY", configured_values)

    def test_real_exclusion_reuses_the_held_old_executor_guard(self) -> None:
        from control_plane.generic_web_testing_exclusion import (
            inspect_generic_web_testing_exclusion,
        )

        with patch(
            "control_plane.testing_activation.inspect_generic_web_testing_exclusion",
            side_effect=inspect_generic_web_testing_exclusion,
        ):
            self.assertEqual(self.run_activation().response_status_code, 202)

    def test_retired_volume_setting_cannot_be_resurrected_by_activation(self) -> None:
        from control_plane.testing_activation import preparation_resources

        (row,) = self.store.list_runtime_environment_records(
            scope="instance", context_name=self.context, instance_name="testing"
        )
        env = dict(row.env)
        key = self.request.volume_keys["database"]
        env.pop(key)
        self.store.write_runtime_environment_record(
            row.model_copy(
                update={
                    "schema_version": 2,
                    "env": env,
                    "retired_provider_keys": (key,),
                }
            )
        )
        resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
        with patch(
            "control_plane.runtime_environments.control_plane_secrets.resolve_site_secret_values",
            return_value={"DB_PASSWORD": "safe-test-password"},
        ):
            with self.assertRaises(ValueError):
                resolve_activation_runtime(self.store, self.root, self.planned_record, resources)
        self.assertEqual(self.writes, [])

    def test_lost_configuration_reply_adopts_only_the_original_complete_render(self) -> None:
        original = self.provider

        def lost(**kwargs: Any) -> Any:
            value = original(**kwargs)
            if kwargs["path"] == "/api/compose.update":
                raise OSError("Lost response after configuration persisted")
            return value

        with patch("control_plane.testing_activation.api.dokploy_request", side_effect=lost):
            self.assertEqual(self.run_activation().response_status_code, 409)
            self.assertEqual(self.run_activation().response_status_code, 202)
        self.assertEqual(sum(path == "/api/compose.create" for path, _ in self.writes), 1)
        self.assertEqual(sum(path == "/api/compose.update" for path, _ in self.writes), 1)
        self.assertIn(
            self.preparation.original, self.store.list_held_provider_target_reservations()
        )

    def test_unsafe_or_production_equal_carried_key_refuses_without_provider_writes(self) -> None:
        from control_plane.contracts.secret_record import SecretBinding, SecretRecord
        from control_plane.contracts.runtime_key_safety_policy import (
            RuntimeKeySafetyPolicyRecord,
            RuntimeSecretSafetyRule,
        )
        from control_plane.runtime_environments import SiteRuntimeEnvironment
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from control_plane.testing_activation import preparation_resources

        profile = self.store.read_product_profile_record(self.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": (
                        *profile.lanes,
                        profile.lanes[0].model_copy(
                            update={"instance": "prod", "context": self.context}
                        ),
                    )
                }
            )
        )

        now = self.preparation.fixture.now()
        self.store.write_secret_record(
            SecretRecord(
                secret_id="testing-password",
                scope="context_instance",
                integration="runtime_environment",
                name="DB_PASSWORD",
                context=self.context,
                instance="testing",
                current_version_id="testing-version",
                created_at=now,
                updated_at=now,
            )
        )
        binding = SecretBinding(
            binding_id="testing-password",
            secret_id="testing-password",
            integration="runtime_environment",
            binding_key="DB_PASSWORD",
            context=self.context,
            instance="testing",
            declared_secret_class="testing",
            created_at=now,
            updated_at=now,
        )
        self.store.write_secret_binding(binding)
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="testing-policy",
                source="fixture",
                updated_at=now,
                rules=(RuntimeSecretSafetyRule(binding_key="DB_PASSWORD", secret_class="testing"),),
            )
        )
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=self.context,
                instance="prod",
                provider_id="dokploy",
                target_category="compose",
                target_id="production-compose",
                display_name="production",
                updated_at=now,
            )
        )
        self.payloads["production-compose"] = {
            "composeId": "production-compose",
            "env": "DB_PASSWORD=production-password",
        }
        resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
        for value, declared, passes in (
            ("independent-password", "testing", True),
            ("production-password", "testing", False),
            ("independent-password", "prod_only", False),
        ):
            self.store.write_secret_binding(
                binding.model_copy(update={"declared_secret_class": declared})
            )
            resolved = SiteRuntimeEnvironment(
                values={"DB_PASSWORD": value}, secret_keys=frozenset({"DB_PASSWORD"})
            )
            with patch(
                "control_plane.testing_activation.runtime_environments.resolve_site_runtime_environment",
                return_value=resolved,
            ):
                if passes:
                    result = resolve_activation_runtime(
                        self.store, self.root, self.planned_record, resources
                    )
                    self.assertEqual(result["DB_PASSWORD"], value)
                else:
                    with self.assertRaises(ValueError):
                        resolve_activation_runtime(
                            self.store, self.root, self.planned_record, resources
                        )
        self.assertEqual(self.writes, [])

    def test_hold_continuation_requires_exact_caller_and_preserves_later_staff_hold(self) -> None:
        from control_plane.testing_activation import require_activation_hold_lift
        from control_plane.testing_lane_hold import TestingHoldApplyRequest
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        self.assertEqual(self.run_activation().response_status_code, 202)
        request = TestingHoldApplyRequest(
            product=self.product,
            context=self.context,
            instance="testing",
            hold=False,
            reason="Lift only the now-satisfied interlock.",
            activation_plan_sha256=self.request.reviewed_plan_sha256,
        )
        self.assertTrue(require_activation_hold_lift(self.store, request, self.actor, self.root))
        with self.assertRaises(ValueError):
            require_activation_hold_lift(self.store, request, "foreign-agent", self.root)
        current = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        current.policies.staff_testing_hold = DokployTargetStaffTestingHold(
            reason="Staff began a real test.", recorded_by=self.actor
        )
        self.store.write_dokploy_target_record(current)
        with self.assertRaises(ValueError):
            require_activation_hold_lift(self.store, request, self.actor, self.root)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).policies.staff_testing_hold,
            current.policies.staff_testing_hold,
        )

    def test_unrelated_profile_edits_do_not_strand_the_activation_lift(self) -> None:
        from control_plane.testing_activation import require_activation_hold_lift
        from control_plane.testing_lane_hold import TestingHoldApplyRequest

        self.assertEqual(self.run_activation().response_status_code, 202)
        profile = self.store.read_product_profile_record(self.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"display_name": "Renamed product"})
        )
        request = TestingHoldApplyRequest(
            product=self.product,
            context=self.context,
            instance="testing",
            hold=False,
            reason="Separation proven",
            activation_plan_sha256=self.request.reviewed_plan_sha256,
        )
        self.assertTrue(require_activation_hold_lift(self.store, request, self.actor, self.root))

    def test_safe_target_edits_keep_old_fence_attributed_but_resource_drift_does_not(self) -> None:
        self.assertEqual(self.run_activation().response_status_code, 202)
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.deploy_timeout_seconds = 123
        self.store.write_dokploy_target_record(target)
        self.assertTrue(retained_fence_is_separated(self.store, self.preparation.original))
        target.env[self.request.port_keys[0]] = "3307"
        self.store.write_dokploy_target_record(target)
        self.assertFalse(retained_fence_is_separated(self.store, self.preparation.original))

    def test_supported_http_lift_replays_and_requests_only_testing_reconcile(self) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.testing_lane_hold import TESTING_HOLD_APPLY_ROUTE
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request
        from control_plane.workflows.public_ingress_monitor import _held_generic_web_deploy_fences

        production = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="prod"
        )
        self.assertEqual(self.run_activation().response_status_code, 202)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=activation_hold_policy(self.product, self.context),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        body = {
            "product": self.product,
            "context": self.context,
            "instance": "testing",
            "hold": False,
            "purpose": "staff_testing",
            "reason": "Separation proven",
            "activation_plan_sha256": self.request.reviewed_plan_sha256,
            "mode": "dry-run",
        }
        refusal = TestingActivationRefusal(
            "private diagnostic must stay redacted",
            code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
        )
        with patch(
            "control_plane.testing_activation.require_activation_hold_lift", side_effect=refusal
        ):
            refused = asyncio.run(
                request(
                    app,
                    "POST",
                    TESTING_HOLD_APPLY_ROUTE,
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload=body,
                )
            )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], refusal.public_code)
        self.assertNotIn(str(refusal), refused.text)

        async def post(payload: Any, key: str = "") -> Any:
            return await request(
                app,
                "POST",
                TESTING_HOLD_APPLY_ROUTE,
                headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
                payload=payload,
            )

        planned = asyncio.run(post(body))
        self.assertEqual(planned.status_code, 202, planned.text)
        applied_body = body | {
            "mode": "apply",
            "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"],
        }
        with patch.object(
            self.store,
            "request_product_reconcile",
            side_effect=OSError("Receipt lost after hold clear"),
        ):
            with self.assertRaises(OSError):
                asyncio.run(post(applied_body, "lift-qualified-testing"))
        self.assertIsNone(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).policies.staff_testing_hold
        )
        applied = asyncio.run(post(applied_body, "lift-qualified-testing"))
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertTrue(applied.json()["result"]["reconcile_requested"])
        replayed = asyncio.run(post(applied_body, "lift-qualified-testing"))
        self.assertEqual(replayed.status_code, 202, replayed.text)
        self.assertTrue(replayed.json()["replayed"])
        self.assertEqual(
            [row.target_key for row in self.store.list_product_reconcile_requests()],
            [self.reconcile_target_key()],
        )
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name=self.context, instance_name="prod"),
            production,
        )
        self.assertEqual(
            self.store.list_held_provider_target_reservations(), (self.preparation.original,)
        )
        self.assertTrue(retained_fence_is_separated(self.store, self.preparation.original))
        self.assertNotIn(
            (self.context, "testing"), _held_generic_web_deploy_fences(self.store) or {}
        )
        from control_plane.workflows.generic_web_deploy_provider import (
            build_generic_web_provider_reconciliation_key,
        )

        target = self.preparation.fixture.target.model_copy(deep=True)
        record = activation_record(self.store, self.product, self.context)
        assert record is not None
        target.resolved_target.target_id = record.target_id
        current_fence = self.preparation.original.model_copy(
            update={
                "idempotency_key": "new-recipient-outcome-unknown",
                "reconciliation_key": build_generic_web_provider_reconciliation_key(
                    target, product=self.product
                ),
            }
        )
        with patch.object(
            self.store,
            "list_held_provider_target_reservations",
            return_value=(self.preparation.original, current_fence),
        ):
            self.assertEqual(
                (_held_generic_web_deploy_fences(self.store) or {})[(self.context, "testing")],
                current_fence,
            )

    def reconcile_target_key(self) -> str:
        from control_plane.contracts.product_reconcile import ProductReconcileTarget

        return ProductReconcileTarget(product=self.product, target_kind="testing").target_key

    def test_supported_route_rejects_production_and_preserves_original_key_receipt(self) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        draft = self.request.model_copy(
            update={"mode": "dry-run", "reviewed_plan_sha256": ""}
        ).model_dump(mode="json")

        async def post(body: Any, key: str = "") -> Any:
            return await request(
                app,
                "POST",
                ROUTE.format(product=self.product),
                headers={
                    "Authorization": "Bearer local-operator-token",
                    **({"Idempotency-Key": key} if key else {}),
                },
                payload=body,
            )

        for instance in ("prod", "preview-1"):
            response = asyncio.run(post({**draft, "instance": instance}))
            self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.writes, [])
        planned = asyncio.run(post(draft))
        self.assertEqual(planned.status_code, 202, planned.text)
        self.assertEqual(planned.json()["result"]["plan_sha256"], self.request.reviewed_plan_sha256)
        key = "activate-http"
        applied = asyncio.run(post(self.request.model_dump(mode="json"), key))
        if self.store.database_dialect_name == "postgresql":
            self.assertEqual(applied.status_code, 202, applied.text)
        else:
            self.assertEqual(applied.status_code, 503, applied.text)
            key = "activate"
            self.assertEqual(self.run_activation().response_status_code, 202)
        receipt = asyncio.run(post({**draft, "mode": "receipt"}, key))
        self.assertEqual(receipt.status_code, 202, receipt.text)
        self.assertTrue(receipt.json()["result"]["separation_verified"])
        # Receipt retrieval remains available after configuration drift.
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.env["CHANGED_AFTER_ACTIVATION"] = "fixture"
        self.store.write_dokploy_target_record(target)
        repeated = asyncio.run(post({**draft, "mode": "receipt"}, key))
        self.assertEqual(repeated.status_code, 202, repeated.text)
        readback = asyncio.run(post({**draft, "mode": "readback"}))
        self.assertEqual(readback.status_code, 409, readback.text)

    def test_route_reports_real_carried_key_refusal_before_provider_effects(self) -> None:
        import asyncio
        from control_plane.testing_activation import preparation_resources
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.contracts.runtime_key_safety_policy import RuntimeSecretSafetyRule
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        policy = self.store.list_runtime_key_safety_policy_records(status="active", limit=1)[0]
        self.store.write_runtime_key_safety_policy_record(
            policy.model_copy(
                update={
                    "rules": (
                        RuntimeSecretSafetyRule(
                            binding_key="DB_PASSWORD", secret_class="prod_only"
                        ),
                    )
                }
            )
        )
        resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        with (
            patch.object(
                TestingActivationAdapter,
                "current",
                side_effect=lambda: resolve_activation_runtime(
                    self.store, self.root, self.planned_record, resources
                ),
            ),
            patch("control_plane.secrets._decrypt_secret_value", return_value="safe-test-password"),
        ):
            response = asyncio.run(
                request(
                    app,
                    "POST",
                    ROUTE.format(product=self.product),
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload=self.request.model_copy(
                        update={"mode": "dry-run", "reviewed_plan_sha256": ""}
                    ).model_dump(mode="json"),
                )
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["error"]["code"],
            TestingActivationRefusalCode.CARRIED_KEYS_UNVERIFIED.value,
        )
        self.assertEqual(self.writes, [])
        self.assertIsNone(activation_record(self.store, self.product, self.context))
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            self.preparation.retained,
        )

    def test_route_names_fixed_refusals_without_exposing_untrusted_error_values(self) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        body = self.request.model_copy(
            update={"mode": "dry-run", "reviewed_plan_sha256": ""}
        ).model_dump(mode="json")
        secret = "provider-error-with-private-credential-value"
        refusal = TestingActivationRefusal(
            secret, code=TestingActivationRefusalCode.ROUTES_REQUIRE_CUTOVER
        )
        modes = ["dry-run"]
        if self.store.database_dialect_name == "postgresql":
            modes.append("apply")
        for mode in modes:
            for index, error in enumerate((refusal, ValueError(secret))):
                payload = body if mode == "dry-run" else self.request.model_dump(mode="json")
                with patch.object(TestingActivationAdapter, "current", side_effect=error):
                    response = asyncio.run(
                        request(
                            app,
                            "POST",
                            ROUTE.format(product=self.product),
                            headers={
                                "Authorization": "Bearer local-operator-token",
                                "Idempotency-Key": f"refusal-{mode}-{index}",
                            },
                            payload=payload,
                        )
                    )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertNotIn(secret, response.text)
                if error is refusal:
                    self.assertEqual(response.json()["error"]["code"], refusal.public_code)
        self.assertEqual(self.writes, [])


class ActivationPortProgramTests(unittest.TestCase):
    def test_port_proof_survives_the_real_schedule_log_transport(self) -> None:
        from pathlib import Path
        from control_plane.odoo_testing_resources import GenericWebTestingResources
        from control_plane.testing_activation import inspect_new_resources

        resources = GenericWebTestingResources(
            product="fixture-product",
            context="fixture-context",
            operation_id="fixture-operation",
            target_id="fixture-target",
            namespace="lp-testing-" + "a" * 32,
        )
        port = 19212
        _, successful_output = self.run_program([port], [])
        _, refused_output = self.run_program([port], [], occupied=True)
        cases = (
            ("available", successful_output, True),
            ("occupied", refused_output, False),
            ("missing", "", False),
            ("malformed", successful_output.replace("=true", "=unproved"), False),
        )
        for name, port_output, accepted in cases:
            with (
                self.subTest(name=name),
                patch(
                    "control_plane.testing_activation.source.read_dokploy_config",
                    return_value=("https://provider.example.invalid", "fixture-token"),
                ),
                patch(
                    "control_plane.testing_activation.api.fetch_dokploy_target_payload",
                    return_value={"composeId": resources.target_id},
                ),
                patch(
                    "control_plane.testing_activation.post_deploy._resolve_dokploy_schedule_runtime",
                    return_value=(
                        "dokploy-server",
                        "fixture-user",
                        "fixture-app",
                        "fixture-server",
                    ),
                ),
                patch(
                    "control_plane.dokploy.api.upsert_dokploy_schedule",
                    return_value={"scheduleId": "fixture-schedule"},
                ),
                patch(
                    "control_plane.dokploy.api.run_dokploy_schedule",
                    return_value="deployment=fixture-deployment status=done",
                ),
                patch(
                    "control_plane.dokploy.api.latest_deployment_for_schedule",
                    return_value={
                        "deploymentId": "fixture-deployment",
                        "logs": [
                            "testing_resources_ok=true",
                            "testing_resources_binding_sha256=" + resources.binding_sha256,
                            *port_output.splitlines(),
                        ],
                    },
                ),
                patch(
                    "control_plane.dokploy.api.dokploy_request",
                    side_effect=AssertionError("Unmodeled provider call"),
                ),
            ):
                if accepted:
                    inspect_new_resources(Path("."), resources, {port}, lambda _phase: None)
                else:
                    with self.assertRaises(TestingActivationRefusal):
                        inspect_new_resources(Path("."), resources, {port}, lambda _phase: None)

    def run_program(
        self, ports: list[int], containers: list[dict[str, Any]], *, occupied: bool = False
    ) -> tuple[int, str]:
        from contextlib import redirect_stdout
        import io
        import subprocess
        import sys
        from control_plane.testing_activation import PORT_INSPECTION_PROGRAM
        from unittest.mock import Mock

        listener = Mock()
        if occupied:
            listener.bind.side_effect = OSError("Address already in use")

        def docker(command: list[str], **kwargs: Any) -> Any:
            if command == ["docker", "ps", "-aq"]:
                output = "\n".join(str(index) for index in range(len(containers)))
            elif command[:2] == ["docker", "inspect"]:
                output = json.dumps([containers[int(command[2])]])
            else:
                self.fail(f"Unexpected host mutation: {command}")
            return subprocess.CompletedProcess(command, 0, stdout=output)

        output = io.StringIO()
        encoded = base64.b64encode(json.dumps(ports).encode()).decode()
        status = 0
        with (
            patch.object(sys, "argv", ["port-inspection", encoded]),
            patch("subprocess.run", side_effect=docker),
            patch("socket.socket", return_value=listener),
            redirect_stdout(output),
        ):
            try:
                exec(compile(PORT_INSPECTION_PROGRAM, "<testing-port-inspection>", "exec"), {})
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
        if listener.bind.called:
            listener.close.assert_called()
        return status, output.getvalue()

    def test_listener_refusal_and_stopped_container_port_are_protected(self) -> None:
        port = 19212
        self.assertNotEqual(self.run_program([port], [], occupied=True)[0], 0)
        stopped: dict[str, Any] = {
            "State": {"Running": False},
            "HostConfig": {"PortBindings": {"3306/tcp": [{"HostPort": str(port)}]}},
        }
        self.assertNotEqual(self.run_program([port], [stopped])[0], 0)
        self.assertEqual(
            stopped["HostConfig"]["PortBindings"]["3306/tcp"][0]["HostPort"], str(port)
        )
        status, output = self.run_program([port], [])
        self.assertEqual(status, 0, output)
