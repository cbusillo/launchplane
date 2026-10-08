import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click
from fastapi import FastAPI
from httpx2 import Response

from control_plane.contracts.odoo_import_parameters import import_parameter_runtime_key
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
    OdooWebsiteBootstrapPayload,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_import_overrides import plan_import_override_reconciliation
from control_plane.odoo_instance_overrides import build_post_deploy_environment
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    GitHubHumanPolicyRule,
)
from control_plane.service_human_auth import (
    BROWSER_CSRF_HEADER_NAME,
    HumanSessionManager,
    InMemoryHumanSessionStore,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    OdooInstanceOverrideConflictError,
    RuntimeEnvironmentConflictError,
)
from tests.http_app_test_support import (
    _RejectingVerifier,
    _github_oauth_config,
    _github_human_identity,
    _browser_mutation_headers,
)
from tests.support.auth import _local_operator_policy
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import _sqlite_database_url
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy
from control_plane.workflows.odoo_post_deploy import _write_odoo_instance_override_apply_result
from control_plane.service_auth import TerminalAgentPolicyRule
from tests.test_odoo_post_deploy import _module_update_evidence

KEYS = ("cm_data.db.user", "repairshopr.sync_db.host", "repairshopr.sync_db.user")
PATH = "/v1/products/odoo-tenant-cm/environments/testing/odoo-import-overrides/reconcile"


def override_record(instance: str = "testing") -> OdooInstanceOverrideRecord:
    return OdooInstanceOverrideRecord(
        context="cm",
        instance=instance,
        config_parameters=tuple(
            OdooConfigParameterOverride(
                key=key, value=OdooOverrideValue(source="literal", value=f"stale-fixture-{key}")
            )
            for key in KEYS
        )
        + (
            OdooConfigParameterOverride(
                key="fishbowl.user",
                value=OdooOverrideValue(source="literal", value="fishbowl-fixture"),
            ),
            OdooConfigParameterOverride(
                key="cm_data.db.password",
                value=OdooOverrideValue(
                    source="secret_binding", secret_binding_id="cm-password-fixture"
                ),
            ),
        ),
        addon_settings=(
            OdooAddonSettingOverride(
                addon="other",
                setting="enabled",
                value=OdooOverrideValue(source="literal", value=True),
            ),
        ),
        website_bootstrap=OdooWebsiteBootstrapPayload(name="Fixture Website"),
        updated_at="2026-10-07T23:00:00Z",
        source_label="fixture",
    )


def runtime_record(instance: str = "testing") -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord(
        scope="instance",
        context="cm",
        instance=instance,
        env={import_parameter_runtime_key(key): f"current-fixture-{key}" for key in KEYS},
        updated_at="2026-10-07T23:00:00Z",
        source_label="fixture",
    )


class ImportOverrideHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(temporary.name) / "records.sqlite3")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane()
        )
        self.store.write_product_profile_record(self.profile)
        for instance in ("testing", "prod"):
            self.store.write_odoo_instance_override_record(override_record(instance))
            self.store.write_runtime_environment_record(runtime_record(instance))
        self.app = self.make_app(
            ("product_environment.read", "product_config.plan", "product_config.apply")
        )

    def make_app(self, actions: tuple[str, ...]) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            authz_policy=_local_operator_policy(
                actions=actions, products=(self.profile.product,), contexts=("cm",)
            ),
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )

    async def submit(
        self, payload: dict[str, object] | None = None, *, path: str = PATH
    ) -> Response:
        return await request(
            self.app,
            "GET" if payload is None else "POST",
            path,
            headers={"Authorization": "Bearer fixture-token"},
            payload=payload,
        )

    async def test_exact_testing_dry_run_is_inert_and_value_free(self) -> None:
        before = self.store.list_odoo_instance_override_records()
        runtime_before = self.store.list_runtime_environment_records()
        with patch.object(
            self.store,
            "write_product_authority_bundle",
            side_effect=AssertionError("planning wrote desired state"),
        ):
            read = await self.submit()
            planned = await self.submit({"mode": "dry-run", "keys": KEYS})
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(planned.status_code, 200, planned.text)
        result = planned.json()["result"]
        self.assertEqual(result, read.json()["result"])
        self.assertFalse(result["applied"])
        self.assertTrue(all(entry["stale_literal"] for entry in result["entries"]))
        self.assertEqual({entry["key"] for entry in result["entries"]}, set(KEYS))
        self.assertNotIn("current-fixture", planned.text)
        self.assertNotIn("stale-fixture", planned.text)
        self.assertNotIn("cm-password-fixture", planned.text)
        self.assertEqual(self.store.list_odoo_instance_override_records(), before)
        self.assertEqual(self.store.list_runtime_environment_records(), runtime_before)

    async def test_apply_preserves_other_settings_and_later_payload_uses_fresh_authority(
        self,
    ) -> None:
        before = override_record()
        production = override_record("prod")
        runtime_before = self.store.list_runtime_environment_records()
        plan = (await self.submit({"keys": KEYS})).json()["result"]
        applied = await self.submit(
            {
                "mode": "apply",
                "keys": KEYS,
                "review_digest": plan["review_digest"],
                "confirmation": "APPLY odoo-tenant-cm/testing",
            }
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertTrue(applied.json()["result"]["live_sync_required"])
        after = self.store.read_odoo_instance_override_record(
            context_name="cm", instance_name="testing"
        )
        self.assertEqual(after.addon_settings, before.addon_settings)
        self.assertEqual(after.website_bootstrap, before.website_bootstrap)
        self.assertEqual(after.apply_on, before.apply_on)
        self.assertEqual(after.last_apply, before.last_apply)
        self.assertEqual(after.source_label, before.source_label)
        self.assertEqual(
            after.config_parameters[len(KEYS) :], before.config_parameters[len(KEYS) :]
        )
        self.assertEqual(
            self.store.read_odoo_instance_override_record(context_name="cm", instance_name="prod"),
            production,
        )
        self.assertEqual(self.store.list_runtime_environment_records(), runtime_before)
        values = {import_parameter_runtime_key(key): f"next-fixture-{key}" for key in KEYS}
        self.store.write_runtime_environment_record(
            runtime_record().model_copy(update={"env": values})
        )
        environment = build_post_deploy_environment(after, record_store=self.store)
        payload = json.loads(
            base64.b64decode(environment.inline_environment["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"])
        )
        by_key = {entry["key"]: entry["value"] for entry in payload["config_parameters"]}
        for key in KEYS:
            self.assertEqual(
                by_key[key],
                {"source": "literal", "value": values[import_parameter_runtime_key(key)]},
            )
        self.assertNotIn("stale-fixture", json.dumps(payload))
        self.assertTrue(
            all(item.value.value is None for item in after.config_parameters if item.key in KEYS)
        )

    async def test_changed_authority_and_changed_override_refuse_stale_review(self) -> None:
        for changed in ("runtime", "override", "profile"):
            with self.subTest(changed=changed):
                self.store.write_runtime_environment_record(runtime_record())
                self.store.write_odoo_instance_override_record(override_record())
                self.store.write_product_profile_record(self.profile)
                review = (await self.submit({"keys": KEYS})).json()["result"]
                if changed == "runtime":
                    current = runtime_record()
                    self.store.write_runtime_environment_record(
                        current.model_copy(
                            update={
                                "env": {
                                    **current.env,
                                    import_parameter_runtime_key(KEYS[0]): "changed-fixture",
                                }
                            }
                        )
                    )
                elif changed == "override":
                    self.store.write_odoo_instance_override_record(
                        override_record().model_copy(update={"source_label": "changed"})
                    )
                else:
                    self.store.write_product_profile_record(
                        self.profile.model_copy(update={"display_name": "Changed"})
                    )
                before = self.store.list_odoo_instance_override_records()
                refused = await self.submit(
                    {
                        "mode": "apply",
                        "keys": KEYS,
                        "review_digest": review["review_digest"],
                        "confirmation": "APPLY odoo-tenant-cm/testing",
                    }
                )
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertEqual(self.store.list_odoo_instance_override_records(), before)

    async def test_plan_permission_cannot_apply(self) -> None:
        self.app = self.make_app(("product_config.plan",))
        review = await self.submit({"keys": KEYS})
        self.assertEqual(review.status_code, 200, review.text)
        refused = await self.submit(
            {
                "mode": "apply",
                "keys": KEYS,
                "review_digest": review.json()["result"]["review_digest"],
                "confirmation": "APPLY odoo-tenant-cm/testing",
            }
        )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name="cm", instance_name="testing"
            ),
            override_record(),
        )

    async def test_browser_apply_requires_origin_and_csrf_before_mutation(self) -> None:
        manager = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        human = _github_human_identity()
        session = manager.issue(human)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            human_session_manager=manager,
            authz_policy=LaunchplaneAuthzPolicy(
                github_humans=(
                    GitHubHumanPolicyRule(
                        github_ids=(human.github_id,),
                        products=(self.profile.product,),
                        contexts=("cm",),
                        actions=("product_environment.read", "product_config.apply"),
                    ),
                )
            ),
        )
        review = await request(
            app, "GET", PATH, headers={"Cookie": manager.session_cookie_header(session)}
        )
        self.assertEqual(review.status_code, 200, review.text)
        payload = {
            "mode": "apply",
            "keys": KEYS,
            "review_digest": review.json()["result"]["review_digest"],
            "confirmation": "APPLY odoo-tenant-cm/testing",
        }
        for missing in ("Origin", BROWSER_CSRF_HEADER_NAME):
            headers = _browser_mutation_headers(manager, session)
            headers.pop(missing)
            with patch.object(
                self.store,
                "write_product_authority_bundle",
                side_effect=AssertionError("unguarded cookie mutation"),
            ):
                refused = await request(app, "POST", PATH, headers=headers, payload=payload)
            self.assertEqual(refused.status_code, 403, refused.text)
        applied = await request(
            app, "POST", PATH, headers=_browser_mutation_headers(manager, session), payload=payload
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertTrue(applied.json()["result"]["applied"])

    async def test_terminal_read_credential_cannot_apply_even_with_a_grant(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="fixture-terminal-token",
                terminal_agent_subject="fixture-terminal",
                terminal_agent_token_label="fixture-terminal-read",
            ),
            authz_policy=LaunchplaneAuthzPolicy(
                terminal_agents=(
                    TerminalAgentPolicyRule(
                        subjects=("fixture-terminal",),
                        token_labels=("fixture-terminal-read",),
                        products=(self.profile.product,),
                        contexts=("cm",),
                        actions=(
                            "product_environment.read",
                            "product_config.plan",
                            "product_config.apply",
                        ),
                    ),
                )
            ),
        )
        headers = {"Authorization": "Bearer fixture-terminal-token"}
        review = await request(app, "POST", PATH, headers=headers, payload={"keys": KEYS})
        self.assertEqual(review.status_code, 200, review.text)
        denied = await request(
            app,
            "POST",
            PATH,
            headers=headers,
            payload={
                "mode": "apply",
                "keys": KEYS,
                "review_digest": review.json()["result"]["review_digest"],
                "confirmation": "APPLY odoo-tenant-cm/testing",
            },
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name="cm", instance_name="testing"
            ),
            override_record(),
        )

    async def test_secret_overlay_is_refused_without_reading_a_secret_value(self) -> None:
        binding = SecretBinding(
            binding_id="fixture-binding",
            secret_id="fixture-secret",
            integration="runtime_environment",
            binding_key=import_parameter_runtime_key(KEYS[0]),
            context="cm",
            instance="testing",
            created_at="fixture",
            updated_at="fixture",
        )
        with patch.object(self.store, "list_secret_bindings", return_value=(binding,)):
            response = await self.submit({"keys": KEYS})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn("fixture-secret", response.text)

    async def test_post_deploy_runner_receives_current_runtime_values_after_reconciliation(
        self,
    ) -> None:
        _plan, bundle = plan_import_override_reconciliation(
            record_store=self.store, profile=self.profile, record=override_record(), keys=KEYS
        )
        self.store.write_product_authority_bundle(bundle)
        runtime = runtime_record()
        latest = {key: f"latest-fixture-{key}" for key in runtime.env}
        self.store.write_runtime_environment_record(runtime.model_copy(update={"env": latest}))
        with (
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_source.read_control_plane_dokploy_source_of_truth",
                return_value=DokploySourceOfTruth(
                    schema_version=1,
                    targets=(
                        DokployTargetDefinition(
                            context="cm",
                            instance="testing",
                            target_type="compose",
                            target_id="fixture-compose",
                        ),
                    ),
                ),
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://provider.example.invalid", "fixture-token"),
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                return_value=_module_update_evidence(
                    odoo_instance_overrides_payload_present="true",
                    website_bootstrap_domain_matches_canonical="true",
                ),
            ) as runner,
        ):
            result = execute_odoo_post_deploy(
                control_plane_root=Path("."),
                record_store=self.store,
                request=OdooPostDeployRequest(context="cm", instance="testing"),
            )
        self.assertEqual(result.post_deploy_status, "pass", result.error_message)
        environment = runner.call_args.kwargs["workflow_environment_overrides"]
        payload = json.loads(base64.b64decode(environment["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"]))
        by_key = {entry["key"]: entry["value"] for entry in payload["config_parameters"]}
        for key in KEYS:
            self.assertEqual(by_key[key]["value"], latest[import_parameter_runtime_key(key)])
        self.assertNotIn("stale-fixture", json.dumps(payload))

    async def test_non_secret_scope_and_confirmation_fail_closed(self) -> None:
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"keys": ["cm_data.db.password"]}, PATH),
            ({"keys": [KEYS[0], KEYS[0]]}, PATH),
            ({"keys": KEYS}, PATH.replace("/testing/", "/prod/")),
            ({"keys": KEYS, "mode": "apply", "review_digest": "unreviewed"}, PATH),
        )
        for payload, path in cases:
            with self.subTest(payload=payload):
                response = await self.submit(payload, path=path)
                self.assertEqual(response.status_code, 400, response.text)
        self.store.write_runtime_environment_record(
            runtime_record().model_copy(update={"env": {"OTHER_SETTING": "fixture"}})
        )
        refused = await self.submit({"keys": KEYS})
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name="cm", instance_name="testing"
            ),
            override_record(),
        )


