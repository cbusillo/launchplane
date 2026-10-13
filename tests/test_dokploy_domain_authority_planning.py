from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, DokployTargetType
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _local_operator_bearer_config,
    _seed_dokploy_target_inspect_records,
)
from tests.support.auth import _identity, _StubVerifier
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url


class DokployDomainAuthorityPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspected_authority_plans_without_writes_and_rejects_drift(self) -> None:
        cases: tuple[tuple[DokployTargetType, str], ...] = (
            ("application", "Synthetic"),
            ("compose", "Synthetic"),
            ("compose", ""),
        )
        for target_type, project_name in cases:
            with (
                self.subTest(target_type=target_type, project=project_name),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                store = PostgresRecordStore(database_url=_sqlite_database_url(root / "db.sqlite3"))
                self.addCleanup(store.close)
                store.ensure_schema()
                target = DokployTargetRecord(
                    context="synthetic-context",
                    instance="prod",
                    target_type=target_type,
                    target_name="synthetic-target",
                    project_name=project_name,
                    domains=("https://old.synthetic.invalid/path",),
                    env={"DATABASE_PASSWORD": "stored-secret"},
                    updated_at="2026-08-12T00:00:00Z",
                )
                target_id = DokployTargetIdRecord(
                    context=target.context,
                    instance=target.instance,
                    target_id="synthetic-provider-target",
                    updated_at=target.updated_at,
                )
                authority = ProviderTargetRecord.from_dokploy_records(
                    target_record=target, target_id_record=target_id
                )
                store.write_dokploy_target_record(target)
                store.write_dokploy_target_id_record(target_id)
                store.write_provider_target_record(authority)
                identity_key = "applicationId" if target_type == "application" else "composeId"
                live_target = {
                    identity_key: target_id.target_id,
                    "name": "synthetic-target",
                    "domains": [{"host": "new.synthetic.invalid"}],
                    "env": {"DATABASE_PASSWORD": "provider-secret"},
                }
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy(),
                    database_url=store.database_url,
                    record_store_factory=lambda: store,
                    control_plane_root_path=root,
                    bearer_identity_config=_local_operator_bearer_config(),
                )
                headers = {"Authorization": "Bearer local-operator-token"}
                with (
                    patch(
                        "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.synthetic.invalid", "synthetic-token"),
                    ),
                    patch(
                        "control_plane.dokploy_target_inspect.dokploy_api.fetch_dokploy_target_payload",
                        return_value=live_target,
                    ),
                    patch(
                        "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.synthetic.invalid", "synthetic-token"),
                    ),
                    patch(
                        "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                        return_value=live_target,
                    ),
                    patch(
                        "control_plane.dokploy_target_setup_http.fetch_dokploy_target_domains_for_setup",
                        return_value=({"host": "new.synthetic.invalid"},),
                    ) as fetch_domains,
                    patch.object(
                        store,
                        "compare_and_write_dokploy_target_domains",
                        side_effect=AssertionError("dry run must not write authority"),
                    ),
                    patch(
                        "control_plane.dokploy_target_setup_http.mutate_dokploy_payload_for_target_setup",
                        side_effect=AssertionError("planning must not mutate the provider"),
                    ),
                ):
                    async with lifespan_client(app) as client:
                        inspection = await client.get(
                            "/v1/dokploy-targets/inspect",
                            params={"context": target.context, "instance": target.instance},
                            headers=headers,
                        )
                        self.assertEqual(inspection.status_code, 200, inspection.text)
                        observed = inspection.json()["inspect"]
                        self.assertNotIn("stored-secret", inspection.text)
                        self.assertNotIn("provider-secret", inspection.text)
                        provider = observed["provider_target_record"]
                        expected = {
                            key: provider[key]
                            for key in (
                                "provider_id",
                                "target_category",
                                "target_id",
                                "display_name",
                                "provider_target_type",
                            )
                        }
                        expected["provider_evidence"] = provider.get("provider_evidence", {})
                        body = {
                            "operation": "repair-domain-authority",
                            "mode": "dry-run",
                            "product": "launchplane",
                            "context": target.context,
                            "instance": target.instance,
                            "target_type": observed["target_type"],
                            "target_id": observed["target_id"],
                            "domains": [route["host"] for route in observed["provider"]["domains"]],
                            "expected_current_provider_target": expected,
                            "reason": "Synthetic inspection-to-planner proof.",
                        }
                        planned = await client.post(
                            "/v1/dokploy-targets/setup", json=body, headers=headers
                        )
                        self.assertEqual(planned.status_code, 202, planned.text)
                        self.assertFalse(planned.json()["result"]["applied"])
                        self.assertEqual(
                            planned.json()["result"]["setup"]["provider_target_record"][
                                "provider_evidence"
                            ],
                            authority.provider_evidence,
                        )
                        self.assertNotIn("stored-secret", planned.text)
                        self.assertNotIn("provider-secret", planned.text)
                        denied = await client.post(
                            "/v1/dokploy-targets/setup",
                            json={
                                **body,
                                "mode": "apply",
                                "confirmation": "APPLY DOKPLOY TARGET SETUP",
                            },
                            headers={**headers, "Idempotency-Key": "synthetic-apply"},
                        )
                        self.assertEqual(denied.status_code, 403, denied.text)

                        for changed in ({"project_name": "Changed"}, {"region": "Changed"}):
                            store.write_provider_target_record(
                                authority.model_copy(update={"provider_evidence": changed})
                            )
                            fetch_domains.reset_mock()
                            stale = await client.post(
                                "/v1/dokploy-targets/setup", json=body, headers=headers
                            )
                            self.assertEqual(stale.status_code, 400, stale.text)
                            fetch_domains.assert_not_called()
                            self.assertEqual(
                                store.read_provider_target_record(
                                    context_name=target.context, instance_name=target.instance
                                ).provider_evidence,
                                changed,
                            )
                        store.write_provider_target_record(authority)

                self.assertEqual(
                    store.read_dokploy_target_record(
                        context_name=target.context, instance_name=target.instance
                    ),
                    target,
                )
                self.assertEqual(
                    store.read_dokploy_target_id_record(
                        context_name=target.context, instance_name=target.instance
                    ),
                    target_id,
                )
                self.assertEqual(
                    store.read_provider_target_record(
                        context_name=target.context, instance_name=target.instance
                    ),
                    authority,
                )

    async def test_unrecognized_or_inconsistent_evidence_is_a_redacted_prerequisite(self) -> None:
        for evidence in (
            {"project_name": "odoo", "private_payload": "hidden-evidence-value"},
            {"project_name": "hidden-evidence-value"},
            {},
        ):
            with self.subTest(evidence=evidence), TemporaryDirectory() as directory:
                root = Path(directory)
                database_url = _sqlite_database_url(root / "db.sqlite3")
                _seed_dokploy_target_inspect_records(database_url)
                store = PostgresRecordStore(database_url=database_url)
                self.addCleanup(store.close)
                authority = store.read_provider_target_record(
                    context_name="cm_website", instance_name="prod"
                ).model_copy(update={"provider_evidence": evidence})
                store.write_provider_target_record(authority)
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy(),
                    database_url=database_url,
                    record_store_factory=lambda: store,
                    control_plane_root_path=root,
                    bearer_identity_config=_local_operator_bearer_config(),
                )
                with (
                    patch(
                        "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.synthetic.invalid", "synthetic-token"),
                    ),
                    patch(
                        "control_plane.dokploy_target_inspect.dokploy_api.fetch_dokploy_target_payload",
                        return_value={"composeId": authority.target_id},
                    ),
                ):
                    async with lifespan_client(app) as client:
                        response = await client.get(
                            "/v1/dokploy-targets/inspect",
                            params={"context": "cm_website", "instance": "prod"},
                            headers={"Authorization": "Bearer local-operator-token"},
                        )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(
                    response.json()["error"]["code"],
                    "dokploy_target_comparison_evidence_unavailable",
                )
                self.assertTrue(response.json()["trace_id"])
                self.assertNotIn("private_payload", response.text)
                self.assertNotIn("hidden-evidence-value", response.text)
                self.assertEqual(
                    store.read_provider_target_record(
                        context_name="cm_website", instance_name="prod"
                    ),
                    authority,
                )
