import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import secrets as control_plane_secrets
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProductProfileConflictError,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _post_product_config_apply
from tests.support.auth import _identity, _StubVerifier
from tests.support.profiles import _generic_site_profile_payload, _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url


class ProductConfigLaneOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_product_cannot_plan_or_write_another_lane(self) -> None:
        for driver in ("generic-web", "odoo"):
            for mode in ("dry-run", "apply"):
                for target in (
                    ("other-site", "testing"),
                    ("own-site", "foreign"),
                    ("other-site", ""),
                    ("", ""),
                ):
                    with self.subTest(driver=driver, mode=mode, target=target):
                        await self._request_config(
                            driver=driver, mode=mode, target=target, refused=True
                        )

    async def test_missing_or_noncanonical_product_is_refused(self) -> None:
        for product in ("unknown", "own-context"):
            with self.subTest(product=product):
                await self._request_config(product=product, refused=True)

    async def test_own_lane_and_context_work_for_both_drivers(self) -> None:
        for driver in ("generic-web", "odoo"):
            for mode in ("dry-run", "apply"):
                for instance in ("testing", ""):
                    with self.subTest(driver=driver, mode=mode, instance=instance):
                        await self._request_config(
                            driver=driver, mode=mode, target=("own-site", instance)
                        )

    async def test_profile_changed_before_commit_refuses_without_config_writes(self) -> None:
        await self._request_config(mode="apply", change_profile=True, refused=True)

    async def test_replay_does_not_bypass_current_ownership(self) -> None:
        await self._request_config(mode="apply", replay_after_change=True)

    async def test_shared_context_refuses_context_wide_effects(self) -> None:
        for mode in ("dry-run", "apply"):
            for historical in (False, True):
                with self.subTest(mode=mode, historical=historical):
                    await self._request_config(
                        mode=mode,
                        target=("own-site", ""),
                        shared_context=True,
                        historical=historical,
                        refused=True,
                    )
                    await self._request_config(
                        mode=mode,
                        context_secret=True,
                        shared_context=True,
                        historical=historical,
                        refused=True,
                    )

    async def test_shared_context_allows_an_owned_instance_scoped_write(self) -> None:
        await self._request_config(mode="apply", shared_context=True)

    async def test_duplicate_lane_ownership_is_refused(self) -> None:
        for mode in ("dry-run", "apply"):
            await self._request_config(mode=mode, shared_lane=True, refused=True)

    async def test_foreign_exact_lane_claim_added_before_commit_refuses(self) -> None:
        await self._request_config(mode="apply", add_foreign_claim_on_commit=True, refused=True)

    async def test_foreign_context_claim_added_before_commit_refuses(self) -> None:
        await self._request_config(
            mode="apply", target=("own-site", ""), add_foreign_claim_on_commit=True, refused=True
        )

    async def _request_config(
        self,
        *,
        driver: str = "generic-web",
        mode: str = "dry-run",
        target: tuple[str, str] = ("own-site", "testing"),
        product: str = "own-site",
        refused: bool = False,
        change_profile: bool = False,
        replay_after_change: bool = False,
        shared_context: bool = False,
        shared_lane: bool = False,
        historical: bool = False,
        context_secret: bool = False,
        add_foreign_claim_on_commit: bool = False,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "test.sqlite3")
            )
            store.ensure_schema()
            profile_payload = (
                _generic_site_profile_payload("own-site")
                if driver == "generic-web"
                else _odoo_preview_profile_payload("own-site")
            )
            profile_payload["lanes"] = [{"context": "own-site", "instance": "testing"}]
            profile = LaunchplaneProductProfileRecord.model_validate(profile_payload)
            store.write_product_profile_record(profile)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    _generic_site_profile_payload("other-site")
                )
            )
            if shared_context or shared_lane:
                other = store.read_product_profile_record("other-site")
                if historical:
                    other = other.model_copy(update={"historical_contexts": ("own-site",)})
                else:
                    other = other.model_copy(
                        update={
                            "lanes": profile.lanes
                            if shared_lane
                            else (profile.lanes[0].model_copy(update={"instance": "prod"}),)
                        }
                    )
                store.write_product_profile_record(other)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": [product],
                            "actions": ["product_config.plan", "product_config.apply"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            payload: dict[str, object] = {
                "mode": mode,
                "product": product,
                "context": target[0],
                "instance": target[1],
                "runtime_env": {"env": {"SITE_MODE": "private-setting"}},
            }
            if refused and not (change_profile or add_foreign_claim_on_commit):
                payload["secrets"] = [{"name": "SMTP_PASSWORD", "value": "private-secret"}]
            if context_secret:
                payload["secrets"] = [
                    {"name": "SMTP_PASSWORD", "value": "private-secret", "scope": "context"}
                ]
            original_write = store.write_product_authority_bundle

            def change_then_write(bundle: ProductAuthorityBundle) -> None:
                if add_foreign_claim_on_commit:
                    other = store.read_product_profile_record("other-site")
                    store.write_product_profile_record(
                        other.model_copy(update={"lanes": profile.lanes})
                    )
                else:
                    store.write_product_profile_record(profile.model_copy(update={"lanes": ()}))
                original_write(bundle)

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                with patch.object(
                    store,
                    "write_product_authority_bundle",
                    side_effect=change_then_write
                    if change_profile or add_foreign_claim_on_commit
                    else original_write,
                ) as writer:
                    response = await _post_product_config_apply(
                        app, payload, idempotency_key="ownership-test"
                    )
                    if refused:
                        self.assertEqual(
                            response.status_code,
                            409 if change_profile or add_foreign_claim_on_commit else 403,
                            response.text,
                        )
                        self.assertEqual(
                            response.json()["error"]["code"],
                            "product_profile_conflict"
                            if change_profile or add_foreign_claim_on_commit
                            else "product_config_lane_not_owned",
                        )
                        self.assertEqual(store.list_runtime_environment_records(), ())
                        self.assertEqual(store.list_secret_records(), ())
                        self.assertNotIn("private-secret", response.text)
                        self.assertNotIn("private-setting", response.text)
                        if not (change_profile or add_foreign_claim_on_commit):
                            writer.assert_not_called()
                    else:
                        self.assertEqual(response.status_code, 202, response.text)
                        if mode == "apply":
                            self.assertEqual(
                                store.list_runtime_environment_records()[0].env,
                                {"SITE_MODE": "private-setting"},
                            )
                        else:
                            self.assertEqual(store.list_runtime_environment_records(), ())
                    if replay_after_change:
                        store.write_product_profile_record(profile.model_copy(update={"lanes": ()}))
                        replay = await _post_product_config_apply(
                            app, payload, idempotency_key="ownership-test"
                        )
                        self.assertEqual(replay.status_code, 403, replay.text)
                        self.assertEqual(
                            replay.json()["error"]["code"], "product_config_lane_not_owned"
                        )
            store.close()


class ProductConfigContextCommitTests(unittest.TestCase):
    def test_both_stores_refuse_a_new_foreign_context_owner_before_publish(self) -> None:
        for store_type, instance in (
            (FilesystemRecordStore, ""),
            (PostgresRecordStore, ""),
            (FilesystemRecordStore, "testing"),
            (PostgresRecordStore, "testing"),
        ):
            with (
                self.subTest(store=store_type.__name__, instance=instance),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                store = (
                    FilesystemRecordStore(state_dir=root)
                    if store_type is FilesystemRecordStore
                    else PostgresRecordStore(
                        database_url=_sqlite_database_url(root / "test.sqlite3")
                    )
                )
                if isinstance(store, PostgresRecordStore):
                    store.ensure_schema()
                profile = LaunchplaneProductProfileRecord.model_validate(
                    _generic_site_profile_payload("own-site")
                )
                store.write_product_profile_record(profile)
                bundle = ProductAuthorityBundle(
                    required_product_config_target=("own-site", "own-site", instance),
                    runtime_environments=(
                        RuntimeEnvironmentRecord(
                            scope="instance" if instance else "context",
                            context="own-site",
                            instance=instance,
                            env={"SITE_MODE": "private-setting"},
                            updated_at="2026-10-03T00:00:00Z",
                            source_label="test",
                        ),
                    ),
                )
                store.write_product_profile_record(
                    profile.model_copy(update={"product": "other-site"})
                )
                with self.assertRaises(ProductProfileConflictError):
                    store.write_product_authority_bundle(bundle)
                self.assertEqual(store.list_runtime_environment_records(), ())
                if isinstance(store, PostgresRecordStore):
                    store.close()

    def test_copy_context_guard_and_lane_guard_both_run_before_publish(self) -> None:
        for store_type in (FilesystemRecordStore, PostgresRecordStore):
            with self.subTest(store=store_type.__name__), TemporaryDirectory() as directory:
                root = Path(directory)
                store = (
                    FilesystemRecordStore(state_dir=root)
                    if store_type is FilesystemRecordStore
                    else PostgresRecordStore(
                        database_url=_sqlite_database_url(root / "test.sqlite3")
                    )
                )
                if isinstance(store, PostgresRecordStore):
                    store.ensure_schema()
                profile = LaunchplaneProductProfileRecord.model_validate(
                    _generic_site_profile_payload("own-site")
                )
                store.write_product_profile_record(profile)
                bundle = ProductAuthorityBundle(
                    required_context_owners=(("own-site", "own-site"),),
                    required_product_config_target=("own-site", "own-site", "testing"),
                    runtime_environments=(
                        RuntimeEnvironmentRecord(
                            scope="instance",
                            context="own-site",
                            instance="testing",
                            env={"SITE_MODE": "private-setting"},
                            updated_at="2026-10-03T00:00:00Z",
                            source_label="test",
                        ),
                    ),
                )
                store.write_product_authority_bundle(bundle)
                # The context remains owned, but the destination lane is removed.
                store.write_product_profile_record(
                    profile.model_copy(
                        update={
                            "lanes": tuple(
                                lane for lane in profile.lanes if lane.instance != "testing"
                            )
                        }
                    )
                )
                with self.assertRaises(ProductProfileConflictError):
                    store.write_product_authority_bundle(bundle)
                if isinstance(store, PostgresRecordStore):
                    store.close()
