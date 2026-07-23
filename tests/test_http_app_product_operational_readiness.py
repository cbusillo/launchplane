from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _identity, _StubVerifier
from tests.support.product_reads import _get_product_operational_readiness
from tests.support.stores import _sqlite_database_url


_ACTION = "odoo_target_replacement_plan.read"
_WORKFLOW_REF = "example/example-odoo/.github/workflows/replace.yml@refs/heads/main"
_JOB_WORKFLOW_REF = (
    "example/launchplane/.github/workflows/reusable-target-replacement.yml@"
    "0123456789abcdef0123456789abcdef01234567"
)


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord.model_validate(
        {
            "product": "example-odoo",
            "display_name": "Example Odoo",
            "repository": "example/example-odoo",
            "driver_id": "odoo",
            "image": {"repository": "ghcr.io/example/example-odoo"},
            "lanes": [
                {
                    "instance": "testing",
                    "context": "example-odoo",
                    "base_url": "https://example-odoo.invalid",
                    "health_url": "https://example-odoo.invalid/launchplane/health",
                }
            ],
            "preview": {"enabled": False},
            "expected_config": {
                "runtime_environment_keys": [
                    {
                        "key": "ODOO_DB_NAME",
                        "context": "example-odoo",
                        "instance": "testing",
                    }
                ],
                "managed_secret_bindings": [
                    {
                        "binding_key": "ODOO_DB_PASSWORD",
                        "context": "example-odoo",
                        "instance": "testing",
                    }
                ],
            },
            "updated_at": "2026-07-23T09:00:00Z",
            "source": "test",
        }
    )


def _identity_for_readiness() -> GitHubActionsIdentity:
    return _identity(
        repository="example/example-odoo",
        workflow_ref=_WORKFLOW_REF,
        job_workflow_ref=_JOB_WORKFLOW_REF,
        event_name="workflow_dispatch",
        environment="testing",
        repository_id="1001",
        repository_owner_id="1000",
    )


def _policy(*, include_read: bool = True) -> LaunchplaneAuthzPolicy:
    rules: list[dict[str, object]] = [
        {
            "managed_set_id": "test.operational-readiness",
            "managed_rule_id": "example-odoo.testing.replacement-plan",
            "repository": "example/example-odoo",
            "repository_id": "1001",
            "repository_owner_id": "1000",
            "workflow_refs": [_WORKFLOW_REF],
            "job_workflow_refs": [_JOB_WORKFLOW_REF],
            "event_names": ["workflow_dispatch"],
            "products": ["example-odoo"],
            "contexts": ["example-odoo"],
            "instances": ["testing"],
            "actions": [_ACTION],
        }
    ]
    if include_read:
        rules.append(
            {
                "managed_set_id": "test.operational-readiness",
                "managed_rule_id": "example-odoo.testing.readiness-read",
                "repository": "example/example-odoo",
                "repository_id": "1001",
                "repository_owner_id": "1000",
                "workflow_refs": [_WORKFLOW_REF],
                "job_workflow_refs": [_JOB_WORKFLOW_REF],
                "event_names": ["workflow_dispatch"],
                "products": ["example-odoo"],
                "contexts": ["example-odoo"],
                "instances": ["testing"],
                "actions": ["product_environment.read"],
            }
        )
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": rules,
        }
    )


class FastApiProductOperationalReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_reports_exact_authorization_and_missing_records_without_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            seed_store = PostgresRecordStore(database_url=database_url)
            seed_store.ensure_schema()
            seed_store.write_product_profile_record(_profile())
            seed_store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="launchplane-authz-policy-operational-readiness",
                    source="test",
                    updated_at="2026-07-23T09:01:00Z",
                    policy=_policy(),
                )
            )
            seed_store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity_for_readiness()),
                authz_policy=_policy(),
                record_store_factory=lambda: app_store,
            )

            response = await _get_product_operational_readiness(app)
            deployments = app_store.list_deployment_records(
                context_name="example-odoo",
                instance_name="testing",
            )
            route_bindings = app_store.list_route_binding_records(
                product="example-odoo",
                context_name="example-odoo",
                instance_name="testing",
            )
            app_store.close()

        self.assertEqual(response.status_code, 200)
        readiness = response.json()["readiness"]
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["state"], "blocked")
        states = {
            dimension["dimension"]: dimension["state"] for dimension in readiness["dimensions"]
        }
        self.assertEqual(states["authorization"], "ready")
        self.assertEqual(states["provider_target"], "missing")
        self.assertEqual(states["route_binding"], "missing")
        self.assertEqual(states["runtime_environment"], "missing")
        self.assertEqual(states["managed_secrets"], "missing")
        self.assertEqual(states["artifact"], "missing")
        self.assertEqual(deployments, ())
        self.assertEqual(route_bindings, ())
        response_text = json.dumps(response.json())
        self.assertNotIn("subject", response_text)
        self.assertNotIn("secret_id", response_text)
        self.assertNotIn("provider_evidence", response_text)

    async def test_readiness_requires_exact_instance_read_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            seed_store = PostgresRecordStore(database_url=database_url)
            seed_store.ensure_schema()
            seed_store.write_product_profile_record(_profile())
            seed_store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity_for_readiness()),
                authz_policy=_policy(include_read=False),
                record_store_factory=lambda: app_store,
            )

            response = await _get_product_operational_readiness(app)
            app_store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_readiness_rejects_context_instance_not_owned_by_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            seed_store = PostgresRecordStore(database_url=database_url)
            seed_store.ensure_schema()
            seed_store.write_product_profile_record(_profile())
            seed_store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity_for_readiness()),
                authz_policy=_policy(),
                record_store_factory=lambda: app_store,
            )

            response = await _get_product_operational_readiness(
                app,
                context="other-context",
            )
            app_store.close()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_openapi_exposes_read_only_operational_readiness_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity_for_readiness()),
                authz_policy=_policy(),
                record_store_factory=lambda: store,
            )

            openapi = app.openapi()
            store.close()

        route = openapi["paths"][
            "/v1/products/{product}/contexts/{context}/instances/{instance}/operational-readiness"
        ]
        self.assertEqual(tuple(route), ("get",))
        self.assertEqual(route["get"]["operationId"], "read_product_operational_readiness")
        parameters = {parameter["name"]: parameter for parameter in route["get"]["parameters"]}
        self.assertIn("expected_current_artifact_id", parameters)
        self.assertFalse(parameters["expected_current_artifact_id"]["required"])
        self.assertEqual(
            route["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductOperationalReadinessResponse",
        )


if __name__ == "__main__":
    unittest.main()
