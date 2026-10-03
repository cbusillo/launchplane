from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import select
from control_plane.storage.postgres import LaunchplaneDokployTargetRow

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
)
from control_plane.dokploy_target_setup_http import (
    DokployTargetSetupEnvelope,
    execute_dokploy_target_setup,
)
from control_plane.dokploy_target_inspect import summarize_dokploy_target_payload
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _StubVerifier, _identity
from tests.support.stores import _sqlite_database_url
import tests.test_service as service_tests


class ComposeSourceSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database_url = _sqlite_database_url(self.root / "records.sqlite3")
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            {
                "product": "sample",
                "display_name": "Sample",
                "repository": "example/sample",
                "driver_id": "generic-web",
                "image": ProductImageProfile().model_dump(mode="json"),
                "lanes": [
                    {"instance": "prod", "context": "sample"},
                    {"instance": "testing", "context": "sample"},
                ],
                "updated_at": "2026-10-03T00:00:00Z",
                "source": "test",
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.target = DokployTargetRecord(
            context="sample",
            instance="testing",
            target_type="compose",
            target_name="Sample testing",
            source_type="github",
            compose_path="./docker-compose.yml",
            env={"KEEP": "private-value"},
            domains=("test.example.invalid",),
            updated_at="2026-10-03T00:00:00Z",
        )
        self.target_id = DokployTargetIdRecord(
            context="sample",
            instance="testing",
            target_id="compose-testing",
            updated_at=self.target.updated_at,
        )
        self.provider = ProviderTargetRecord.from_dokploy_records(
            target_record=self.target,
            target_id_record=self.target_id,
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(self.provider)
        self.live: dict[str, Any] = {
            "composeId": self.target_id.target_id,
            "name": self.target.target_name,
            "environmentId": "env-testing",
            "serverId": "server-one",
            "appName": "sample-testing",
            "sourceType": "github",
            "composePath": "./docker-compose.yml",
            "autoDeploy": False,
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.readback_wrong = False
        self.patches = (
            patch(
                "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
                return_value=("https://provider.invalid", "test-only"),
            ),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=lambda *_: deepcopy(self.live),
            ),
            patch(
                "control_plane.dokploy_target_setup_http.mutate_dokploy_payload_for_target_setup",
                side_effect=self.mutate,
            ),
        )
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def mutate(self, _host: str, _token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, deepcopy(payload)))
        if path == "/api/compose.update":
            self.live.update(payload)
            if self.readback_wrong:
                self.live["customGitBranch"] = "unexpected"
            return deepcopy(self.live)
        if path == "/api/project.create":
            return {"projectId": "project-new"}
        if path == "/api/environment.create":
            return {"environmentId": self.live["environmentId"]}
        return {"composeId": self.live["composeId"]}

    def request(self, **overrides: Any) -> DokployTargetSetupEnvelope:
        return DokployTargetSetupEnvelope.model_validate(
            {
                "operation": "complete-compose-source",
                "context": "sample",
                "instance": "testing",
                "custom_git_branch": "main",
                "compose_path": "./deploy/compose.yml",
                **overrides,
            }
        )

    def invoke_http(self, *, mode: str = "dry-run", **overrides: Any) -> tuple[int, dict[str, Any]]:
        workflow = "example/ops/.github/workflows/setup.yml@refs/heads/main"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [
                    {
                        "repository": "example/ops",
                        "workflow_refs": [workflow],
                        "event_names": ["workflow_dispatch"],
                        "products": ["sample"],
                        "contexts": ["sample"],
                        "instances": ["testing"],
                        "actions": ["dokploy_target.lane_setup"],
                    }
                ],
            }
        )
        app = cast(Any, service_tests.create_launchplane_dokploy_target_setup_app)(
            state_dir=self.root / "state",
            control_plane_root_path=self.root,
            database_url=self.database_url,
            authz_policy=policy,
            verifier=_StubVerifier(
                _identity(
                    repository="example/ops",
                    workflow_ref=workflow,
                    event_name="workflow_dispatch",
                )
            ),
        )
        payload = {
            "operation": "complete-compose-source",
            "context": "sample",
            "instance": "testing",
            "custom_git_branch": "main",
            "compose_path": "./deploy/compose.yml",
            "mode": mode,
            **overrides,
        }
        headers = {}
        if mode == "apply":
            payload.update(confirmation="APPLY DOKPLOY TARGET SETUP", reason="Complete source")
            headers["Idempotency-Key"] = "complete-source-test"
        return cast(
            tuple[int, dict[str, Any]],
            cast(Any, service_tests._invoke_dokploy_target_setup_app)(
                app,
                payload=payload,
                headers=headers,
            ),
        )

    def test_lane_grant_dry_run_reports_profile_repository_without_writes(self) -> None:
        status, response = self.invoke_http()
        self.assertEqual(status, 202, response)
        self.assertEqual(
            response["result"]["setup"]["source"]["custom_git_url"],
            f"https://github.com/{self.profile.repository}.git",
        )
        self.assertFalse(response["result"]["applied"])
        self.assertEqual(self.calls, [])
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name="sample", instance_name="testing"),
            self.target,
        )
        self.assertNotIn("private-value", str(response))

    def test_lane_grant_apply_preserves_binding_settings_and_provider_readback(self) -> None:
        status, response = self.invoke_http(mode="apply")
        self.assertEqual(status, 202, response)
        actual = self.store.read_dokploy_target_record(
            context_name="sample", instance_name="testing"
        )
        self.assertEqual(actual.custom_git_url, f"https://github.com/{self.profile.repository}.git")
        self.assertEqual(actual.custom_git_branch, self.live["customGitBranch"])
        self.assertEqual(actual.compose_path, self.live["composePath"])
        self.assertEqual(actual.env, self.target.env)
        self.assertEqual(actual.domains, self.target.domains)
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name="sample", instance_name="testing"
            ),
            self.target_id,
        )
        self.assertEqual(
            self.store.read_provider_target_record(context_name="sample", instance_name="testing"),
            self.provider,
        )
        self.assertEqual([path for path, _ in self.calls], ["/api/compose.update"])
        self.assertIs(self.live["autoDeploy"], False)
        summary = summarize_dokploy_target_payload(
            target_type="compose", target_id=self.target_id.target_id, payload=self.live
        )
        self.assertEqual(summary["custom_git_url"], actual.custom_git_url)
        self.assertEqual(summary["custom_git_branch"], actual.custom_git_branch)
        # Apply replay never updates the now configured source a second time.
        replay_status, _ = self.invoke_http(mode="apply")
        self.assertEqual(replay_status, 202)
        self.assertEqual(len(self.calls), 1)

    def test_source_completion_rejects_production_and_arbitrary_target_inputs(self) -> None:
        for overrides in (
            {"instance": "prod"},
            {"target_id": "other"},
            {"environment_id": "env-prod"},
            {"custom_git_url": "https://other.invalid"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                self.request(**overrides)
        self.assertEqual(self.calls, [])

    def test_source_completion_refuses_missing_or_shared_context_and_lane(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"lanes": self.profile.lanes[:1]})
        )
        with self.assertRaisesRegex(ValueError, "existing product testing lane"):
            execute_dokploy_target_setup(
                control_plane_root_path=self.root, record_store=self.store, request=self.request()
            )
        self.store.write_product_profile_record(self.profile)
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"product": "other"})
        )
        status, response = self.invoke_http()
        self.assertEqual(status, 403, response)
        self.assertEqual(self.calls, [])

    def test_completion_refuses_configured_partial_and_different_live_sources(self) -> None:
        empty = deepcopy(self.live)
        for field, value in (
            ("customGitUrl", "https://github.com/example/sample.git"),
            ("repository", "sample"),
            ("customGitSSHKeyId", "existing-key"),
            ("composeFile", "services: {}"),
            ("sourceType", "docker"),
        ):
            with self.subTest(field=field):
                self.live = {**empty, field: value}
                with self.assertRaises(ValueError):
                    execute_dokploy_target_setup(
                        control_plane_root_path=self.root,
                        record_store=self.store,
                        request=self.request(mode="apply"),
                    )
        self.assertEqual(self.calls, [])

    def test_completion_refuses_stale_records_before_provider_update(self) -> None:
        replacement = self.target.model_copy(
            update={"custom_git_url": "https://github.com/example/sample.git"}
        )
        for changed in ("profile", "target", "id", "provider"):
            with self.subTest(changed=changed):
                self.store.write_product_profile_record(self.profile)
                self.store.write_dokploy_target_record(self.target)
                self.store.write_dokploy_target_id_record(self.target_id)
                self.store.write_provider_target_record(self.provider)
                if changed == "profile":
                    self.store.write_product_profile_record(
                        self.profile.model_copy(update={"repository": "example/other"})
                    )
                elif changed == "target":
                    self.store.write_dokploy_target_record(
                        self.target.model_copy(update={"env": {"NEW": "value"}})
                    )
                elif changed == "id":
                    self.store.write_dokploy_target_id_record(
                        self.target_id.model_copy(update={"target_id": "other"})
                    )
                else:
                    self.store.write_provider_target_record(
                        self.provider.model_copy(update={"updated_at": "2026-10-03T01:00:00Z"})
                    )
                with self.assertRaises(ValueError):
                    self.store.complete_dokploy_compose_source(
                        expected_profile=self.profile,
                        expected_record=self.target,
                        expected_target_id=self.target_id,
                        expected_provider_target=self.provider,
                        replacement_record=replacement,
                        apply_provider=lambda: self.fail("stale authority reached provider update"),
                    )

    def test_readback_mismatch_does_not_persist_planned_source(self) -> None:
        self.readback_wrong = True
        with self.assertRaisesRegex(ValueError, "read-back"):
            execute_dokploy_target_setup(
                control_plane_root_path=self.root,
                record_store=self.store,
                request=self.request(mode="apply"),
            )
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name="sample", instance_name="testing"),
            self.target,
        )

    def test_creation_sets_source_before_adoption_and_reports_it_in_plan(self) -> None:
        request = self.request(
            operation="create-compose",
            target_name="New compose",
            project_name="New",
            server_id="server-one",
            mode="dry-run",
        )
        plan = execute_dokploy_target_setup(
            control_plane_root_path=self.root, record_store=self.store, request=request
        )
        planned_setup = cast(dict[str, Any], plan["setup"])
        self.assertEqual(
            planned_setup["target_record"]["custom_git_url"],
            f"https://github.com/{self.profile.repository}.git",
        )
        self.assertEqual(self.calls, [])
        # A different empty lane creates a new compose rather than replacing testing.
        self.live["composeId"] = "compose-new"
        result = execute_dokploy_target_setup(
            control_plane_root_path=self.root,
            record_store=self.store,
            request=request.model_copy(update={"mode": "apply", "instance": "preview"}),
        )
        self.assertTrue(result["applied"])
        self.assertEqual(
            [path for path, _ in self.calls],
            [
                "/api/project.create",
                "/api/environment.create",
                "/api/compose.create",
                "/api/compose.update",
            ],
        )
        persisted = self.store.read_dokploy_target_record(
            context_name="sample", instance_name="preview"
        )
        self.assertEqual(persisted.custom_git_url, self.live["customGitUrl"])
        self.assertEqual(persisted.compose_path, self.live["composePath"])

    def test_source_inputs_reject_shell_syntax_and_repository_path_escape(self) -> None:
        for overrides in (
            {"custom_git_branch": "main$(id)"},
            {"custom_git_branch": "main;id"},
            {"compose_path": "./compose.yml;id"},
            {"compose_path": "../other/compose.yml"},
            {"compose_path": "/tmp/compose.yml"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                self.request(**overrides)

    def test_routine_target_provenance_change_does_not_change_binding(self) -> None:
        self.store.write_dokploy_target_record(
            self.target.model_copy(
                update={
                    "updated_at": "2026-10-03T02:00:00Z",
                    "source_label": "service:testing-hold",
                }
            )
        )
        status, response = self.invoke_http(mode="apply")
        self.assertEqual(status, 202, response)
        self.assertEqual(
            self.store.read_provider_target_record(context_name="sample", instance_name="testing"),
            self.provider,
        )

    def test_older_payload_with_missing_defaults_can_complete(self) -> None:
        with self.store._session_factory() as session:
            row = session.scalar(select(LaunchplaneDokployTargetRow))
            assert row is not None
            old_payload = deepcopy(row.payload)
            old_payload.pop("healthcheck_timeout_seconds", None)
            old_payload["policies"].pop("integration_allowances", None)
            row.payload = old_payload
            session.commit()
        status, response = self.invoke_http(mode="apply")
        self.assertEqual(status, 202, response)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name="sample", instance_name="testing"
            ).env,
            self.target.env,
        )

    def test_missing_tracked_binding_returns_a_validation_error(self) -> None:
        self.store.delete_provider_target_record(expected_record=self.provider)
        status, response = self.invoke_http()
        self.assertEqual(status, 400, response)
        self.assertEqual(self.calls, [])

    def test_partial_provider_update_is_reported_without_persisting(self) -> None:
        self.readback_wrong = True
        status, response = self.invoke_http(mode="apply")
        self.assertEqual(status, 502, response)
        self.assertEqual(response["error"]["code"], "dokploy_source_partial_outcome")
        self.assertIn("Provider source applied", response["error"]["message"])
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name="sample", instance_name="testing"),
            self.target,
        )

    def test_historical_context_and_partial_branches_are_refused(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"historical_contexts": ("sample",)})
        )
        with self.assertRaisesRegex(ValueError, "historical"):
            execute_dokploy_target_setup(
                control_plane_root_path=self.root, record_store=self.store, request=self.request()
            )
        self.store.write_product_profile_record(self.profile)
        self.live["gitlabBranch"] = "unfinished-source"
        status, response = self.invoke_http()
        self.assertEqual(status, 400, response)
        self.assertEqual(self.calls, [])

    def test_inspect_never_exposes_url_credentials(self) -> None:
        self.live["customGitUrl"] = "https://secret:token@github.com/example/sample.git"
        summary = summarize_dokploy_target_payload(
            target_type="compose", target_id=self.target_id.target_id, payload=self.live
        )
        self.assertNotIn("custom_git_url", summary)


if __name__ == "__main__":
    unittest.main()
