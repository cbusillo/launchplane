import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click
from fastapi import FastAPI
from httpx2 import Response

from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_instance_overrides import (
    build_post_deploy_environment,
    config_parameter_secret_env_key,
)
from control_plane.odoo_secret_references import plan_secret_reference_reconciliation
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    GitHubHumanPolicyRule,
    TerminalAgentPolicyRule,
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
    ProductProfileConflictError,
    RuntimeEnvironmentConflictError,
    SecretDeliveryConflictError,
)
from tests.http_app_test_support import (
    _RejectingVerifier,
    _github_human_identity,
    _github_oauth_config,
    _browser_mutation_headers,
)
from tests.support.auth import _local_operator_policy
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import _sqlite_database_url

KEY = "cm_data.db.password"
PATH = "/v1/products/odoo-tenant-cm/environments/prod/odoo-secret-references/reconcile"
STAMP = "2026-10-16T12:00:00Z"
Store = FilesystemRecordStore | PostgresRecordStore


def seed(store: Store) -> tuple[LaunchplaneProductProfileRecord, OdooInstanceOverrideRecord]:
    profile = LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload_with_prod_lane())
    store.write_product_profile_record(profile)
    for instance, name in (("", "context-password"), ("prod", "lane-password")):
        store.write_secret_record(
            SecretRecord(
                secret_id=name,
                scope="context_instance" if instance else "context",
                integration="runtime_environment",
                name=name,
                context="cm",
                instance=instance,
                current_version_id=f"{name}-version",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        store.write_secret_binding(
            SecretBinding(
                binding_id=name,
                secret_id=name,
                integration="runtime_environment",
                binding_key=config_parameter_secret_env_key(KEY),
                context="cm",
                instance=instance,
                declared_secret_class="prod_only" if instance else None,
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
    record = OdooInstanceOverrideRecord(
        context="cm",
        instance="prod",
        config_parameters=(
            OdooConfigParameterOverride(
                key=KEY,
                value=OdooOverrideValue(
                    source="secret_binding", secret_binding_id="context-password"
                ),
            ),
            OdooConfigParameterOverride(
                key="unrelated",
                value=OdooOverrideValue(source="literal", value="private-unrelated-literal"),
            ),
        ),
        updated_at=STAMP,
        source_label="fixture",
    )
    store.write_odoo_instance_override_record(record)
    return profile, record


class SecretReferenceStorageTests(unittest.TestCase):
    def test_metadata_only_correction_and_rollback_in_both_stores(self) -> None:
        for sql in (False, True):
            with self.subTest(sql=sql), TemporaryDirectory() as directory:
                store: Store = (
                    PostgresRecordStore(
                        database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
                    )
                    if sql
                    else FilesystemRecordStore(Path(directory))
                )
                if isinstance(store, PostgresRecordStore):
                    store.ensure_schema()
                    self.addCleanup(store.close)
                profile, before = seed(store)
                secrets = store.list_secret_records()
                bindings = store.list_secret_bindings()
                plan, bundle = plan_secret_reference_reconciliation(
                    store=store, profile=profile, record=before, keys=(KEY,)
                )
                self.assertEqual(plan.entries[0].action, "reconcile_reference")
                for private in ("context-password", "lane-password", "private-unrelated-literal"):
                    self.assertNotIn(private, plan.model_dump_json())
                with self.assertRaises(click.ClickException):
                    build_post_deploy_environment(before, record_store=store)
                # Exercise the store's real failure boundary before any publication/commit.
                hook = "_after_product_authority_bundle_step"
                with patch.object(store, hook, side_effect=RuntimeError("injected failure")):
                    with self.assertRaises(RuntimeError):
                        store.write_product_authority_bundle(bundle)
                self.assertEqual(
                    store.read_odoo_instance_override_record(
                        context_name="cm", instance_name="prod"
                    ),
                    before,
                )
                store.write_product_authority_bundle(bundle)
                after = store.read_odoo_instance_override_record(
                    context_name="cm", instance_name="prod"
                )
                self.assertEqual(after, bundle.odoo_instance_override_writes[0].record)
                self.assertEqual(
                    after.config_parameters[0].value.secret_binding_id, "lane-password"
                )
                self.assertEqual(
                    after.model_dump(exclude={"config_parameters"}),
                    before.model_dump(exclude={"config_parameters"}),
                )
                self.assertEqual(after.config_parameters[1:], before.config_parameters[1:])
                self.assertEqual(store.list_secret_records(), secrets)
                self.assertEqual(store.list_secret_bindings(), bindings)
                environment = build_post_deploy_environment(after, record_store=store)
                self.assertEqual(
                    environment.payload.config_parameters[0].value.secret_binding_id,
                    "lane-password",
                )
                _, retry = plan_secret_reference_reconciliation(
                    store=store, profile=profile, record=after, keys=(KEY,)
                )
                self.assertFalse(retry.requires_write())

    def test_transaction_guards_insertions_mutations_and_unrelated_override_edits(self) -> None:
        for sql in (False, True):
            for change in (
                "binding",
                "insert",
                "version",
                "runtime",
                "override",
                "scalar_type",
                "profile",
            ):
                with self.subTest(sql=sql, change=change), TemporaryDirectory() as directory:
                    store: Store = (
                        PostgresRecordStore(
                            database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
                        )
                        if sql
                        else FilesystemRecordStore(Path(directory))
                    )
                    if isinstance(store, PostgresRecordStore):
                        store.ensure_schema()
                        self.addCleanup(store.close)
                    profile, before = seed(store)
                    if change == "scalar_type":
                        item = before.config_parameters[1]
                        before = before.model_copy(
                            update={
                                "config_parameters": (
                                    before.config_parameters[0],
                                    item.model_copy(
                                        update={
                                            "value": item.value.model_copy(update={"value": True})
                                        }
                                    ),
                                )
                            }
                        )
                        store.write_odoo_instance_override_record(before)
                    _, bundle = plan_secret_reference_reconciliation(
                        store=store, profile=profile, record=before, keys=(KEY,)
                    )
                    expected_error: type[ValueError] = SecretDeliveryConflictError
                    if change in {"binding", "insert"}:
                        binding = next(b for b in store.list_secret_bindings() if b.instance)
                        store.write_secret_binding(
                            binding.model_copy(
                                update={
                                    "binding_id": "new-candidate"
                                    if change == "insert"
                                    else binding.binding_id,
                                    "status": "configured" if change == "insert" else "disabled",
                                }
                            )
                        )
                    elif change == "version":
                        secret = store.read_secret_record("lane-password")
                        store.write_secret_record(
                            secret.model_copy(update={"current_version_id": "rotated"})
                        )
                    elif change == "runtime":
                        store.write_runtime_environment_record(
                            RuntimeEnvironmentRecord(
                                schema_version=2,
                                scope="instance",
                                context="cm",
                                instance="prod",
                                env={},
                                retired_provider_keys=(config_parameter_secret_env_key(KEY),),
                                updated_at=STAMP,
                            )
                        )
                        expected_error = RuntimeEnvironmentConflictError
                    elif change == "scalar_type":
                        item = before.config_parameters[1]
                        store.write_odoo_instance_override_record(
                            before.model_copy(
                                update={
                                    "config_parameters": (
                                        before.config_parameters[0],
                                        item.model_copy(
                                            update={
                                                "value": item.value.model_copy(update={"value": 1})
                                            }
                                        ),
                                    )
                                }
                            )
                        )
                        expected_error = OdooInstanceOverrideConflictError
                    elif change == "override":
                        store.write_odoo_instance_override_record(
                            before.model_copy(
                                update={"source_label": "concurrent unrelated intent"}
                            )
                        )
                        expected_error = OdooInstanceOverrideConflictError
                    else:
                        store.write_product_profile_record(
                            profile.model_copy(update={"display_name": "Changed"})
                        )
                        expected_error = ProductProfileConflictError
                    current = store.read_odoo_instance_override_record(
                        context_name="cm", instance_name="prod"
                    )
                    if change == "scalar_type":
                        self.assertIs(type(current.config_parameters[1].value.value), int)
                        with self.assertRaises(OdooInstanceOverrideConflictError):
                            store.write_odoo_instance_override_record(
                                bundle.odoo_instance_override_writes[0].record,
                                expected_record=before,
                            )
                    with self.assertRaises(expected_error):
                        store.write_product_authority_bundle(bundle)
                    self.assertEqual(
                        store.read_odoo_instance_override_record(
                            context_name="cm", instance_name="prod"
                        ),
                        current,
                    )
                    if change == "scalar_type":
                        self.assertIs(
                            type(
                                store.read_odoo_instance_override_record(
                                    context_name="cm", instance_name="prod"
                                )
                                .config_parameters[1]
                                .value.value
                            ),
                            int,
                        )

    def test_unsafe_selection_refuses_and_context_precedence_is_supported(self) -> None:
        for change in (
            "context",
            "missing",
            "disabled",
            "retired",
            "ambiguous",
            "wrong_context",
            "wrong_key",
            "global",
        ):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                profile, record = seed(store)
                lane = next(b for b in store.list_secret_bindings() if b.instance)
                if change in {"context", "missing", "disabled"}:
                    store.write_secret_binding(lane.model_copy(update={"status": "disabled"}))
                    if change == "missing":
                        record = record.model_copy(
                            update={
                                "config_parameters": (
                                    record.config_parameters[0].model_copy(
                                        update={
                                            "value": OdooOverrideValue(
                                                source="secret_binding", secret_binding_id="missing"
                                            )
                                        }
                                    ),
                                )
                            }
                        )
                    if change == "disabled":
                        source = next(b for b in store.list_secret_bindings() if not b.instance)
                        store.write_secret_binding(source.model_copy(update={"status": "disabled"}))
                elif change == "retired":
                    store.write_runtime_environment_record(
                        RuntimeEnvironmentRecord(
                            schema_version=2,
                            scope="instance",
                            context="cm",
                            instance="prod",
                            env={},
                            updated_at=STAMP,
                            retired_provider_keys=(lane.binding_key,),
                        )
                    )
                elif change == "ambiguous":
                    store.write_secret_binding(lane.model_copy(update={"binding_id": "duplicate"}))
                else:
                    source = next(b for b in store.list_secret_bindings() if not b.instance)
                    if change == "global":
                        secret = store.read_secret_record(source.secret_id)
                        store.write_secret_record(
                            secret.model_copy(update={"scope": "global", "context": ""})
                        )
                    else:
                        store.write_secret_binding(
                            source.model_copy(
                                update={
                                    "context"
                                    if change == "wrong_context"
                                    else "binding_key": "wrong"
                                }
                            )
                        )
                if change == "context":
                    plan, bundle = plan_secret_reference_reconciliation(
                        store=store, profile=profile, record=record, keys=(KEY,)
                    )
                    self.assertEqual(plan.entries[0].action, "already_agrees")
                    self.assertFalse(bundle.requires_write())
                else:
                    with self.assertRaises(ValueError):
                        plan_secret_reference_reconciliation(
                            store=store, profile=profile, record=record, keys=(KEY,)
                        )


class SecretReferenceHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(temporary.name) / "records.sqlite3")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile, self.record = seed(self.store)
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

    async def submit(self, payload: dict[str, object] | None = None, path: str = PATH) -> Response:
        return await request(
            self.app,
            "GET" if payload is None else "POST",
            path,
            headers={"Authorization": "Bearer fixture-token"},
            payload=payload,
        )

    async def test_review_apply_readback_and_fresh_noop_are_value_free(self) -> None:
        with patch.object(
            self.store, "read_secret_version", side_effect=AssertionError("secret value access")
        ):
            before = await self.submit()
            plan = await self.submit({"keys": (KEY,)})
            self.assertEqual(plan.status_code, 200, plan.text)
            self.assertEqual(before.json()["result"], plan.json()["result"])
            self.assertEqual(
                self.store.read_odoo_instance_override_record(
                    context_name="cm", instance_name="prod"
                ),
                self.record,
            )
            body = {
                "mode": "apply",
                "keys": (KEY,),
                "review_digest": plan.json()["result"]["review_digest"],
                "confirmation": "APPLY odoo-tenant-cm/prod",
            }
            applied = await self.submit(body)
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertTrue(applied.json()["result"]["read_back_matches"])
            self.assertTrue(applied.json()["result"]["applied"])
            readback = await self.submit()
            self.assertEqual(readback.json()["result"]["entries"][0]["action"], "already_agrees")
            body["review_digest"] = readback.json()["result"]["review_digest"]
            with patch.object(
                self.store,
                "write_product_authority_bundle",
                side_effect=AssertionError("noop write"),
            ):
                retry = await self.submit(body)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertFalse(retry.json()["result"]["applied"])
            for response in (before, plan, applied, readback, retry):
                for private in ("context-password", "lane-password", "private-unrelated-literal"):
                    self.assertNotIn(private, response.text)

    async def test_authorization_confirmation_scope_and_stale_reviews_refuse(self) -> None:
        planned = (await self.submit({"keys": (KEY,)})).json()["result"]
        body = {
            "mode": "apply",
            "keys": (KEY,),
            "review_digest": planned["review_digest"],
            "confirmation": "APPLY odoo-tenant-cm/prod",
        }
        self.app = self.make_app(("product_environment.read", "product_config.plan"))
        self.assertEqual((await self.submit(body)).status_code, 403)
        self.app = self.make_app(
            ("product_environment.read", "product_config.plan", "product_config.apply")
        )
        self.assertEqual(
            (await self.submit({**body, "confirmation": "APPLY wrong/prod"})).status_code, 400
        )
        self.assertEqual(
            (await self.submit(body, PATH.replace("odoo-tenant-cm", "other-product"))).status_code,
            400,
        )
        self.store.write_secret_record(
            self.store.read_secret_record("lane-password").model_copy(
                update={"current_version_id": "rotated"}
            )
        )
        self.assertEqual((await self.submit(body)).status_code, 409)
        self.assertEqual(
            self.store.read_odoo_instance_override_record(context_name="cm", instance_name="prod"),
            self.record,
        )

    async def test_browser_csrf_and_terminal_apply_boundaries(self) -> None:
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
            "keys": (KEY,),
            "review_digest": review.json()["result"]["review_digest"],
            "confirmation": "APPLY odoo-tenant-cm/prod",
        }
        for missing in ("Origin", BROWSER_CSRF_HEADER_NAME):
            headers = _browser_mutation_headers(manager, session)
            headers.pop(missing)
            with patch.object(
                self.store,
                "write_product_authority_bundle",
                side_effect=AssertionError("unguarded write"),
            ):
                refused = await request(app, "POST", PATH, headers=headers, payload=payload)
            self.assertEqual(refused.status_code, 403, refused.text)
        applied = await request(
            app, "POST", PATH, headers=_browser_mutation_headers(manager, session), payload=payload
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-fixture",
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
        headers = {"Authorization": "Bearer terminal-fixture"}
        review = await request(app, "POST", PATH, headers=headers, payload={"keys": (KEY,)})
        self.assertEqual(review.status_code, 200, review.text)
        refused = await request(
            app,
            "POST",
            PATH,
            headers=headers,
            payload={**payload, "review_digest": review.json()["result"]["review_digest"]},
        )
        self.assertEqual(refused.status_code, 403, refused.text)

    async def test_invalid_reference_does_not_echo_stored_or_submitted_secrets(self) -> None:
        sentinel = "private-misplaced-credential"
        invalid = self.record.model_copy(
            update={
                "config_parameters": (
                    self.record.config_parameters[0].model_copy(
                        update={
                            "value": OdooOverrideValue(
                                source="secret_binding", secret_binding_id=sentinel
                            )
                        }
                    ),
                )
            }
        )
        self.store.write_odoo_instance_override_record(invalid)
        payloads: tuple[dict[str, object] | None, ...] = (
            None,
            {"keys": (KEY,)},
            {"keys": (sentinel,)},
        )
        for payload in payloads:
            result = await self.submit(payload)
            self.assertEqual(result.status_code, 400, result.text)
            self.assertNotIn(sentinel, result.text)

    async def test_lane_without_secret_parameters_has_an_empty_read_plan(self) -> None:
        self.store.write_odoo_instance_override_record(
            self.record.model_copy(update={"config_parameters": self.record.config_parameters[1:]})
        )
        result = await self.submit()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["result"]["entries"], [])
        self.assertFalse(result.json()["result"]["applied"])


class SecretReferenceCliTests(unittest.TestCase):
    def test_compose_post_deploy_checks_secret_only_config_parameters(self) -> None:
        from control_plane.cli import _run_compose_post_deploy_update
        from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
        from tests.test_odoo_instance_overrides import _ship_request

        for agrees in (False, True):
            with self.subTest(agrees=agrees), TemporaryDirectory() as directory:
                url = _sqlite_database_url(Path(directory) / "records.sqlite3")
                store = PostgresRecordStore(database_url=url)
                self.addCleanup(store.close)
                store.ensure_schema()
                profile, record = seed(store)
                if agrees:
                    _, bundle = plan_secret_reference_reconciliation(
                        store=store, profile=profile, record=record, keys=(KEY,)
                    )
                    store.write_product_authority_bundle(bundle)
                source = DokploySourceOfTruth(
                    schema_version=1,
                    targets=(
                        DokployTargetDefinition(
                            context="cm",
                            instance="prod",
                            target_type="compose",
                            target_name="fixture-prod",
                            target_id="fixture-target",
                        ),
                    ),
                )
                with (
                    patch.dict("os.environ", {"LAUNCHPLANE_DATABASE_URL": url}),
                    patch(
                        "control_plane.dokploy.source.read_dokploy_config",
                        return_value=("https://provider.invalid", "fixture"),
                    ),
                    patch(
                        "control_plane.dokploy.source.read_control_plane_dokploy_source_of_truth",
                        return_value=source,
                    ),
                    patch(
                        "control_plane.dokploy.post_deploy.run_compose_post_deploy_update"
                    ) as runner,
                    patch.object(
                        PostgresRecordStore,
                        "read_secret_version",
                        side_effect=AssertionError("value read"),
                    ),
                ):
                    request = _ship_request().model_copy(update={"context": "cm"})
                    if agrees:
                        _run_compose_post_deploy_update(env_file=None, request=request)
                        runner.assert_called_once()
                    else:
                        with self.assertRaises(click.ClickException):
                            _run_compose_post_deploy_update(env_file=None, request=request)
                        runner.assert_not_called()
                stored = store.read_odoo_instance_override_record(
                    context_name="cm", instance_name="prod"
                )
                self.assertEqual(stored.last_apply.status, "pass" if agrees else "fail")
