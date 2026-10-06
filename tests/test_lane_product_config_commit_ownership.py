import unittest
import json
from dataclasses import asdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from typing import ClassVar

from fastapi import FastAPI

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductProfileConflictError
from tests.support.auth import _identity, _StubVerifier, local_operator_policy
from tests.support.http import request as http_request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import sqlite_database_url
from tests.test_odoo_addon_settings_override import _seed_lane
from tests.test_odoo_addon_settings_override import _request_payload as addon_payload
from tests.test_integration_allowances import _request_payload as allowances_payload
from tests.test_testing_lane_hold import _payload as hold_payload


_record_store: ContextVar[FilesystemRecordStore | PostgresRecordStore] = ContextVar(
    "lane_commit_test_store"
)


class LaneProductConfigCommitOwnershipTests(unittest.IsolatedAsyncioTestCase):
    _apps: ClassVar[dict[tuple[str, str, str, str], FastAPI]]
    _app_root: ClassVar[TemporaryDirectory[str]]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._apps = {}
        cls._app_root = TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._apps.clear()
        cls._app_root.cleanup()
        super().tearDownClass()

    @classmethod
    @contextmanager
    def _app_for_store(
        cls,
        store: FilesystemRecordStore | PostgresRecordStore,
        *,
        caller: str,
        identity: GitHubActionsIdentity,
        policy: LaunchplaneAuthzPolicy,
        config: BearerIdentityConfig,
    ) -> Iterator[FastAPI]:
        # Cache routes and caller policy, never records. Context propagates into
        # the app's worker threads and resets before the next isolated scenario.
        token = _record_store.set(store)
        try:
            key = (
                caller,
                policy.model_dump_json(),
                config.model_dump_json(),
                json.dumps(asdict(identity), sort_keys=True),
            )
            if key not in cls._apps:
                cls._apps[key] = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(identity),
                    authz_policy=policy,
                    record_store_factory=lambda: _record_store.get(),
                    control_plane_root_path=Path(cls._app_root.name),
                    bearer_identity_config=config,
                )
            yield cls._apps[key]
        finally:
            _record_store.reset(token)
            if isinstance(store, PostgresRecordStore):
                store.close()

    async def test_padded_stored_lanes_apply_and_fence_foreign_normalized_claims(self) -> None:
        for route, payload, writer in (
            ("odoo-addon-settings", addon_payload(), "write_odoo_instance_override_record"),
            (
                "integration-allowances",
                allowances_payload(),
                "compare_and_write_dokploy_target_record",
            ),
            ("testing-hold", hold_payload(), "compare_and_write_dokploy_target_record"),
        ):
            for padding in ({"context": " cm\t"}, {"instance": " testing\n"}):
                await self._apply_route(
                    route,
                    payload,
                    writer,
                    caller="github_actions",
                    padding=padding,
                    scenarios=("unchanged", "shared_context", "duplicate_lane", "reassigned"),
                )

    async def test_addon_settings_refuse_context_reassignment_at_commit(self) -> None:
        await self._apply_route(
            "odoo-addon-settings", addon_payload(), "write_odoo_instance_override_record"
        )

    async def test_integration_allowances_refuse_context_reassignment_at_commit(self) -> None:
        await self._apply_route(
            "integration-allowances",
            allowances_payload(),
            "compare_and_write_dokploy_target_record",
        )

    async def test_testing_hold_refuses_context_reassignment_at_commit(self) -> None:
        await self._apply_route(
            "testing-hold", hold_payload(), "compare_and_write_dokploy_target_record"
        )

    async def test_other_callers_addon_settings_keep_named_lane_at_commit(self) -> None:
        for caller in ("github_actions", "local_admin"):
            await self._apply_route(
                "odoo-addon-settings",
                addon_payload(),
                "write_odoo_instance_override_record",
                caller=caller,
            )

    async def test_other_callers_allowances_keep_named_lane_at_commit(self) -> None:
        for caller in ("github_actions", "local_admin"):
            await self._apply_route(
                "integration-allowances",
                allowances_payload(),
                "compare_and_write_dokploy_target_record",
                caller=caller,
            )

    async def test_other_callers_testing_hold_keep_named_lane_at_commit(self) -> None:
        for caller in ("github_actions", "local_admin"):
            await self._apply_route(
                "testing-hold",
                hold_payload(),
                "compare_and_write_dokploy_target_record",
                caller=caller,
            )

    async def _apply_route(
        self,
        route: str,
        payload: dict[str, object],
        writer_name: str,
        *,
        caller: str = "local_operator",
        padding: dict[str, str] | None = None,
        scenarios: tuple[str, ...] = ("unchanged", "reassigned", "shared_context", "lost_instance"),
    ) -> None:
        for store_type in (FilesystemRecordStore, PostgresRecordStore):
            for scenario in scenarios:
                with (
                    self.subTest(
                        route=route,
                        store=store_type.__name__,
                        caller=caller,
                        scenario=scenario,
                        padding=padding,
                    ),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    store = (
                        FilesystemRecordStore(state_dir=root)
                        if store_type is FilesystemRecordStore
                        else PostgresRecordStore(
                            database_url=sqlite_database_url(root / "records.sqlite3")
                        )
                    )
                    if isinstance(store, PostgresRecordStore):
                        store.ensure_schema()
                    profile = LaunchplaneProductProfileRecord.model_validate(
                        _odoo_profile_payload_with_prod_lane()
                    )
                    if padding and scenario != "duplicate_lane":
                        profile = LaunchplaneProductProfileRecord.model_validate(
                            {
                                **profile.model_dump(),
                                "lanes": [
                                    {**lane.model_dump(), **padding}
                                    if lane.instance == "testing"
                                    else lane.model_dump()
                                    for lane in profile.lanes
                                ],
                            }
                        )
                    store.write_product_profile_record(profile)
                    _seed_lane(store)
                    identity = _identity()
                    actions = ("product_config.plan", "product_config.apply")
                    rule: dict[str, object] = {
                        "products": [profile.product],
                        "contexts": ["cm"],
                        "actions": list(actions),
                    }
                    if caller == "github_actions":
                        rule.update(
                            repository=identity.repository,
                            workflow_refs=[identity.workflow_ref],
                            event_names=[identity.event_name],
                        )
                        token = "valid-token"
                        config = BearerIdentityConfig()
                    else:
                        rule.update(
                            subjects=["local-owner-agent"], token_labels=["local-owner-write"]
                        )
                        token = "test-operator-token"
                        config = BearerIdentityConfig.model_validate(
                            {
                                f"{caller}_token": token,
                                f"{caller}_subject": "local-owner-agent",
                                f"{caller}_token_label": "local-owner-write",
                            }
                        )
                    policy = (
                        local_operator_policy(
                            actions=actions, products=(profile.product,), contexts=("cm",)
                        )
                        if caller == "local_operator"
                        else LaunchplaneAuthzPolicy.model_validate(
                            {
                                "github_actions"
                                if caller == "github_actions"
                                else "local_admins": [rule]
                            }
                        )
                    )
                    with self._app_for_store(
                        store, caller=caller, identity=identity, policy=policy, config=config
                    ) as app:
                        headers = {"Authorization": f"Bearer {token}"}
                        path = f"/v1/product-config/{route}/apply"
                        dry_run = await http_request(
                            app, "POST", path, headers=headers, payload=payload
                        )
                        self.assertEqual(dry_run.status_code, 202, dry_run.text)
                        apply_payload = {
                            **payload,
                            "mode": "apply",
                            "reviewed_plan_sha256": dry_run.json()["result"]["plan_sha256"],
                        }
                        before = store.read_dokploy_target_record(
                            context_name="cm", instance_name="testing"
                        )
                        original_writer = getattr(store, writer_name)

                        def write_after_reassignment(*args: object, **kwargs: object) -> object:
                            if scenario in ("reassigned", "lost_instance"):
                                store.write_product_profile_record(
                                    profile.model_copy(
                                        update={
                                            "lanes": ()
                                            if scenario == "reassigned"
                                            else tuple(
                                                lane
                                                for lane in profile.lanes
                                                if lane.instance != "testing"
                                            )
                                        }
                                    )
                                )
                            if scenario in ("reassigned", "shared_context", "duplicate_lane"):
                                store.write_product_profile_record(
                                    profile.model_copy(
                                        update={
                                            "product": "other-product",
                                            "lanes": tuple(
                                                lane.model_copy(update=padding)
                                                if scenario == "duplicate_lane"
                                                and padding
                                                and lane.instance == "testing"
                                                else lane
                                                for lane in profile.lanes
                                                if scenario in ("reassigned", "duplicate_lane")
                                                or lane.instance == "prod"
                                            ),
                                        }
                                    )
                                )
                            return original_writer(*args, **kwargs)

                        with patch.object(
                            store, writer_name, side_effect=write_after_reassignment
                        ) as writer:
                            response = await http_request(
                                app,
                                "POST",
                                path,
                                headers={**headers, "Idempotency-Key": f"ownership-{route}"},
                                payload=apply_payload,
                            )
                        writer.assert_called_once()
                        refused = scenario in ("reassigned", "lost_instance", "duplicate_lane") or (
                            caller == "local_operator" and scenario == "shared_context"
                        )
                        context_refused = caller == "local_operator" and scenario != "lost_instance"
                        self.assertEqual(
                            response.status_code,
                            (403 if context_refused else 409) if refused else 202,
                            response.text,
                        )
                        if refused:
                            self.assertEqual(
                                response.json()["error"]["code"],
                                "local_operator_lane_scope_required"
                                if context_refused
                                else "product_profile_conflict",
                            )
                            self.assertEqual(
                                store.read_dokploy_target_record(
                                    context_name="cm", instance_name="testing"
                                ),
                                before,
                            )
                            self.assertEqual(store.list_odoo_instance_override_records(), ())
                        else:
                            self.assertTrue(response.json()["result"]["applied"])
                            self.assertEqual(
                                store.read_product_profile_record(profile.product), profile
                            )
                        if isinstance(store, PostgresRecordStore):
                            self.assertEqual(store.list_product_reconcile_requests(), ())


class FilesystemLaneWriteLockTests(unittest.TestCase):
    def test_writers_wait_for_lane_reassignment_and_refuse_atomically(self) -> None:
        for writer_kind in ("target", "override"):
            with self.subTest(writer=writer_kind), TemporaryDirectory() as directory:
                root = Path(directory)
                store = FilesystemRecordStore(state_dir=root)
                owner_store = FilesystemRecordStore(state_dir=root)
                profile = LaunchplaneProductProfileRecord.model_validate(
                    _odoo_profile_payload_with_prod_lane()
                )
                store.write_product_profile_record(profile)
                _seed_lane(store)
                target = store.read_dokploy_target_record(
                    context_name="cm", instance_name="testing"
                )
                at_lock = threading.Event()
                original_lock = store._product_authority_bundle_lock

                @contextmanager
                def signal_lock() -> Iterator[None]:
                    at_lock.set()
                    with original_lock():
                        yield

                def write_lane() -> None:
                    requirement = (profile.product, "cm", "testing")
                    if writer_kind == "target":
                        store.compare_and_write_dokploy_target_record(
                            expected_record=target,
                            replacement_record=target.model_copy(
                                update={"source_label": "guarded"}
                            ),
                            required_product_config_target=requirement,
                        )
                    else:
                        from tests.test_odoo_addon_settings_override import _existing_record

                        store.write_odoo_instance_override_record(
                            _existing_record(),
                            required_product_config_target=requirement,
                        )

                with (
                    patch.object(store, "_product_authority_bundle_lock", side_effect=signal_lock),
                    ThreadPoolExecutor(max_workers=1) as workers,
                ):
                    # Model the administrator's profile write while holding the same file lock.
                    with owner_store._product_authority_bundle_lock():
                        future = workers.submit(write_lane)
                        self.assertTrue(at_lock.wait(5))
                        with self.assertRaises(TimeoutError):
                            future.result(timeout=0.1)
                        owner_store._write_model_locked(
                            "launchplane_product_profiles",
                            profile.product,
                            profile.model_copy(update={"lanes": ()}),
                        )
                    with self.assertRaises(ProductProfileConflictError):
                        future.result(timeout=5)
                self.assertEqual(
                    store.read_dokploy_target_record(context_name="cm", instance_name="testing"),
                    target,
                )
                self.assertEqual(store.list_odoo_instance_override_records(), ())
