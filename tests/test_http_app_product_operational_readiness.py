from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _identity, _StubVerifier
from tests.support.artifact_manifests import artifact_manifest_v2
from tests.support.product_reads import _get_product_operational_readiness
from tests.support.stores import _sqlite_database_url


_ACTION = "odoo_target_replacement_plan.read"
_WORKFLOW_REF = "example/example-odoo/.github/workflows/replace.yml@refs/heads/main"
_JOB_WORKFLOW_REF = (
    "example/launchplane/.github/workflows/reusable-target-replacement.yml@"
    "0123456789abcdef0123456789abcdef01234567"
)
_ARTIFACT_ID = "artifact-example-odoo-0123456789abcdef"
_SOURCE_GIT_REF = "89abcdef0123456789abcdef0123456789abcdef"


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


def _current_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _seed_recorded_lane(store: PostgresRecordStore) -> None:
    timestamp = _current_timestamp()
    deploy = DeploymentEvidence(
        target_name="example-odoo-testing",
        target_type="compose",
        deploy_mode="runtime-provider-api",
        deployment_id="provider-deployment-example-odoo-testing",
        status="pass",
        started_at=timestamp,
        finished_at=timestamp,
    )
    health = HealthcheckEvidence(
        verified=True,
        urls=("https://example-odoo.invalid/launchplane/health",),
        timeout_seconds=30,
        status="pass",
        runtime_identity_status="match",
    )
    deployment = DeploymentRecord(
        record_id="deployment-example-odoo-testing",
        artifact_identity=ArtifactIdentityReference(artifact_id=_ARTIFACT_ID),
        context="example-odoo",
        instance="testing",
        source_git_ref=_SOURCE_GIT_REF,
        deploy=deploy,
        destination_health=health,
    )
    store.write_artifact_manifest(
        artifact_manifest_v2(
            artifact_id=_ARTIFACT_ID,
            image_repository="ghcr.io/example/example-odoo",
        )
    )
    store.write_provider_target_record(
        ProviderTargetRecord(
            context="example-odoo",
            instance="testing",
            provider_id="dokploy",
            target_category="compose",
            target_id="provider-private-id",
            display_name="example-odoo-testing",
            provider_target_type="compose",
            updated_at=timestamp,
            source_label="test",
        )
    )
    store.write_deployment_record(deployment)
    store.write_environment_inventory(
        EnvironmentInventory(
            context="example-odoo",
            instance="testing",
            artifact_identity=ArtifactIdentityReference(artifact_id=_ARTIFACT_ID),
            source_git_ref=_SOURCE_GIT_REF,
            deploy=deploy,
            destination_health=health,
            updated_at=timestamp,
            deployment_record_id=deployment.record_id,
        )
    )


class FastApiProductOperationalReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_db_backed_readiness_enriches_raw_lane_provenance(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            seed_store = PostgresRecordStore(database_url=database_url)
            seed_store.ensure_schema()
            seed_store.write_product_profile_record(_profile())
            seed_store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="launchplane-authz-policy-operational-readiness-ready",
                    revision=1,
                    source="test",
                    updated_at=_current_timestamp(),
                    policy=_policy(),
                )
            )
            _seed_recorded_lane(seed_store)
            raw_summary = seed_store.read_lane_summary(
                context_name="example-odoo",
                instance_name="testing",
            )
            self.assertEqual(raw_summary.provenance.freshness_status, "missing")
            seed_store.close()

            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity_for_readiness()),
                authz_policy=_policy(),
                record_store_factory=lambda: app_store,
            )

            response = await _get_product_operational_readiness(
                app,
                artifact_id=_ARTIFACT_ID,
                expected_current_artifact_id=_ARTIFACT_ID,
            )
            app_store.close()

        self.assertEqual(response.status_code, 200)
        readiness = response.json()["readiness"]
        states = {
            dimension["dimension"]: dimension["state"] for dimension in readiness["dimensions"]
        }
        self.assertEqual(states["provider_target"], "ready")
        self.assertEqual(states["deployment"], "ready")

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
