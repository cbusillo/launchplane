"""Service contracts for provider routes whose writes can complete before a failure."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

import click

from control_plane.dokploy.api import JsonValue
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _StubVerifier, _identity
from tests.support.stores import _seed_tracked_target_records, _sqlite_database_url
import tests.test_service as service_tests


class ComposeDomainOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        self.database_url = _sqlite_database_url(root / "launchplane.sqlite3")
        _seed_tracked_target_records(
            database_url=self.database_url,
            context="synthetic-context",
            instance="testing",
            target_id="compose-synthetic",
            target_type="compose",
            target_name="synthetic-compose",
        )
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.addCleanup(self.store.close)
        self.before = self.store.read_dokploy_target_record(
            context_name="synthetic-context", instance_name="testing"
        )
        self.before = self.before.model_copy(
            update={"domains": ("first.synthetic.invalid", "second.synthetic.invalid")}
        )
        self.store.write_dokploy_target_record(self.before)
        workflow_ref = "synthetic-owner/synthetic-repo/.github/workflows/setup.yml@refs/heads/main"
        create_app = cast(Any, service_tests.create_launchplane_dokploy_target_setup_app)
        self.app = create_app(
            state_dir=root / "state",
            verifier=_StubVerifier(
                _identity(
                    repository="synthetic-owner/synthetic-repo",
                    workflow_ref=workflow_ref,
                    event_name="workflow_dispatch",
                )
            ),
            authz_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "synthetic-owner/synthetic-repo",
                            "workflow_refs": [workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            ),
            control_plane_root_path=root,
            database_url=self.database_url,
        )
        config_patch = patch(
            "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
            return_value=("https://provider.synthetic.invalid", "private-token"),
        )
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.routes: list[dict[str, JsonValue]] = [
            {
                "domainId": domain_id,
                "host": host,
                "composeId": "compose-synthetic",
                "domainType": "compose",
                "serviceName": "web",
                "path": "/",
                "internalPath": "/",
                "port": 8080,
                "https": True,
            }
            for domain_id, host in (
                ("domain-first", "first.synthetic.invalid"),
                ("domain-second", "second.synthetic.invalid"),
            )
        ]
        self.writes: list[str] = []
        self.fail_at: int | None = None
        self.fail_read_at: int | None = None
        self.reads = 0
        self.failure: Exception = click.ClickException("private-provider-error private-token")
        provider_patch = patch(
            "control_plane.dokploy.api.dokploy_request", side_effect=self.provider_request
        )
        provider_patch.start()
        self.addCleanup(provider_patch.stop)

    def provider_request(self, **kwargs: Any) -> JsonValue:
        if kwargs["path"] == "/api/domain.byComposeId":
            self.reads += 1
            if self.reads == self.fail_read_at:
                raise self.failure
            return cast(JsonValue, [dict(route) for route in self.routes])
        self.assertEqual(kwargs["method"], "POST")
        payload = kwargs["payload"]
        domain_id = payload.get("domainId", "domain-created")
        self.writes.append(domain_id)
        if kwargs["path"] == "/api/domain.delete":
            self.routes = [route for route in self.routes if route["domainId"] != domain_id]
        elif kwargs["path"] == "/api/domain.update":
            for route in self.routes:
                if route.get("domainId") == domain_id:
                    route.update(payload)
        elif kwargs["path"] == "/api/domain.create":
            self.routes.append({"domainId": domain_id, **payload})
        else:
            self.fail(f"Unexpected provider path: {kwargs['path']}")
        # A lost response can follow an actual provider effect, even on the first call.
        if len(self.writes) == self.fail_at:
            raise self.failure
        return {"domainId": domain_id}

    def invoke(
        self, operation: str, *, mode: str = "apply", **overrides: object
    ) -> tuple[int, dict[str, Any]]:
        payload: dict[str, object] = {
            "mode": mode,
            "operation": operation,
            "context": "synthetic-context",
            "instance": "testing",
            "domains": ["first.synthetic.invalid", "second.synthetic.invalid"],
        }
        if operation == "reconcile-compose-domain":
            payload["runtime_port"] = 9000
        if mode == "apply":
            payload.update(
                {
                    "confirmation": "APPLY DOKPLOY TARGET SETUP",
                    "reason": "Synthetic route test",
                }
            )
        payload.update(overrides)
        invoke = cast(Any, service_tests._invoke_dokploy_target_setup_app)
        return cast(
            tuple[int, dict[str, Any]],
            invoke(self.app, payload=payload, headers={"Idempotency-Key": "synthetic-route-write"}),
        )

    def assert_partial(
        self, status: int, payload: dict[str, Any], *, completed: list[str], stage: str
    ) -> dict[str, Any]:
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "dokploy_domain_partial_outcome")
        self.assertTrue(payload["trace_id"])
        self.assertNotIn("private-provider-error", json.dumps(payload))
        self.assertNotIn("private-token", json.dumps(payload))
        recovery = json.loads(payload["records"]["domain_recovery"])
        self.assertEqual(recovery["completed_domain_ids"], completed)
        self.assertEqual(recovery["compose_id"], "compose-synthetic")
        self.assertEqual(recovery["stage"], stage)
        self.assertEqual(recovery["outcome"], "unknown")
        persisted = self.store.read_dokploy_target_record(
            context_name="synthetic-context", instance_name="testing"
        )
        self.assertEqual(persisted, self.before)
        return cast(dict[str, Any], recovery)

    def test_reconcile_second_write_failure_preserves_partial_evidence(self) -> None:
        self.fail_at = 2
        status, payload = self.invoke("reconcile-compose-domain")
        recovery = self.assert_partial(
            status, payload, completed=["domain-first"], stage="route-reconcile"
        )
        self.assertEqual(recovery["pending_domain"], "second.synthetic.invalid")
        self.assertEqual(self.writes, ["domain-first", "domain-second"])
        self.assertEqual([route["port"] for route in self.routes], [9000, 9000])

    def test_prune_second_write_failure_preserves_partial_evidence(self) -> None:
        self.fail_at = 2
        status, payload = self.invoke("prune-compose-domain")
        recovery = self.assert_partial(
            status, payload, completed=["domain-first"], stage="route-prune"
        )
        self.assertEqual(recovery["pending_domain"], "domain-second")
        self.assertEqual(self.writes, ["domain-first", "domain-second"])
        self.assertEqual(self.routes, [])

    def test_first_write_lost_response_is_also_uncertain(self) -> None:
        self.fail_at = 1
        self.failure = RuntimeError("private-provider-error private-token")
        status, payload = self.invoke("reconcile-compose-domain")
        self.assert_partial(status, payload, completed=[], stage="route-reconcile")
        self.assertEqual(self.writes, ["domain-first"])
        self.assertEqual(self.routes[0]["port"], 9000)

    def test_record_write_failure_after_routes_remains_partial(self) -> None:
        with patch.object(
            PostgresRecordStore,
            "write_dokploy_target_record",
            side_effect=RuntimeError("db failed"),
        ):
            status, payload = self.invoke(
                "reconcile-compose-domain", domains=["new.synthetic.invalid"]
            )
        recovery = self.assert_partial(
            status, payload, completed=["domain-created"], stage="record-write"
        )
        self.assertEqual(recovery["pending_domain"], "")
        self.assertEqual(self.writes, ["domain-created"])

    def test_validation_before_effects_is_distinct(self) -> None:
        status, payload = self.invoke("reconcile-compose-domain", runtime_port=0)
        self.assertEqual(status, 400)
        self.assertNotEqual(payload["error"]["code"], "dokploy_domain_partial_outcome")
        self.assertEqual(self.writes, [])
        self.routes[1].pop("domainId")
        status, payload = self.invoke("prune-compose-domain")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_dokploy_target_setup")
        self.assertEqual(self.writes, [])

    def test_reconcile_preflight_failure_after_a_completed_route_is_partial(self) -> None:
        self.fail_read_at = 2
        status, payload = self.invoke("reconcile-compose-domain")
        self.assert_partial(status, payload, completed=["domain-first"], stage="route-reconcile")
        self.assertEqual(self.writes, ["domain-first"])

    def test_first_reconcile_lookup_failure_is_distinct_and_has_no_effect(self) -> None:
        self.fail_read_at = 1
        status, payload = self.invoke("reconcile-compose-domain")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_dokploy_target_setup")
        self.assertNotIn("private-token", json.dumps(payload))
        self.assertEqual(self.writes, [])

    def test_invalid_route_inventory_is_refused_before_a_write(self) -> None:
        with patch("control_plane.dokploy.api.dokploy_request", return_value={"not": "routes"}):
            for mode in ("dry-run", "apply"):
                with self.subTest(mode=mode):
                    status, payload = self.invoke("reconcile-compose-domain", mode=mode)
                    self.assertEqual(status, 400)
                    self.assertEqual(payload["error"]["code"], "invalid_dokploy_target_setup")
        self.assertEqual(self.writes, [])

    def test_selected_route_without_id_is_refused_before_a_write(self) -> None:
        self.routes[0].pop("domainId")
        for mode in ("dry-run", "apply"):
            with self.subTest(mode=mode):
                status, payload = self.invoke("reconcile-compose-domain", mode=mode)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_dokploy_target_setup")
        self.assertEqual(self.writes, [])

    def test_prune_record_failure_after_deletions_remains_partial(self) -> None:
        with patch.object(
            PostgresRecordStore,
            "write_dokploy_target_record",
            side_effect=RuntimeError("db failed"),
        ):
            status, payload = self.invoke("prune-compose-domain")
        self.assert_partial(
            status, payload, completed=["domain-first", "domain-second"], stage="record-write"
        )
        self.assertEqual(self.routes, [])

    def test_dry_run_exposes_rewrite_and_duplicate_routes_without_effects(self) -> None:
        self.routes[0].update(
            {
                "serviceName": "other-service",
                "path": "/api",
                "internalPath": "/backend",
                "https": False,
                "certificateType": "letsencrypt",
                "stripPath": True,
                "customCertResolver": "custom-resolver",
                "applicationId": "old-app",
                "previewDeploymentId": "old-preview",
                "privatePayload": "private-provider-error",
            }
        )
        self.routes.insert(1, {**self.routes[0], "domainId": "duplicate-route", "port": 9100})
        self.routes.append({"host": "unrelated.synthetic.invalid", "privatePayload": "secret"})
        status, payload = self.invoke("reconcile-compose-domain", mode="dry-run")
        self.assertEqual(status, 202)
        previews = payload["result"]["setup"]["existing_routes"]
        self.assertEqual([route["selected_for_rewrite"] for route in previews], [True, False, True])
        self.assertEqual(
            previews[0],
            {
                "host": "first.synthetic.invalid",
                "domain_id": "domain-first",
                "service_name": "other-service",
                "path": "/api",
                "internal_path": "/backend",
                "port": 8080,
                "https": False,
                "certificate_type": "letsencrypt",
                "strip_path": True,
                "custom_cert_resolver": "custom-resolver",
                "application_id": "old-app",
                "preview_deployment_id": "old-preview",
                "selected_for_rewrite": True,
            },
        )
        self.assertNotIn("private-provider-error", json.dumps(payload))
        self.assertEqual(self.writes, [])
        # Apply chooses the same first-host match and rewrites it to the requested web route.
        status, _ = self.invoke("reconcile-compose-domain")
        self.assertEqual(status, 202)
        self.assertEqual(self.routes[0]["serviceName"], "web")
        self.assertEqual(self.routes[0]["path"], "/")
        self.assertEqual(self.routes[0]["port"], 9000)
        self.assertEqual(self.routes[0]["certificateType"], "none")
        self.assertFalse(self.routes[0]["stripPath"])
        self.assertIsNone(self.routes[0]["customCertResolver"])
        self.assertIsNone(self.routes[0]["applicationId"])
        self.assertIsNone(self.routes[0]["previewDeploymentId"])
        self.assertEqual(self.routes[1]["certificateType"], "letsencrypt")
        self.assertEqual(self.routes[1]["serviceName"], "other-service")


if __name__ == "__main__":
    unittest.main()
