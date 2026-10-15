import base64
from control_plane._odoo_website_settings_program import WEBSITE_INDEXING_PARAMETER
import json
import unittest
import click
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.odoo_instance_override_record import OdooOverrideApplyResult
from control_plane.odoo_instance_overrides import build_post_deploy_environment
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.odoo_website_settings import (
    OdooWebsiteSettings,
    OdooWebsiteSettingsIntent,
)
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.odoo_website_settings import (
    OdooWebsiteSettingsRequest,
    OdooWebsiteSettingsStale,
    plan_website_settings,
    prepare_website_settings,
)
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane.release_review import build_release_review
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy
from tests.test_release_review import (
    seed as release_seed,
    profile as release_profile,
    github_read,
    decision,
)
from tests.test_odoo_post_deploy import _module_update_evidence


def profile() -> LaunchplaneProductProfileRecord:
    value = release_profile()
    return value.model_copy(
        update={
            "lanes": tuple(
                lane.model_copy(update={"base_url": "https://site.example"})
                if lane.instance == "prod"
                else lane
                for lane in value.lanes
            )
        }
    )


def seed(store: FilesystemRecordStore) -> None:
    release_seed(store)
    store.write_product_profile_record(profile())


def intent(*, key: str = "G-TEST123") -> OdooWebsiteSettingsIntent:
    return OdooWebsiteSettingsIntent(
        artifact_id="artifact-testing",
        settings=OdooWebsiteSettings(
            website_id=1,
            public_origin=next(
                lane.base_url for lane in profile().lanes if lane.instance == "prod"
            ),
            public_indexing_enabled=True,
            google_analytics_key=key,
        ),
    )


def request() -> OdooWebsiteSettingsRequest:
    return OdooWebsiteSettingsRequest(
        product="example-site",
        context="example-site",
        website_settings=intent(),
        reason="Test release",
    )


def prepare(store: FilesystemRecordStore) -> None:
    value = request()
    plan = plan_website_settings(store, value)
    prepare_website_settings(
        store,
        value.model_copy(update={"mode": "apply", "reviewed_plan_sha256": plan["plan_sha256"]}),
    )


