from contextlib import nullcontext
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from typing import Any

from control_plane.contracts.deploy_target import DeployedTargetReference, ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, DokployTargetType
from control_plane.contracts.privileged_operation_worker_heartbeat import (
    PrivilegedOperationWorkerHeartbeatRecord,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _local_operator_bearer_config,
    _seed_dokploy_target_inspect_records,
)
from tests.support.auth import _identity, _StubVerifier, scoped_agent_transport
from tests.support.profiles import product_profile_payload
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.secrets import LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url


def _repair_body(observed: dict[str, Any]) -> dict[str, Any]:
    provider = observed["provider_target_record"]
    expected = DeployedTargetReference.model_validate(
        {key: provider[key] for key in DeployedTargetReference.model_fields if key in provider}
    )
    body: dict[str, Any] = {
        "operation": "repair-domain-authority",
        "mode": "dry-run",
        "product": "launchplane",
        "context": observed["tracked_target"]["context"],
        "instance": observed["tracked_target"]["instance"],
        "target_type": observed["target_type"],
        "target_id": observed["target_id"],
        "domains": [route["host"] for route in observed["provider"]["domains"]],
        "expected_current_provider_target": expected.model_dump(mode="json"),
        "reason": "Synthetic inspection-to-planner proof.",
    }

    if provider.get("comparison_fingerprint"):
        body["expected_current_provider_target_fingerprint"] = provider["comparison_fingerprint"]
    return body


class DokployDomainAuthorityPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspected_authority_plans_without_writes_and_rejects_drift(self) -> None:
        cases: tuple[tuple[DokployTargetType, str, bool], ...] = (
            ("application", "Synthetic", False),
            ("compose", "Synthetic", False),
            ("compose", "", False),
            ("application", "Synthetic", True),
        )
        for target_type, project_name, empty_evidence in cases:
            with (
                self.subTest(target_type=target_type, project=project_name, empty=empty_evidence),
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
                if empty_evidence:
                    authority = authority.model_copy(update={"provider_evidence": {}})
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
                        body = _repair_body(observed)
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

    async def test_unrecognized_evidence_without_key_is_a_redacted_prerequisite(self) -> None:
        await self._noncanonical_round_trip(fingerprint_available=False, lane_scoped=False)

    async def test_noncanonical_fingerprint_plans_and_preserves_boundaries(self) -> None:
        await self._noncanonical_round_trip(fingerprint_available=True, lane_scoped=False)

    async def test_lane_scoped_comparison_and_missing_input_diagnostics(self) -> None:
        await self._noncanonical_round_trip(fingerprint_available=True, lane_scoped=True)
        await self._noncanonical_round_trip(fingerprint_available=False, lane_scoped=True)

    async def _noncanonical_round_trip(
        self, *, fingerprint_available: bool, lane_scoped: bool
    ) -> None:
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        if fingerprint_available:
            os.environ[LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR] = "synthetic-comparison-key"
        for evidence in (
            {"project_name": "odoo", "private_payload": "hidden-evidence-value"},
            {"project_name": "hidden-evidence-value"},
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
                profile_payload = product_profile_payload("synthetic-product")
                profile_payload["lanes"] = (
                    {
                        "instance": authority.instance,
                        "context": authority.context,
                        "base_url": "https://synthetic.invalid",
                        "health_url": "https://synthetic.invalid/health",
                    },
                )
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(profile_payload)
                )
                policy = (
                    LaunchplaneAuthzPolicy.model_validate(
                        {
                            "schema_version": 2,
                            "local_operators": [
                                {
                                    "subjects": ["local-owner-agent"],
                                    "token_labels": ["local-owner-read"],
                                    "products": ["synthetic-product", "launchplane"],
                                    "contexts": [authority.context],
                                    "instances": [authority.instance],
                                    "actions": ["agent.read"],
                                }
                            ],
                        }
                    )
                    if lane_scoped
                    else LaunchplaneAuthzPolicy()
                )
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    database_url=database_url,
                    record_store_factory=lambda: store,
                    control_plane_root_path=root,
                    bearer_identity_config=_local_operator_bearer_config(),
                )
                image = f"ghcr.io/synthetic/runtime@sha256:{'a' * 64}"
                worker_identity = "b" * 64
                recorded_at = datetime.now(timezone.utc)
                store.write_privileged_operation_worker_heartbeat_record(
                    PrivilegedOperationWorkerHeartbeatRecord(
                        worker_identity_sha256=worker_identity,
                        image_reference=image,
                        poll_interval_seconds=15,
                        last_poll_succeeded_at=recorded_at.isoformat(),
                    ),
                    prune_before=(recorded_at - timedelta(days=7)).isoformat(),
                    prune_after=(recorded_at + timedelta(minutes=1)).isoformat(),
                )
                live_target = {
                    "composeId": authority.target_id,
                    "appName": "synthetic-app",
                    "serverId": "synthetic-server",
                    "domains": [{"host": "new.synthetic.invalid"}],
                }
                with (
                    scoped_agent_transport() if lane_scoped else nullcontext(),
                    patch(
                        "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.synthetic.invalid", "synthetic-token"),
                    ),
                    patch(
                        "control_plane.dokploy_target_inspect.dokploy_api.fetch_dokploy_target_payload",
                        return_value=live_target,
                    ),
                    patch(
                        "control_plane.dokploy_target_inspect.dokploy_runtime_evidence.fetch_compose_service_runtime",
                        return_value={
                            "container_id": "synthetic-container",
                            "container_identity_sha256": worker_identity,
                            "running": True,
                            "image_reference_immutable": True,
                            "immutable_image_reference": image,
                        },
                    ),
                    patch(
                        "control_plane.dokploy_target_inspect.dokploy_runtime_evidence.fetch_compose_container_logs",
                        return_value=(),
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
                        side_effect=AssertionError("planning must not write authority"),
                    ),
                ):
                    async with lifespan_client(app) as client:
                        headers = {"Authorization": "Bearer local-operator-token"}
                        response = await client.get(
                            "/v1/dokploy-targets/inspect",
                            params={
                                "context": "cm_website",
                                "instance": "prod",
                                "service": "synthetic-service",
                                "expected_image": image,
                            },
                            headers=headers,
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                        observed = response.json()["inspect"]
                        self.assertTrue(observed["runtime_evidence"]["proof_ready"])
                        if not fingerprint_available:
                            self.assertEqual(
                                observed["provider_target_record"].get(
                                    "comparison_evidence_status"
                                ),
                                "unavailable",
                            )
                        self.assertNotIn("provider_evidence", observed["provider_target_record"])
                        self.assertNotIn("private_payload", response.text)
                        self.assertNotIn("hidden-evidence-value", response.text)
                        body = _repair_body(observed)
                        missing_body = dict(body)
                        missing_body.pop("expected_current_provider_target_fingerprint", None)
                        prerequisite = await client.post(
                            "/v1/dokploy-targets/setup", json=missing_body, headers=headers
                        )
                        self.assertEqual(prerequisite.status_code, 400, prerequisite.text)
                        self.assertEqual(
                            prerequisite.json()["error"]["code"],
                            "dokploy_target_comparison_evidence_unavailable",
                        )
                        self.assertTrue(prerequisite.json()["trace_id"])
                        self.assertNotIn("hidden-evidence-value", prerequisite.text)
                        fetch_domains.assert_not_called()
                        if fingerprint_available:
                            planned = await client.post(
                                "/v1/dokploy-targets/setup", json=body, headers=headers
                            )
                            self.assertEqual(planned.status_code, 202, planned.text)
                            self.assertFalse(planned.json()["result"]["applied"])
                            self.assertNotIn("hidden-evidence-value", planned.text)
                            self.assertNotIn("private_payload", planned.text)
                            self.assertNotIn(
                                "provider_evidence",
                                planned.json()["result"]["setup"]["provider_target_record"],
                            )
                            fetch_domains.reset_mock()
                            for changes in (
                                {"expected_current_provider_target_fingerprint": "tampered"},
                                {"context": "foreign-context"},
                                {"instance": "testing"},
                                {
                                    "expected_current_provider_target": {
                                        **body["expected_current_provider_target"],
                                        "target_id": "foreign-target",
                                    }
                                },
                                {
                                    "expected_current_provider_target": {
                                        **body["expected_current_provider_target"],
                                        "provider_evidence": {"unexpected": "conflicting"},
                                    }
                                },
                            ):
                                denied = await client.post(
                                    "/v1/dokploy-targets/setup",
                                    json={**body, **changes},
                                    headers=headers,
                                )
                                self.assertIn(denied.status_code, (400, 403), denied.text)
                                fetch_domains.assert_not_called()
                            for changes in (
                                {"provider_evidence": {"private_payload": "changed"}},
                                {"display_name": "changed-name"},
                            ):
                                store.write_provider_target_record(
                                    authority.model_copy(update=changes)
                                )
                                stale = await client.post(
                                    "/v1/dokploy-targets/setup", json=body, headers=headers
                                )
                                self.assertIn(stale.status_code, (400, 403), stale.text)
                                fetch_domains.assert_not_called()
                            store.write_provider_target_record(authority)
                            with patch.dict(
                                os.environ,
                                {LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "rotated-synthetic-key"},
                            ):
                                rotated = await client.post(
                                    "/v1/dokploy-targets/setup", json=body, headers=headers
                                )
                                self.assertIn(rotated.status_code, (400, 403), rotated.text)
                                fetch_domains.assert_not_called()
                            live_target["composeId"] = "drifted-provider-target"
                            identity_drift = await client.post(
                                "/v1/dokploy-targets/setup", json=body, headers=headers
                            )
                            self.assertEqual(identity_drift.status_code, 400, identity_drift.text)
                            fetch_domains.assert_not_called()
                            live_target["composeId"] = authority.target_id
                            for provider_domains in ((), ({"host": "foreign.synthetic.invalid"},)):
                                fetch_domains.return_value = provider_domains
                                mismatch = await client.post(
                                    "/v1/dokploy-targets/setup", json=body, headers=headers
                                )
                                self.assertEqual(mismatch.status_code, 400, mismatch.text)
                            fetch_domains.return_value = ({"host": "new.synthetic.invalid"},)
                            denied_apply = await client.post(
                                "/v1/dokploy-targets/setup",
                                json={**body, "mode": "apply"},
                                headers=headers,
                            )
                            self.assertGreaterEqual(denied_apply.status_code, 400)
                        # A caller that already has the complete private expectation
                        # retains the existing full-comparison path; no record repair
                        # or new grant is required by this source change.
                        complete = await client.post(
                            "/v1/dokploy-targets/setup",
                            json={
                                **missing_body,
                                "expected_current_provider_target": authority.to_deployed_target_reference().model_dump(
                                    mode="json"
                                ),
                            },
                            headers=headers,
                        )
                        self.assertEqual(complete.status_code, 202, complete.text)
                        self.assertFalse(complete.json()["result"]["applied"])
                self.assertEqual(
                    store.read_provider_target_record(
                        context_name="cm_website", instance_name="prod"
                    ),
                    authority,
                )
