from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from typing import Any, cast

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.storage.postgres import PostgresRecordStore
import tests.test_service as service_tests
from tests.support.auth import _StubVerifier, _identity
from tests.support.stores import _sqlite_database_url
from control_plane.service_auth import LaunchplaneAuthzPolicy


class DokployTargetSetupHttpTests(unittest.TestCase):
    def test_repair_domain_authority_endpoint_applies_domain_only_change(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            target_record = DokployTargetRecord(
                context="synthetic-context",
                instance="synthetic-instance",
                target_type="application",
                target_name="synthetic-target",
                source_type="docker",
                custom_git_url="synthetic-source",
                env={"KEEP": "exact"},
                domains=("https://old.synthetic.invalid/path",),
                updated_at="2026-08-12T00:00:00Z",
                source_label="synthetic-source",
            )
            target_id_record = DokployTargetIdRecord(
                context=target_record.context,
                instance=target_record.instance,
                target_id="application-synthetic",
                updated_at=target_record.updated_at,
                source_label="synthetic-source",
            )
            provider_target = ProviderTargetRecord.from_dokploy_records(
                target_record=target_record,
                target_id_record=target_id_record,
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_dokploy_target_record(target_record)
            store.write_dokploy_target_id_record(target_id_record)
            store.write_provider_target_record(provider_target)
            store.close()

            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "synthetic-owner/synthetic-repo",
                            "workflow_refs": [
                                "synthetic-owner/synthetic-repo/.github/workflows/"
                                "dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            create_app = cast(Any, service_tests.create_launchplane_dokploy_target_setup_app)
            invoke_setup = cast(Any, service_tests._invoke_dokploy_target_setup_app)
            app = create_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="synthetic-owner/synthetic-repo",
                        workflow_ref=(
                            "synthetic-owner/synthetic-repo/.github/workflows/"
                            "dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            mutation = patch(
                "control_plane.dokploy_target_setup_http.mutate_dokploy_payload_for_target_setup",
                side_effect=AssertionError("repair must not mutate Dokploy"),
            )
            with (
                patch(
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
                    return_value=("https://provider.synthetic.invalid", "synthetic-token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                    return_value={"applicationId": "application-synthetic"},
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_target_domains_for_setup",
                    return_value=({"host": "new.synthetic.invalid"},),
                ),
                mutation,
            ):
                status_code, payload = invoke_setup(
                    app,
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "operation": "repair-domain-authority",
                        "product": "launchplane",
                        "context": "synthetic-context",
                        "instance": "synthetic-instance",
                        "target_type": "application",
                        "target_id": "application-synthetic",
                        "domains": ["new.synthetic.invalid"],
                        "expected_current_provider_target": provider_target.to_deployed_target_reference().model_dump(
                            mode="json"
                        ),
                        "confirmation": "APPLY DOKPLOY TARGET SETUP",
                        "reason": "Synthetic domain authority repair.",
                    },
                    headers={"Idempotency-Key": "synthetic-domain-repair"},
                )

            store = PostgresRecordStore(database_url=database_url)
            persisted = store.read_dokploy_target_record(
                context_name="synthetic-context",
                instance_name="synthetic-instance",
            )
            persisted_target_id = store.read_dokploy_target_id_record(
                context_name="synthetic-context",
                instance_name="synthetic-instance",
            )
            persisted_provider_target = store.read_provider_target_record(
                context_name="synthetic-context",
                instance_name="synthetic-instance",
            )
            store.close()

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["applied"])
        self.assertEqual(persisted.domains, ("new.synthetic.invalid",))
        self.assertEqual(persisted.source_type, "docker")
        self.assertEqual(persisted.custom_git_url, "synthetic-source")
        self.assertEqual(persisted.env, {"KEEP": "exact"})
        self.assertEqual(
            persisted_provider_target,
            ProviderTargetRecord.from_dokploy_records(
                target_record=persisted,
                target_id_record=persisted_target_id,
            ),
        )

    def test_repair_domain_authority_plan_action_allows_dry_run(self) -> None:
        status_code, payload = self._invoke_repair_with_actions(
            actions=["dokploy_target.repair_domain_authority.plan"],
            mode="dry-run",
        )

        self.assertEqual(status_code, 202)
        self.assertFalse(payload["result"]["applied"])

    def test_generic_plan_action_cannot_plan_domain_authority_repair(self) -> None:
        status_code, payload = self._invoke_repair_with_actions(
            actions=["dokploy_target.plan"],
            mode="dry-run",
        )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_repair_domain_authority_apply_action_allows_apply(self) -> None:
        status_code, payload = self._invoke_repair_with_actions(
            actions=["dokploy_target.repair_domain_authority"],
            mode="apply",
        )

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["applied"])

    def _invoke_repair_with_actions(
        self,
        *,
        actions: list[str],
        mode: str,
    ) -> tuple[int, dict[str, Any]]:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        database_url = _sqlite_database_url(root / "launchplane.sqlite3")
        target_record = DokployTargetRecord(
            context="synthetic-context",
            instance="synthetic-instance",
            target_type="application",
            target_name="synthetic-target",
            source_type="docker",
            domains=("https://old.synthetic.invalid",),
            updated_at="2026-08-12T00:00:00Z",
        )
        target_id_record = DokployTargetIdRecord(
            context=target_record.context,
            instance=target_record.instance,
            target_id="application-synthetic",
            updated_at=target_record.updated_at,
        )
        provider_target = ProviderTargetRecord.from_dokploy_records(
            target_record=target_record,
            target_id_record=target_id_record,
        )
        store = PostgresRecordStore(database_url=database_url)
        store.ensure_schema()
        store.write_dokploy_target_record(target_record)
        store.write_dokploy_target_id_record(target_id_record)
        store.write_provider_target_record(provider_target)
        store.close()
        workflow_ref = (
            "synthetic-owner/synthetic-repo/.github/workflows/"
            "dokploy-target-setup.yml@refs/heads/main"
        )
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "synthetic-owner/synthetic-repo",
                        "workflow_refs": [workflow_ref],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": actions,
                    }
                ]
            }
        )
        create_app = cast(Any, service_tests.create_launchplane_dokploy_target_setup_app)
        invoke_setup = cast(Any, service_tests._invoke_dokploy_target_setup_app)
        app = create_app(
            state_dir=root / "state",
            verifier=_StubVerifier(
                _identity(
                    repository="synthetic-owner/synthetic-repo",
                    workflow_ref=workflow_ref,
                    event_name="workflow_dispatch",
                )
            ),
            authz_policy=policy,
            control_plane_root_path=root,
            database_url=database_url,
        )
        payload: dict[str, object] = {
            "schema_version": 1,
            "mode": mode,
            "operation": "repair-domain-authority",
            "product": "launchplane",
            "context": target_record.context,
            "instance": target_record.instance,
            "target_type": "application",
            "target_id": target_id_record.target_id,
            "domains": ["new.synthetic.invalid"],
            "expected_current_provider_target": (
                provider_target.to_deployed_target_reference().model_dump(mode="json")
            ),
        }
        headers: dict[str, str] = {}
        if mode == "apply":
            payload.update(
                {
                    "confirmation": "APPLY DOKPLOY TARGET SETUP",
                    "reason": "Synthetic domain authority repair.",
                }
            )
            headers["Idempotency-Key"] = "synthetic-domain-repair-action"
        with (
            patch(
                "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
                return_value=("https://provider.synthetic.invalid", "synthetic-token"),
            ),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                return_value={"applicationId": target_id_record.target_id},
            ),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_domains_for_setup",
                return_value=({"host": "new.synthetic.invalid"},),
            ),
        ):
            return cast(
                tuple[int, dict[str, Any]],
                invoke_setup(app, payload=payload, headers=headers),
            )


if __name__ == "__main__":
    unittest.main()