class WebsiteSettingsTests(unittest.TestCase):
    def test_preparation_and_dry_run_do_not_activate_or_supply_acceptance(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            plan_website_settings(store, request())
            with self.assertRaises(FileNotFoundError):
                store.read_odoo_instance_override_record(
                    context_name="example-site", instance_name="prod"
                )
            prepare(store)
            self.assertFalse(
                build_release_review(store=store, profile=profile(), read=github_read).approved
            )
            stored = store.read_odoo_instance_override_record(
                context_name="example-site", instance_name="prod"
            )
            self.assertFalse(stored.last_apply.attempted)

    def test_pending_intent_is_absent_from_deploy_and_legacy_ship_payloads(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            prepare(store)
            record = store.read_odoo_instance_override_record(
                context_name="example-site", instance_name="prod"
            )
            payload = build_post_deploy_environment(record, record_store=store).payload
            self.assertIsNone(payload.website_settings)
            self.assertFalse(
                any(item.key == WEBSITE_INDEXING_PARAMETER for item in payload.config_parameters)
            )

    def test_generic_apply_success_cannot_confirm_failed_website_settings(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            prepare(store)
            accepted = decision(store)
            store.create_release_review_decision_record_if_absent(accepted)
            self.assertTrue(
                build_release_review(store=store, profile=profile(), read=github_read).approved
            )
            record = store.read_odoo_instance_override_record(
                context_name="example-site", instance_name="prod"
            )
            record.last_apply = OdooOverrideApplyResult(
                attempted=True,
                status="pass",
                website_settings=intent(),
                website_settings_verified=False,
                applied_at=record.updated_at,
            )
            store.write_odoo_instance_override_record(record)
            from control_plane.cli import _write_odoo_instance_override_apply_result as legacy_write
            from types import SimpleNamespace
            from unittest.mock import Mock

            adapter = SimpleNamespace(
                ensure_schema=Mock(),
                close=Mock(),
                update_odoo_instance_override_apply_result=store.update_odoo_instance_override_apply_result,
            )
            with patch("control_plane.cli.PostgresRecordStore", return_value=adapter):
                legacy_write(
                    database_url="fixture",
                    record=record,
                    status="pass",
                    detail="Unrelated successful legacy ship",
                )
            persisted = store.read_odoo_instance_override_record(
                context_name="example-site", instance_name="prod"
            )
            self.assertEqual(
                persisted.last_apply.website_settings, record.last_apply.website_settings
            )
            self.assertFalse(persisted.last_apply.website_settings_verified)
            self.assertFalse(
                build_release_review(store=store, profile=profile(), read=github_read).approved
            )

    def test_stale_plan_cannot_replace_current_intent(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            value = request()
            old = plan_website_settings(store, value)
            prepare(store)
            with self.assertRaises(OdooWebsiteSettingsStale):
                prepare_website_settings(
                    store,
                    value.model_copy(
                        update={"mode": "apply", "reviewed_plan_sha256": old["plan_sha256"]}
                    ),
                )

    def test_wrong_production_origin_is_refused_without_preparing_intent(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            bad = request().model_copy(
                update={
                    "website_settings": intent().model_copy(
                        update={
                            "settings": intent().settings.model_copy(
                                update={"public_origin": "https://other.example"}
                            )
                        }
                    )
                }
            )
            with self.assertRaises(ValueError):
                plan_website_settings(store, bad)
            with self.assertRaises(FileNotFoundError):
                store.read_odoo_instance_override_record(
                    context_name="example-site", instance_name="prod"
                )

    def test_other_product_artifact_and_nonproduction_enablement_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            OdooWebsiteSettingsRequest.model_validate(
                {**request().model_dump(), "instance": "testing"}
            )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            with self.assertRaises(ValueError):
                plan_website_settings(
                    store,
                    request().model_copy(
                        update={
                            "website_settings": intent().model_copy(
                                update={"artifact_id": "other-product-artifact"}
                            )
                        }
                    ),
                )

    def test_changed_settings_invalidate_acceptance_without_changing_the_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            prepare(store)
            accepted = decision(store)
            store.create_release_review_decision_record_if_absent(accepted)
            self.assertTrue(
                build_release_review(store=store, profile=profile(), read=github_read).approved
            )
            stored = store.read_odoo_instance_override_record(
                context_name="example-site", instance_name="prod"
            )
            store.write_odoo_instance_override_record(
                stored.model_copy(update={"website_settings": intent(key="G-NEXT456")})
            )
            changed = build_release_review(store=store, profile=profile(), read=github_read)
            self.assertFalse(changed.approved)
            self.assertNotEqual(changed.checklist_digest, accepted.checklist_digest)
            assert changed.checklist is not None
            self.assertNotEqual(
                changed.checklist.website_settings, accepted.checklist.website_settings
            )

    def test_release_renderer_uses_accepted_snapshot_and_rollback_clears_forward_settings(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            prepare(store)
            accepted = decision(store)
            store.create_release_review_decision_record_if_absent(accepted)
            stored = store.read_odoo_instance_override_record(
                context_name="example-site", instance_name="prod"
            )
            store.write_odoo_instance_override_record(
                stored.model_copy(update={"website_settings": intent(key="G-UNACCEPTED")})
            )
            captured = []

            def apply(**kwargs: object) -> dict[str, str]:
                environment = kwargs["workflow_environment_overrides"]
                assert isinstance(environment, dict)
                payload = json.loads(
                    base64.b64decode(environment[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY])
                )
                captured.append(payload)
                return _module_update_evidence(
                    website_settings_sha256=OdooWebsiteSettings.model_validate(
                        payload["website_settings"]
                    ).digest
                )

            target = DokployTargetDefinition(
                context="example-site",
                instance="prod",
                domains=("site.example",),
                public_hosts=("site.example",),
            )
            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy._resolve_compose_target_definition",
                    return_value=target,
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                    return_value=("https://provider.example", "test-token"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                    side_effect=apply,
                ),
            ):
                released = execute_odoo_post_deploy(
                    artifact_id="artifact-testing",
                    release_website_settings=accepted.checklist.website_settings,
                    release_operation=True,
                    control_plane_root=Path(directory),
                    record_store=store,
                    request=OdooPostDeployRequest(context="example-site", instance="prod"),
                )
                maintenance = execute_odoo_post_deploy(
                    artifact_id="artifact-testing",
                    control_plane_root=Path(directory),
                    record_store=store,
                    request=OdooPostDeployRequest(context="example-site", instance="prod"),
                )
                rollback = execute_odoo_post_deploy(
                    artifact_id="artifact-prod",
                    release_operation=True,
                    rollback=True,
                    control_plane_root=Path(directory),
                    record_store=store,
                    request=OdooPostDeployRequest(context="example-site", instance="prod"),
                )
            self.assertEqual(released.post_deploy_status, "pass")
            self.assertEqual(rollback.post_deploy_status, "pass")
            self.assertEqual(maintenance.post_deploy_status, "pass")
            self.assertEqual(captured[0]["website_settings"], captured[1]["website_settings"])
            self.assertEqual(
                captured[0]["website_settings"]["google_analytics_key"],
                intent().settings.google_analytics_key,
            )
            self.assertEqual(captured[2]["website_settings"]["google_analytics_key"], "")
            self.assertFalse(captured[2]["website_settings"]["public_indexing_enabled"])
            self.assertIsNone(captured[2]["website_settings"]["cookies_bar"])
            legacy_parameters = {
                item["key"]: item["value"]["value"] for item in captured[2]["config_parameters"]
            }
            self.assertEqual(legacy_parameters[WEBSITE_INDEXING_PARAMETER], "0")
            self.assertIsNone(captured[2]["website_settings"]["block_third_party_domains"])
            for digest in ("", "f" * 64):
                with (
                    self.subTest(digest=digest),
                    patch(
                        "control_plane.workflows.odoo_post_deploy._resolve_compose_target_definition",
                        return_value=target,
                    ),
                    patch(
                        "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                        return_value=("https://provider.example", "test-token"),
                    ),
                    patch(
                        "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                        return_value=_module_update_evidence(website_settings_sha256=digest),
                    ),
                ):
                    failed = execute_odoo_post_deploy(
                        artifact_id="artifact-testing",
                        release_operation=True,
                        release_website_settings=accepted.checklist.website_settings,
                        control_plane_root=Path(directory),
                        record_store=store,
                        request=OdooPostDeployRequest(context="example-site", instance="prod"),
                    )
                    self.assertEqual(failed.post_deploy_status, "fail")
                    review = build_release_review(store=store, profile=profile(), read=github_read)
                    self.assertFalse(review.approved)
                    self.assertTrue(review.blockers)
            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy._resolve_compose_target_definition",
                    return_value=target,
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                    return_value=("https://provider.example", "test-token"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                    side_effect=apply,
                ),
            ):
                with self.assertRaises(click.ClickException):
                    execute_odoo_post_deploy(
                        artifact_id="artifact-prod",
                        control_plane_root=Path(directory),
                        record_store=store,
                        request=OdooPostDeployRequest(context="example-site", instance="prod"),
                    )
                recovered = execute_odoo_post_deploy(
                    artifact_id="artifact-testing",
                    control_plane_root=Path(directory),
                    record_store=store,
                    request=OdooPostDeployRequest(context="example-site", instance="prod"),
                )
            self.assertEqual(recovered.post_deploy_status, "pass")
            self.assertEqual(captured[-1]["website_settings"], captured[0]["website_settings"])
            restored = store.read_odoo_instance_override_record(
                context_name="example-site", instance_name="prod"
            )
            self.assertEqual(
                restored.last_apply.website_settings, accepted.checklist.website_settings
            )

    def test_unpublished_acceptance_cannot_activate_prepared_settings(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            prepare(store)
            accepted = decision(store).model_copy(update={"release_issue_url": ""})
            store.create_release_review_decision_record_if_absent(accepted)
            self.assertFalse(
                build_release_review(store=store, profile=profile(), read=github_read).approved
            )

    def test_new_testing_build_reports_stale_prepared_settings(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            seed(store)
            prepare(store)
            testing = store.read_release_tuple_record(
                context_name="example-site", channel_name="testing"
            )
            original = store.read_artifact_manifest(testing.artifact_id)
            store.write_artifact_manifest(
                original.model_copy(update={"artifact_id": "new-testing-artifact"})
            )
            store.write_release_tuple_record(
                testing.model_copy(update={"artifact_id": "new-testing-artifact"})
            )
            review = build_release_review(store=store, profile=profile(), read=github_read)
            self.assertFalse(review.approved)
            self.assertTrue(review.blockers)
            self.assertIsNone(review.checklist.website_settings if review.checklist else None)

    def test_analytics_requires_explicit_native_consent_controls(self) -> None:
        for values in (
            {"cookies_bar": False},
            {"block_third_party_domains": False},
            {"google_analytics_key": "UA-legacy"},
            {"cookies_bar": "true"},
            {"website_id": True},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                OdooWebsiteSettings.model_validate({**intent().settings.model_dump(), **values})


class WebsiteSettingsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_requires_authority_and_reviewed_idempotent_plan(self) -> None:
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.odoo_website_settings import ODOO_WEBSITE_SETTINGS_ROUTE
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.support.auth import _identity, _StubVerifier
        from tests.support.http import request as http_request

        identity = _identity(
            repository="example/control",
            workflow_ref="example/control/.github/workflows/operator.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )

        def policy(actions: list[str]) -> LaunchplaneAuthzPolicy:
            return LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": identity.repository,
                            "workflow_refs": [identity.workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["example-site"],
                            "contexts": ["example-site"],
                            "actions": actions,
                        }
                    ]
                }
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = FilesystemRecordStore(state_dir=root / "state")
            seed(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=policy(["product_profile.read"]),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            headers = {"Authorization": "Bearer valid-token"}
            denied = await http_request(
                app,
                "POST",
                ODOO_WEBSITE_SETTINGS_ROUTE,
                headers=headers,
                payload=request().model_dump(mode="json"),
            )
            self.assertEqual(denied.status_code, 403)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=policy(["product_config.plan", "product_config.apply"]),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            dry = await http_request(
                app,
                "POST",
                ODOO_WEBSITE_SETTINGS_ROUTE,
                headers=headers,
                payload=request().model_dump(mode="json"),
            )
            self.assertEqual(dry.status_code, 202)
            payload = request().model_dump(mode="json")
            payload.update(mode="apply", reviewed_plan_sha256=dry.json()["result"]["plan_sha256"])
            missing = await http_request(
                app, "POST", ODOO_WEBSITE_SETTINGS_ROUTE, headers=headers, payload=payload
            )
            self.assertEqual(missing.status_code, 400)
            applied = await http_request(
                app,
                "POST",
                ODOO_WEBSITE_SETTINGS_ROUTE,
                headers={**headers, "Idempotency-Key": "website-test"},
                payload=payload,
            )
            replay = await http_request(
                app,
                "POST",
                ODOO_WEBSITE_SETTINGS_ROUTE,
                headers={**headers, "Idempotency-Key": "website-test"},
                payload=payload,
            )
            self.assertEqual(applied.status_code, 202)
            self.assertTrue(applied.json()["result"]["read_back_matches"])
            self.assertTrue(replay.json()["replayed"])
            self.assertFalse(
                build_release_review(store=store, profile=profile(), read=github_read).approved
            )