class ImportOverrideStorageTests(unittest.TestCase):
    def test_late_completion_preserves_reconciled_sources_and_stale_setting_write_is_refused(
        self,
    ) -> None:
        for backend in ("sqlite", "filesystem"):
            with self.subTest(backend=backend), TemporaryDirectory() as temporary:
                store = (
                    PostgresRecordStore(
                        database_url=_sqlite_database_url(Path(temporary) / "records.sqlite3")
                    )
                    if backend == "sqlite"
                    else FilesystemRecordStore(Path(temporary))
                )
                if isinstance(store, PostgresRecordStore):
                    store.ensure_schema()
                    self.addCleanup(store.close)
                profile = LaunchplaneProductProfileRecord.model_validate(
                    _odoo_profile_payload_with_prod_lane()
                )
                store.write_product_profile_record(profile)
                before = override_record()
                store.write_odoo_instance_override_record(before)
                store.write_runtime_environment_record(runtime_record())
                _plan, bundle = plan_import_override_reconciliation(
                    record_store=store, profile=profile, record=before, keys=KEYS
                )
                store.write_product_authority_bundle(bundle)
                completion = _write_odoo_instance_override_apply_result(
                    record_store=store,
                    record=before,
                    status="pass",
                    detail="late completion of an older payload",
                )
                self.assertEqual(completion.last_apply.status, "pass")
                self.assertTrue(
                    all(
                        item.value.source == "runtime_environment"
                        for item in completion.config_parameters
                        if item.key in KEYS
                    )
                )
                with self.assertRaises(OdooInstanceOverrideConflictError):
                    store.write_odoo_instance_override_record(
                        before.model_copy(update={"source_label": "late canonical writer"}),
                        expected_record=before,
                    )
                self.assertEqual(
                    store.read_odoo_instance_override_record(
                        context_name="cm", instance_name="testing"
                    ),
                    completion,
                )

    def test_commit_checks_runtime_and_override_snapshots_in_both_stores(self) -> None:
        for backend in ("sqlite", "filesystem"):
            for change in ("runtime", "override"):
                with (
                    self.subTest(backend=backend, change=change),
                    TemporaryDirectory() as temporary,
                ):
                    store = (
                        PostgresRecordStore(
                            database_url=_sqlite_database_url(Path(temporary) / "records.sqlite3")
                        )
                        if backend == "sqlite"
                        else FilesystemRecordStore(Path(temporary))
                    )
                    if isinstance(store, PostgresRecordStore):
                        store.ensure_schema()
                        self.addCleanup(store.close)
                    profile = LaunchplaneProductProfileRecord.model_validate(
                        _odoo_profile_payload_with_prod_lane()
                    )
                    store.write_product_profile_record(profile)
                    store.write_runtime_environment_record(runtime_record())
                    store.write_odoo_instance_override_record(override_record())
                    _plan, bundle = plan_import_override_reconciliation(
                        record_store=store, profile=profile, record=override_record(), keys=KEYS
                    )
                    if change == "runtime":
                        store.write_runtime_environment_record(
                            runtime_record().model_copy(update={"updated_at": "changed"})
                        )
                        error: type[ValueError] = RuntimeEnvironmentConflictError
                    else:
                        store.write_odoo_instance_override_record(
                            override_record().model_copy(update={"source_label": "changed"})
                        )
                        error = OdooInstanceOverrideConflictError
                    before = store.read_odoo_instance_override_record(
                        context_name="cm", instance_name="testing"
                    )
                    with self.assertRaises(error):
                        store.write_product_authority_bundle(bundle)
                    self.assertEqual(
                        store.read_odoo_instance_override_record(
                            context_name="cm", instance_name="testing"
                        ),
                        before,
                    )

    def test_render_refuses_missing_runtime_authority(self) -> None:
        record = override_record()
        first = record.config_parameters[0].model_copy(
            update={"value": OdooOverrideValue(source="runtime_environment")}
        )
        record = record.model_copy(
            update={"config_parameters": (first, *record.config_parameters[1:])}
        )
        with self.assertRaises(click.ClickException):
            build_post_deploy_environment(record)
