from __future__ import annotations

import json
import os
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyEvaluation,
    RuntimeKeySafetyPolicyRecord,
    RuntimeKeySafetyTarget,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_config_service import apply_product_config_service_request
from control_plane.runtime_key_safety import evaluate_runtime_key_safety
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _asgi_get,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _post_product_environment_config_apply,
    _RejectingVerifier,
)
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _sqlite_database_url


class TestingSecretEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_entry_rotation_and_replay_preserve_production_and_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            with (
                closing(
                    PostgresRecordStore(database_url=_sqlite_database_url(root / "records.sqlite3"))
                ) as store,
                patch.dict(
                    os.environ,
                    {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "fake-test-root"},
                    clear=True,
                ),
            ):
                store.ensure_schema()
                profile_payload = _generic_site_profile_payload()
                profile_payload["expected_config"] = {
                    "managed_secret_bindings": [
                        {
                            "binding_key": key,
                            "integration": "runtime_environment",
                            "context": "example-site",
                            "instance": instance,
                        }
                        for instance in ("testing", "prod")
                        for key in ("DISCORD_TOKEN", "AGENT_SESSION_TOKEN")
                    ]
                }
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(profile_payload)
                )
                policy = RuntimeKeySafetyPolicyRecord(
                    record_id="test-policy",
                    source="test",
                    updated_at="2026-10-15T00:00:00Z",
                    rules=(
                        RuntimeSecretSafetyRule(
                            binding_key="DISCORD_TOKEN",
                            secret_class="prod_only",
                            allowed_contexts=("example-site",),
                            allowed_instances=("prod",),
                        ),
                    ),
                )
                store.write_runtime_key_safety_policy_record(policy)
                result, error = apply_product_config_service_request(
                    record_store=store,
                    payload={
                        "product": "example-site",
                        "context": "example-site",
                        "instance": "prod",
                        "secrets": [
                            {
                                "binding_key": "DISCORD_TOKEN",
                                "value": "fake-production-token",
                                "secret_class": "prod_only",
                            }
                        ],
                    },
                    mode="apply",
                    actor="test",
                    source_label="test",
                )
                self.assertIsNone(error)
                self.assertIsNotNone(result)
                production_records = store.list_secret_records(instance_name="prod")
                production_bindings = store.list_secret_bindings(instance_name="prod")
                production_versions = store.list_secret_versions(
                    secret_id=production_records[0].secret_id
                )
                sessions = HumanSessionManager(
                    config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
                )
                session = sessions.issue(_github_human_identity())

                def browser_headers() -> dict[str, str]:
                    return _browser_mutation_headers(sessions, session)

                app = create_launchplane_fastapi_app(
                    verifier=_RejectingVerifier(),
                    authz_policy=_github_human_product_config_policy(
                        actions=(
                            "product_environment.read",
                            "product_config.plan",
                            "product_config.apply",
                        ),
                        product="example-site",
                        context="example-site",
                        instances=("testing",),
                        schema_version=2,
                    ),
                    record_store_factory=lambda: store,
                    human_session_manager=sessions,
                )
                status = await _asgi_get(
                    app,
                    "/v1/products/example-site/environments/testing/config-status",
                    headers=browser_headers(),
                )
                self.assertEqual(status.status_code, 200, status.json())
                config = status.json()["config_status"]
                self.assertTrue(config["write_availability"]["managed_secrets"]["plan"]["enabled"])
                self.assertEqual(
                    {item["status"] for item in config["managed_secrets"]}, {"missing"}
                )

                for revision in ("first", "rotated"):
                    request: dict[str, object] = {
                        "mode": "dry-run",
                        "reason": "Isolated testing credentials",
                        "managed_secrets": [
                            {
                                "integration": "runtime_environment",
                                "binding_key": key,
                                "value": f"fake-{revision}-{key}",
                            }
                            for key in ("DISCORD_TOKEN", "AGENT_SESSION_TOKEN")
                        ],
                    }
                    before_records = store.list_secret_records()
                    before_bindings = store.list_secret_bindings()
                    plan = await _post_product_environment_config_apply(
                        app,
                        request,
                        environment="testing",
                        authorization="",
                        headers=browser_headers(),
                        idempotency_key=f"{revision}-plan",
                    )
                    self.assertEqual(plan.status_code, 202, plan.json())
                    self.assertEqual(store.list_secret_records(), before_records)
                    self.assertEqual(store.list_secret_bindings(), before_bindings)
                    request.update(mode="apply", confirmation="APPLY example-site/testing")
                    applied = await _post_product_environment_config_apply(
                        app,
                        request,
                        environment="testing",
                        authorization="",
                        headers=browser_headers(),
                        idempotency_key=f"{revision}-apply",
                    )
                    self.assertEqual(applied.status_code, 202, applied.json())
                    versions = {
                        record.secret_id: store.list_secret_versions(secret_id=record.secret_id)
                        for record in store.list_secret_records(instance_name="testing")
                    }
                    replay = await _post_product_environment_config_apply(
                        app,
                        request,
                        environment="testing",
                        authorization="",
                        headers=browser_headers(),
                        idempotency_key=f"{revision}-apply",
                    )
                    self.assertEqual(replay.status_code, 202, replay.json())
                    self.assertTrue(replay.json()["replayed"])
                    for secret_id, saved_versions in versions.items():
                        self.assertEqual(
                            store.list_secret_versions(secret_id=secret_id), saved_versions
                        )
                    for response in (plan, applied, replay):
                        for key in ("DISCORD_TOKEN", "AGENT_SESSION_TOKEN"):
                            self.assertNotIn(f"fake-{revision}-{key}", json.dumps(response.json()))
                    bindings = store.list_secret_bindings(instance_name="testing")
                    self.assertEqual(
                        {binding.binding_key for binding in bindings},
                        {"DISCORD_TOKEN", "AGENT_SESSION_TOKEN"},
                    )
                    self.assertEqual(
                        {binding.declared_secret_class for binding in bindings}, {"testing"}
                    )
                    self.assertEqual(
                        {(binding.context, binding.instance) for binding in bindings},
                        {("example-site", "testing")},
                    )

                self.assertEqual(
                    store.list_secret_records(instance_name="prod"), production_records
                )
                self.assertEqual(
                    store.list_secret_bindings(instance_name="prod"), production_bindings
                )
                self.assertEqual(
                    store.list_secret_versions(secret_id=production_records[0].secret_id),
                    production_versions,
                )
                self.assertEqual(store.list_runtime_key_safety_policy_records(), (policy,))


class IsolatedBindingClassificationTests(unittest.TestCase):
    def test_explicit_test_binding_is_independent_of_production_key_name_rule(self) -> None:
        binding = self._binding()
        evaluation = self._evaluate(binding)
        self.assertEqual(evaluation.status, "pass")

    def test_production_foreign_shared_and_preview_bindings_still_fail(self) -> None:
        for changes, instance, environment_class, expected_code in (
            (
                {"instance": "prod", "declared_secret_class": "prod_only"},
                "testing",
                "testing",
                "binding_missing",
            ),
            ({"context": "other-site"}, "testing", "testing", "binding_missing"),
            (
                {"context": "", "instance": "", "declared_secret_class": None},
                "testing",
                "testing",
                "secret_class_not_allowed",
            ),
            (
                {"instance": "", "declared_secret_class": None},
                "testing",
                "testing",
                "secret_class_not_allowed",
            ),
            ({"instance": "pr-1"}, "pr-1", "preview", "secret_class_not_allowed"),
            ({"instance": "prod"}, "prod", "prod", "secret_class_not_allowed"),
            ({"declared_secret_class": None}, "testing", "testing", "secret_class_not_allowed"),
            (
                {"declared_secret_class": "prod_only"},
                "testing",
                "testing",
                "secret_class_not_allowed",
            ),
            (
                {"declared_secret_class": "shared_safe"},
                "testing",
                "testing",
                "secret_class_not_allowed",
            ),
        ):
            with self.subTest(changes=changes, target=instance):
                target = RuntimeKeySafetyTarget.model_validate(
                    {
                        "context": "example-site",
                        "instance": instance,
                        "environment_class": environment_class,
                    }
                )
                evaluation = self._evaluate(
                    self._binding().model_copy(update=changes), target=target
                )
                self.assertEqual(evaluation.status, "fail")
                self.assertIn(expected_code, {finding.code for finding in evaluation.findings})

    @staticmethod
    def _binding() -> SecretBinding:
        return SecretBinding(
            binding_id="test-binding",
            secret_id="test-secret",
            integration="runtime_environment",
            binding_key="DISCORD_TOKEN",
            context="example-site",
            instance="testing",
            declared_secret_class="testing",
            created_at="2026-10-15T00:00:00Z",
            updated_at="2026-10-15T00:00:00Z",
        )

    @staticmethod
    def _evaluate(
        binding: SecretBinding, *, target: RuntimeKeySafetyTarget | None = None
    ) -> RuntimeKeySafetyEvaluation:
        return evaluate_runtime_key_safety(
            target=target
            or RuntimeKeySafetyTarget(
                context="example-site", instance="testing", environment_class="testing"
            ),
            required_binding_keys=("DISCORD_TOKEN",),
            secret_bindings=(binding,),
            secret_rules=(
                RuntimeSecretSafetyRule(
                    binding_key="DISCORD_TOKEN",
                    secret_class="prod_only",
                    allowed_contexts=("example-site",),
                    allowed_instances=("prod",),
                ),
            ),
        )
