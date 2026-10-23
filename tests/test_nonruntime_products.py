from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from itertools import chain, count, repeat
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import DEFAULT, Mock, patch

import click
from fastapi import FastAPI
from httpx2 import Response
from pydantic import ValidationError

from control_plane.artifact_retention import product_retention_dry_run
from control_plane.artifact_scan import qualify_product_artifact
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    NonruntimeClientUnsupportedError,
    ProductClassificationChangeError,
    ProductOwnerProfile,
)
from control_plane.contracts.product_environment_read_model import build_product_site_overview
from control_plane.contracts.product_reconcile import ProductReconcileRequestRecord
from control_plane.contracts.repo_product_mapping_read_model import (
    build_repo_product_mapping_from_records,
)
from control_plane.generic_web_onboarding import (
    GenericWebOnboardingIntent,
    build_generic_web_onboarding_manifest,
)
from control_plane.product_repository_identity import current_tracked_inventory_record
from control_plane.github_app_webhook import _product_for_repository
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_reconcile import (
    ProductReconcileError,
    reconcile_product_request,
    request_product_reconcile_sweep,
    resolve_build_provenance_transport,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProductProfileConflictError,
    ProductRepositoryOwnershipError,
    require_product_profile_writes,
)
from control_plane.testing_activation import read_product_compose
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    resolve_generic_web_profile_lane,
)
from control_plane.product_promotion_http import resolve_product_promotion_target
from control_plane.product_owner_setting import (
    ProductOwnerIdentity,
    updated_product_owner_profile,
)
from control_plane.release_invitation import publish_release_invitation
from control_plane.workflows.product_onboarding import plan_product_onboarding_authority_bundle
from control_plane.workflows.product_onboarding import product_onboarding_manifest_plan_sha256
from control_plane.workflows.public_ingress_monitor import discover_public_ingress_monitor_targets
from tests.http_app_test_support import _asgi_request, _product_config_policy
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _RejectingVerifier,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from tests.support.auth import StubVerifier, identity
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url
from tests.test_github_app_webhook import _inventory
from tests.test_authz_administration_read import _database_app

NOW = "2026-10-20T20:00:00Z"
ROUTE = "/v1/product-onboarding/apply"


def inert_profile(product: str = "desktop-tool") -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        classification="active_awareness",
        product=product,
        display_name="Desktop tool",
        repository="example/desktop-tool",
        owner=ProductOwnerProfile(github_login="director", github_id="1234"),
        release_on_acceptance="director_standing",
        updated_at=NOW,
        source="test",
    )


def inert_manifest() -> dict[str, object]:
    return {
        "classification": "active_awareness",
        "product": "desktop-tool",
        "display_name": "Desktop tool",
        "repository": "Example/Desktop-Tool",
        "owner": {"github_login": "director", "github_id": "1234"},
        "release_on_acceptance": "director_standing",
    }


def assert_registration_race(case: unittest.TestCase, store: PostgresRecordStore) -> None:
    for same_product in (True, False):
        with case.subTest(same_product=same_product):
            suffix = "key" if same_product else "repository"
            first = inert_profile(f"first-{suffix}").model_copy(
                update={"repository": f"example/{suffix}"}
            )
            second = first.model_copy(
                update={
                    "product": first.product if same_product else f"second-{suffix}",
                    "display_name": "Competing registration",
                }
            )
            store.write_repository_inventory_record(
                _inventory(repository_id=100 if same_product else 101, repository=first.repository)
            )
            barrier = threading.Barrier(2)

            def register(profile: LaunchplaneProductProfileRecord) -> str:
                barrier.wait(timeout=10)
                try:
                    store.write_product_authority_bundle(
                        ProductAuthorityBundle(
                            product_profiles=(profile,),
                            absent_product_ids=(profile.product,),
                        )
                    )
                except (ProductProfileConflictError, ProductRepositoryOwnershipError):
                    return "conflict"
                return "created"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(register, (first, second)))
            case.assertCountEqual(outcomes, ("created", "conflict"))
            rows = tuple(
                p for p in store.list_product_profile_records() if p.repository == first.repository
            )
            case.assertEqual(len(rows), 1)
            case.assertIn(rows[0], (first, second))

    runtime = LaunchplaneProductProfileRecord.model_validate(
        {
            **product_profile_payload("alias-runtime"),
            "repository": "example/old-alias",
            "repository_id": "202",
            "repository_owner_id": "7",
            "preview": {},
            "lifecycle_state": "retired",
        }
    )
    inert = LaunchplaneProductProfileRecord.model_validate(
        {
            **inert_profile("alias-inert").model_dump(mode="json"),
            "repository": "example/current-alias",
            "lifecycle_state": "retired",
        }
    )
    store.write_repository_inventory_record(
        _inventory(repository_id=202, repository=runtime.repository)
    )
    store.write_repository_inventory_record(
        _inventory(repository_id=202, repository=inert.repository, inventory_revision=2)
    )
    store.write_product_profile_record(runtime)
    store.write_product_profile_record(inert)
    barrier = threading.Barrier(2)

    def activate(profile: LaunchplaneProductProfileRecord) -> str:
        barrier.wait(timeout=10)
        active = LaunchplaneProductProfileRecord.model_validate(
            {**profile.model_dump(mode="json"), "lifecycle_state": "active"}
        )
        try:
            store.compare_and_write_product_profile_record(
                expected_record=profile, replacement_record=active
            )
        except ProductRepositoryOwnershipError:
            return "conflict"
        return "activated"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(activate, (runtime, inert)))
    case.assertCountEqual(outcomes, ("activated", "conflict"))
    case.assertEqual(
        sum(
            store.read_product_profile_record(profile.product).is_active
            for profile in (runtime, inert)
        ),
        1,
    )


class NonruntimeProductTests(unittest.TestCase):
    def test_nonruntime_registration_requires_explicit_standing_client(self) -> None:
        invalid_inputs: tuple[dict[str, object], ...] = (
            {"owner": {}},
            {"release_on_acceptance": "held"},
            {
                "owner": {"github_login": "other-client", "github_id": "5678"},
                "release_on_acceptance": "held",
            },
        )
        for changes in invalid_inputs:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ProductOnboardingManifest.model_validate({**inert_manifest(), **changes})
        manifest = ProductOnboardingManifest.model_validate(
            {
                **inert_manifest(),
                "owner": {"github_login": "director", "github_id": "1234"},
                "release_on_acceptance": "director_standing",
            }
        )
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            result, _ = plan_product_onboarding_authority_bundle(
                record_store=store, manifest=manifest
            )
        self.assertEqual(result.product_profile.owner, manifest.owner)
        self.assertEqual(
            result.product_profile.release_on_acceptance, manifest.release_on_acceptance
        )

    def test_active_nonruntime_client_and_acceptance_cannot_be_changed_to_held(self) -> None:
        from control_plane.product_owner_setting import (
            ProductOwnerApplyRequest,
            ProductOwnerIdentity,
            build_product_owner_setting_plan,
        )
        from control_plane.product_production_use_setting import (
            ProductProductionUseApplyRequest,
            build_product_production_use_plan,
        )

        profile = inert_profile()
        for owner in (
            ProductOwnerIdentity(),
            ProductOwnerIdentity(github_login="other-client", github_id="5678"),
        ):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                build_product_owner_setting_plan(
                    profile=profile,
                    request=ProductOwnerApplyRequest(
                        github_login=owner.github_login,
                        clear=not owner.github_id,
                        reason="Change Client",
                    ),
                    resolved_owner=owner,
                )
        with self.assertRaises(ValueError):
            build_product_production_use_plan(
                profile=profile,
                request=ProductProductionUseApplyRequest(
                    production_use="unknown", release_on_acceptance="held", reason="Hold release"
                ),
            )
        with self.assertRaises(ValidationError):
            LaunchplaneProductProfileRecord.model_validate(
                {**profile.model_dump(mode="json"), "release_on_acceptance": "held"}
            )

    def test_explicit_classification_preserves_legacy_serialization(self) -> None:
        runtime = LaunchplaneProductProfileRecord.model_validate(product_profile_payload("site"))
        self.assertNotIn("classification", runtime.model_dump(mode="json"))
        explicit = LaunchplaneProductProfileRecord.model_validate(
            {
                **runtime.model_dump(mode="json"),
                "classification": "managed_runtime",
            }
        )
        self.assertEqual(explicit.model_dump(mode="json"), runtime.model_dump(mode="json"))
        spaced_payload = {**runtime.model_dump(mode="json"), "driver_id": f" {runtime.driver_id} "}
        spaced = LaunchplaneProductProfileRecord.model_validate(spaced_payload)
        self.assertEqual(spaced.model_dump(mode="json"), spaced_payload)
        with self.assertRaises(ValidationError):
            LaunchplaneProductProfileRecord.model_validate(
                {
                    "product": "tool",
                    "display_name": "Tool",
                    "repository": "example/tool",
                    "updated_at": NOW,
                    "source": "test",
                }
            )
        profile = inert_profile()
        self.assertTrue(profile.is_active)
        self.assertFalse(profile.is_managed_runtime)
        self.assertEqual(
            LaunchplaneProductProfileRecord.model_validate_json(profile.model_dump_json()), profile
        )

    def test_nonruntime_shape_cannot_smuggle_runtime_authority(self) -> None:
        payload = inert_profile().model_dump(mode="json")
        for extra in (
            {"driver_id": "generic-web"},
            {"image": {"repository": "ghcr.io/example/tool"}},
            {"image": {"scan_enabled": True}},
            {"lanes": [{"context": "tool", "instance": "testing"}]},
            {"historical_contexts": ["old-tool"]},
            {"preview": {"enabled": False, "context": "tool"}},
            {"runtime_port": 8080, "health_path": "/health"},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                LaunchplaneProductProfileRecord.model_validate({**payload, **extra})
        with self.assertRaises(ValidationError):
            ProductOnboardingManifest.model_validate(
                {
                    **inert_manifest(),
                    "provider_targets": [{"context": "tool", "instance": "testing"}],
                }
            )

    def test_mapping_keeps_real_identity_and_refuses_ambiguous_repository(self) -> None:
        profile = inert_profile()
        mapping = build_repo_product_mapping_from_records(
            generated_at=NOW, product_profiles=(profile,)
        )
        entry = mapping.repositories[0]
        self.assertNotIn("driver_id", entry.model_dump(mode="json"))
        self.assertEqual(
            (entry.product, entry.classification), (profile.product, profile.classification)
        )
        self.assertEqual(
            (entry.contexts, entry.environments, entry.driver_id, entry.preview_context),
            ((), (), "", ""),
        )
        runtime = LaunchplaneProductProfileRecord.model_validate(
            {
                **product_profile_payload("site"),
                "repository": profile.repository.upper(),
            }
        )
        runtime = LaunchplaneProductProfileRecord.model_validate(
            {
                **runtime.model_dump(mode="json"),
                "repository_id": "42",
                "repository_owner_id": "7",
            }
        )
        alias = LaunchplaneProductProfileRecord.model_validate(
            {
                **runtime.model_dump(mode="json"),
                "product": "alias",
                "repository": "example/renamed",
            }
        )
        requests = tuple(
            EveryCodeWorkRequestRecord(
                request_id=f"request-{number}",
                source="manual",
                state="queued",
                repository=repository,
                issue_number=number,
                issue_url=f"https://github.com/{repository}/issues/{number}",
                trigger_label="agent-work",
                queued_at=NOW,
                updated_at=NOW,
            )
            for number, repository in enumerate(
                (profile.repository, alias.repository, "example/unrelated"), start=1
            )
        )
        for profiles in ((runtime, profile, alias), (alias, profile, runtime)):
            with self.subTest(order=profiles[0].product):
                safe = inert_profile("safe").model_copy(update={"repository": "example/safe"})
                mapping = build_repo_product_mapping_from_records(
                    generated_at=NOW, product_profiles=(*profiles, safe), work_requests=requests
                )
                self.assertEqual(
                    tuple(entry.repository for entry in mapping.repositories),
                    (safe.repository, requests[-1].repository),
                )
                self.assertEqual(
                    mapping.ambiguous_repositories,
                    tuple(sorted((profile.repository, alias.repository))),
                )

    def test_store_absence_repository_and_classification_guards(self) -> None:
        with TemporaryDirectory() as directory:
            database = PostgresRecordStore(
                database_url=sqlite_database_url(Path(directory) / "db.sqlite")
            )
            self.addCleanup(database.close)
            database.ensure_schema()
            for store in (database, FilesystemRecordStore(Path(directory) / "files")):
                with self.subTest(store=type(store).__name__):
                    profile = inert_profile()
                    bundle = ProductAuthorityBundle(
                        product_profiles=(profile,), absent_product_ids=(profile.product,)
                    )
                    store.write_product_authority_bundle(bundle)
                    invalid = profile.model_copy(update={"runtime_port": 8080})
                    with self.assertRaises(ValidationError):
                        store.compare_and_write_product_profile_record(
                            expected_record=profile, replacement_record=invalid
                        )
                    with self.assertRaises(ProductProfileConflictError):
                        store.write_product_authority_bundle(bundle)
                    collision = profile.model_copy(
                        update={"product": "other", "repository": profile.repository.upper()}
                    )
                    with self.assertRaises(ProductRepositoryOwnershipError):
                        store.write_product_profile_record(collision)
                    runtime = LaunchplaneProductProfileRecord.model_validate(
                        {
                            **product_profile_payload(profile.product),
                            "repository": profile.repository,
                        }
                    )
                    with self.assertRaises(ProductClassificationChangeError):
                        store.write_product_profile_record(runtime)
                    self.assertEqual(store.read_product_profile_record(profile.product), profile)
                    self.assertEqual(len(store.list_product_profile_records()), 1)
                    other = profile.model_copy(
                        update={"product": "other", "repository": "example/other"}
                    )
                    store.write_repository_inventory_record(
                        _inventory(repository_id=42, repository=profile.repository)
                    )
                    store.write_repository_inventory_record(
                        _inventory(repository_id=43, repository=other.repository)
                    )
                    store.write_product_profile_record(other)
                    with self.assertRaises(ProductRepositoryOwnershipError):
                        store.compare_and_write_product_profile_record(
                            expected_record=other,
                            replacement_record=other.model_copy(
                                update={"repository": profile.repository}
                            ),
                        )
                    self.assertEqual(store.read_product_profile_record(other.product), other)

    def test_renamed_repository_collision_rechecks_each_profile_writer(self) -> None:
        runtime = LaunchplaneProductProfileRecord.model_validate(
            {
                **product_profile_payload("runtime"),
                "repository": "example/old-name",
                "repository_id": "42",
                "repository_owner_id": "7",
                "preview": {},
            }
        )
        inert = LaunchplaneProductProfileRecord.model_validate(
            {**inert_profile().model_dump(mode="json"), "repository": "example/current-name"}
        )
        for filesystem in (False, True):
            for incoming_runtime in (False, True):
                for writer in ("direct", "bundle", "compare"):
                    with (
                        self.subTest(
                            filesystem=filesystem, incoming_runtime=incoming_runtime, writer=writer
                        ),
                        TemporaryDirectory() as directory,
                        nullcontext(FilesystemRecordStore(Path(directory) / "files"))
                        if filesystem
                        else closing(
                            PostgresRecordStore(
                                database_url=sqlite_database_url(Path(directory) / "db.sqlite")
                            )
                        ) as store,
                    ):
                        if isinstance(store, PostgresRecordStore):
                            store.ensure_schema()
                        store.write_repository_inventory_record(
                            _inventory(repository_id=42, repository=runtime.repository)
                        )
                        store.write_repository_inventory_record(
                            _inventory(
                                repository_id=42, repository=inert.repository, inventory_revision=2
                            )
                        )
                        incoming, existing = (
                            (runtime, inert) if incoming_runtime else (inert, runtime)
                        )
                        store.write_product_profile_record(existing)
                        retired = LaunchplaneProductProfileRecord.model_validate(
                            {**incoming.model_dump(mode="json"), "lifecycle_state": "retired"}
                        )
                        if writer == "compare":
                            store.write_product_profile_record(retired)
                        before = store.list_product_profile_records()
                        with self.assertRaises(ProductRepositoryOwnershipError):
                            if writer == "direct":
                                store.write_product_profile_record(incoming)
                            elif writer == "bundle":
                                store.write_product_authority_bundle(
                                    ProductAuthorityBundle(
                                        product_profiles=(incoming,),
                                        absent_product_ids=(incoming.product,),
                                    )
                                )
                            else:
                                store.compare_and_write_product_profile_record(
                                    expected_record=retired, replacement_record=incoming
                                )
                        self.assertEqual(store.list_product_profile_records(), before)

    def test_repository_comparison_refuses_only_known_latest_inventory_collisions(self) -> None:
        runtime = LaunchplaneProductProfileRecord.model_validate(
            {
                **product_profile_payload("private-runtime"),
                "repository": "example/private-runtime",
                "preview": {},
            }
        )
        inert = inert_profile()
        own = _inventory(repository_id=42, repository=inert.repository)
        other = _inventory(repository_id=43, repository=runtime.repository)
        for inventory in (
            (own,),
            (own, other, _inventory(repository_id=44, repository=runtime.repository)),
        ):
            with self.subTest(inventory=inventory):
                require_product_profile_writes((inert,), (runtime,), inventory_records=inventory)
        copied_runtime = LaunchplaneProductProfileRecord.model_validate(
            {**runtime.model_dump(mode="json"), "repository_id": "43", "repository_owner_id": "7"}
        )
        with self.assertRaises(ProductRepositoryOwnershipError):
            require_product_profile_writes(
                (inert,),
                (copied_runtime,),
                inventory_records=(own, _inventory(repository_id=43, repository=inert.repository)),
            )
        for state in ("tracked", "retired"):
            current = _inventory(
                repository_id=43, repository=runtime.repository, inventory_state=state
            )
            require_product_profile_writes((inert,), (runtime,), inventory_records=(own, current))
        unchanged_ownership = LaunchplaneProductProfileRecord.model_validate(
            {**runtime.model_dump(mode="json"), "display_name": "Unrelated settings change"}
        )
        require_product_profile_writes(
            (unchanged_ownership,),
            (runtime, inert),
            inventory_records=iter(
                Mock(side_effect=AssertionError("Unrelated settings must not consume inventory")),
                None,
            ),
        )
        renamed = LaunchplaneProductProfileRecord.model_validate(
            {
                **runtime.model_dump(mode="json"),
                "repository_id": own.repository_id,
                "repository_owner_id": own.repository_owner_id,
            }
        )
        latest = _inventory(
            repository_id=42,
            repository=inert.repository,
            inventory_state="retired",
            inventory_revision=2,
        )
        with self.assertRaises(ProductRepositoryOwnershipError):
            require_product_profile_writes((inert,), (renamed,), inventory_records=(own, latest))
        renamed_inventory = _inventory(
            repository_id=42, repository="example/no-longer-tool", inventory_revision=2
        )
        with self.assertRaises(ProductRepositoryOwnershipError):
            require_product_profile_writes(
                (inert,), (renamed,), inventory_records=(own, renamed_inventory)
            )
        for state in ("tracked", "retired"):
            reused_name = _inventory(
                repository_id=43, repository=inert.repository, inventory_state=state
            )
            require_product_profile_writes(
                (inert,), (renamed,), inventory_records=(own, renamed_inventory, reused_name)
            )

    def test_runtime_onboarding_does_not_require_unrelated_inventory(self) -> None:
        for with_inert in (False, True):
            with (
                self.subTest(with_inert=with_inert),
                TemporaryDirectory() as directory,
                closing(
                    PostgresRecordStore(
                        database_url=sqlite_database_url(Path(directory) / "db.sqlite")
                    )
                ) as store,
            ):
                store.ensure_schema()
                if with_inert:
                    inert = inert_profile()
                    store.write_repository_inventory_record(
                        _inventory(repository_id=43, repository=inert.repository)
                    )
                    store.write_product_profile_record(inert)
                manifest = ProductOnboardingManifest.model_validate(
                    {
                        "product": "new-runtime",
                        "display_name": "New Runtime",
                        "repository": "example/new-runtime",
                        "driver_id": "generic-web",
                        "image_repository": "ghcr.io/example/new-runtime",
                        "runtime_port": 3000,
                        "health_path": "/health",
                        "lanes": [{"instance": "testing", "context": "new-runtime-testing"}],
                    }
                )
                inventory_before = store.list_repository_inventory_records()
                result, bundle = plan_product_onboarding_authority_bundle(
                    record_store=store, manifest=manifest
                )
                store.write_product_authority_bundle(bundle)
                self.assertEqual(
                    store.read_product_profile_record(manifest.product), result.product_profile
                )
                self.assertEqual(store.list_repository_inventory_records(), inventory_before)

    def test_sqlite_registration_transactions_compete_without_overwrite(self) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            assert_registration_race(self, store)

    def test_runtime_admissions_stop_before_transport_or_provider(self) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            profile = inert_profile()
            store.write_product_profile_record(profile)
            store.write_repository_inventory_record(_inventory(repository=profile.repository))
            self.assertFalse(_product_for_repository(store=store, repository_id="424242")[0])
            transport = Mock(side_effect=AssertionError("Runtime transport must not be obtained"))
            for kind in ("testing", "preview"):
                request = ProductReconcileRequestRecord(
                    target_key=f"{profile.product}:{kind}" + (":7" if kind == "preview" else ""),
                    product=profile.product,
                    target_kind=kind,
                    pull_request_number=7 if kind == "preview" else None,
                    state="pending",
                    requested_at=NOW,
                    updated_at=NOW,
                    request_count=1,
                )
                outcome = reconcile_product_request(
                    record_store=store, request=request, transport_factory=transport
                )
                self.assertEqual(outcome.plan["action"], "none")
            self.assertEqual(request_product_reconcile_sweep(store, NOW, transport), ())
            transport.assert_not_called()
            with patch(
                "control_plane.product_reconcile._merge_train_app_identity",
                side_effect=AssertionError("No token lookup"),
            ):
                with self.assertRaises(ProductReconcileError):
                    resolve_build_provenance_transport(store, profile)
                with self.assertRaises(ValueError):
                    read_product_compose(store, profile.product, "a" * 40, "compose.yml")
                with self.assertRaises(ValueError):
                    qualify_product_artifact(store, product=profile.product)
                with self.assertRaises(ValueError):
                    product_retention_dry_run(store, product=profile.product)
            with self.assertRaises(click.ClickException):
                resolve_generic_web_profile_lane(
                    record_store=store,
                    request=GenericWebDeployRequest(
                        product=profile.product,
                        instance="testing",
                        artifact_id="sha256:" + "a" * 64,
                        source_git_ref="a" * 40,
                    ),
                )
            with self.assertRaises(FileNotFoundError):
                resolve_product_promotion_target(
                    record_store=store, product=profile.product, destination_environment="prod"
                )
            overview = build_product_site_overview(
                record_store=store, product=profile.product, action_allowed=lambda *_: True
            )
            self.assertEqual(overview.environments, ())
            self.assertFalse(overview.preview.enabled)
            self.assertEqual(overview.warnings, ())
            self.assertFalse(any(action.enabled for action in overview.available_actions))
            self.assertEqual(discover_public_ingress_monitor_targets(store), ())
            with (
                patch(
                    "control_plane.release_invitation.launchplane_public_origin_from_env",
                    return_value="https://launchplane.example.invalid",
                ),
                patch(
                    "control_plane.release_invitation.release_version",
                    side_effect=AssertionError("An inert product has no runtime release"),
                ),
            ):
                self.assertFalse(
                    publish_release_invitation(
                        store=store,
                        control_plane_root=Path(directory),
                        profile=profile.model_copy(
                            update={
                                "owner": ProductOwnerProfile(
                                    github_login="example-client", github_id="1234"
                                )
                            }
                        ),
                    )
                )


class NonruntimeProductHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for name, value in (
            ("resolve_launchplane_github_token", "fixture-token"),
            ("github_api_request", {"type": "User", "login": "director", "id": 1234}),
        ):
            patcher = patch(f"control_plane.http_app.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _registration_app(
        self, store: PostgresRecordStore
    ) -> tuple[FastAPI, HumanSessionManager, LaunchplaneHumanSession]:
        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        session = sessions.issue(_github_human_identity())
        rule = _github_human_product_config_policy(
            actions=(
                "product_onboarding.apply",
                "product_profile.read",
                "product_environment.read",
                "product_profile.write",
            ),
            product="launchplane",
            context="launchplane",
        ).model_dump(mode="json")["github_humans"][0]
        machine = _product_config_policy(
            action="product_profile.write", product="launchplane", context="launchplane"
        ).model_dump(mode="json")["github_actions"][0]
        products = ["launchplane", "desktop-tool", "runtime"]
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [{**rule, "products": products}],
                    "github_actions": [{**machine, "products": products}],
                }
            ),
            record_store_factory=lambda: store,
            human_session_manager=sessions,
        )
        return app, sessions, session

    async def test_registration_qualifies_current_inventory_before_plan_and_commit(self) -> None:
        for state, supplied_ids, distinct, late_reactivation in (
            ("active", False, False, False),
            ("active", True, False, False),
            ("retired", False, False, False),
            ("retired", False, False, True),
            ("active", False, True, False),
        ):
            with (
                self.subTest(state=state, supplied_ids=supplied_ids, distinct=distinct),
                TemporaryDirectory() as directory,
                closing(
                    PostgresRecordStore(
                        database_url=sqlite_database_url(Path(directory) / "db.sqlite")
                    )
                ) as store,
            ):
                store.ensure_schema()
                runtime = LaunchplaneProductProfileRecord.model_validate(
                    {
                        **product_profile_payload("runtime"),
                        "repository": "example/old-name",
                        "repository_id": "42",
                        "repository_owner_id": "7",
                        "preview": {},
                        "lifecycle_state": state,
                    }
                )
                store.write_product_profile_record(runtime)
                store.write_repository_inventory_record(
                    _inventory(repository_id=42, repository=runtime.repository)
                )
                store.write_repository_inventory_record(
                    _inventory(
                        repository_id=42, repository="example/current-name", inventory_revision=2
                    )
                )
                repository = "example/distinct" if distinct else "example/current-name"
                if distinct:
                    store.write_repository_inventory_record(
                        _inventory(repository_id=43, repository=repository)
                    )
                manifest = {
                    **inert_manifest(),
                    "repository": repository,
                    "owner": {"github_login": "example-operator", "github_id": "123"},
                }
                if supplied_ids:
                    manifest.update(repository_id="42", repository_owner_id="7")
                app, sessions, session = self._registration_app(store)
                with patch(
                    "control_plane.http_app.github_api_request",
                    return_value={"type": "User", "login": "example-operator", "id": 123},
                ):
                    payload: dict[str, object] = {
                        "product": "launchplane",
                        "mode": "dry_run",
                        "manifest": manifest,
                    }
                    planned = await _asgi_request(
                        app,
                        "POST",
                        ROUTE,
                        headers=_browser_mutation_headers(sessions, session),
                        payload=payload,
                    )
                    if state == "active" and not distinct:
                        self.assertEqual(planned.status_code, 409, planned.text)
                        self.assertEqual(
                            planned.json()["error"]["code"], ProductRepositoryOwnershipError.code
                        )
                    else:
                        self.assertEqual(planned.status_code, 202, planned.text)
                        if late_reactivation:
                            store.write_product_profile_record(
                                LaunchplaneProductProfileRecord.model_validate(
                                    {**runtime.model_dump(mode="json"), "lifecycle_state": "active"}
                                )
                            )
                        applied = await _asgi_request(
                            app,
                            "POST",
                            ROUTE,
                            headers={
                                **_browser_mutation_headers(sessions, session),
                                "Idempotency-Key": "inventory-registration",
                            },
                            payload={
                                **payload,
                                "mode": "apply",
                                "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"],
                            },
                        )
                        self.assertEqual(
                            applied.status_code, 409 if late_reactivation else 202, applied.text
                        )
                profiles = store.list_product_profile_records()
                self.assertEqual(
                    {p.product for p in profiles},
                    {"runtime", "desktop-tool"}
                    if distinct or (state == "retired" and not late_reactivation)
                    else {"runtime"},
                )

    async def test_reactivation_refuses_alias_after_supported_retired_identity_clear(self) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            runtime = LaunchplaneProductProfileRecord.model_validate(
                {
                    **product_profile_payload("runtime"),
                    "repository": "example/old-name",
                    "repository_id": "42",
                    "repository_owner_id": "7",
                    "preview": {},
                    "lifecycle_state": "retired",
                }
            )
            store.write_product_profile_record(runtime)
            store.write_repository_inventory_record(
                _inventory(repository_id=42, repository=runtime.repository)
            )
            store.write_repository_inventory_record(
                _inventory(
                    repository_id=42, repository="example/current-name", inventory_revision=2
                )
            )
            app, sessions, session = self._registration_app(store)

            request_keys = count()

            async def post(
                path: str,
                payload: dict[str, object],
                *,
                human: bool = False,
                expected_status: int = 202,
            ) -> Response:
                headers = (
                    _browser_mutation_headers(sessions, session)
                    if human
                    else {"Authorization": "Bearer valid-token"}
                )
                response = await _asgi_request(
                    app,
                    "POST",
                    path,
                    headers={
                        **headers,
                        "Idempotency-Key": str(next(request_keys)),
                    },
                    payload=payload,
                )
                self.assertEqual(response.status_code, expected_status, response.text)
                return response

            async def acceptance(mode: str) -> None:
                current = store.read_product_profile_record("desktop-tool")
                payload: dict[str, object] = {
                    "mode": "dry-run",
                    "production_use": current.production_use,
                    "release_on_acceptance": mode,
                    "reason": "Reviewed lifecycle continuation",
                }
                planned = await post(
                    "/v1/product-profiles/desktop-tool/production-use", payload, human=True
                )
                await post(
                    "/v1/product-profiles/desktop-tool/production-use",
                    {
                        **payload,
                        "mode": "apply",
                        "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"],
                    },
                    human=True,
                )

            with patch(
                "control_plane.http_app.github_api_request",
                return_value={"type": "User", "login": "example-operator", "id": 123},
            ):
                payload: dict[str, object] = {
                    "product": "launchplane",
                    "mode": "dry_run",
                    "manifest": {
                        **inert_manifest(),
                        "repository": "example/current-name",
                        "repository_id": "42",
                        "repository_owner_id": "7",
                        "owner": {"github_login": "example-operator", "github_id": "123"},
                    },
                }
                planned = await post(ROUTE, payload, human=True)
                await post(
                    ROUTE,
                    {
                        **payload,
                        "mode": "apply",
                        "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"],
                    },
                    human=True,
                )
                current = store.read_product_profile_record("desktop-tool")
                await post(
                    "/v1/product-profiles",
                    {**current.model_dump(mode="json"), "lifecycle_state": "retired"},
                )
                await acceptance("held")
                current = store.read_product_profile_record("desktop-tool")
                await post(
                    "/v1/product-profiles",
                    {
                        **current.model_dump(mode="json"),
                        "repository_id": "",
                        "repository_owner_id": "",
                    },
                )
                await post(
                    "/v1/product-profiles",
                    {**runtime.model_dump(mode="json"), "lifecycle_state": "active"},
                )
                await acceptance("director_standing")
                current = store.read_product_profile_record("desktop-tool")
                refused = await post(
                    "/v1/product-profiles",
                    {**current.model_dump(mode="json"), "lifecycle_state": "active"},
                    expected_status=409,
                )
                self.assertEqual(
                    refused.json()["error"]["code"], ProductRepositoryOwnershipError.code
                )
                self.assertEqual(store.read_product_profile_record(current.product), current)
                self.assertTrue(store.read_product_profile_record(runtime.product).is_active)

    async def test_historical_alias_refuses_without_overriding_current_name_reuse(self) -> None:
        for reused in (False, True):
            with (
                self.subTest(reused=reused),
                TemporaryDirectory() as directory,
                closing(
                    PostgresRecordStore(
                        database_url=sqlite_database_url(Path(directory) / "db.sqlite")
                    )
                ) as store,
            ):
                store.ensure_schema()
                runtime = LaunchplaneProductProfileRecord.model_validate(
                    {
                        **product_profile_payload("runtime"),
                        "repository": "example/old-name",
                        "owner": {"github_login": "client", "github_id": "5678"},
                    }
                )
                store.write_repository_inventory_record(
                    _inventory(repository_id=42, repository=runtime.repository)
                )
                store.write_product_profile_record(runtime)
                store.write_repository_inventory_record(
                    _inventory(
                        repository_id=42, repository="example/current-name", inventory_revision=2
                    )
                )
                if reused:
                    store.write_repository_inventory_record(
                        _inventory(repository_id=43, repository=runtime.repository)
                    )
                app, sessions, session = self._registration_app(store)

                def token(*, repository: str, **kwargs: object) -> str:
                    current_tracked_inventory_record(
                        repository=repository,
                        inventory_records=store.list_repository_inventory_records(),
                    )
                    return "fixture-token"

                def github(*, method: str = "GET", **kwargs: object) -> dict[str, object] | None:
                    return (
                        None
                        if method == "DELETE"
                        else {"type": "User", "login": "example-operator", "id": 123}
                    )

                payload: dict[str, object] = {
                    "product": "launchplane",
                    "mode": "dry_run",
                    "manifest": {
                        **inert_manifest(),
                        "repository": "example/current-name",
                        "owner": {"github_login": "example-operator", "github_id": "123"},
                    },
                }
                with (
                    patch(
                        "control_plane.http_app.resolve_launchplane_github_token", side_effect=token
                    ),
                    patch("control_plane.http_app.github_api_request", side_effect=github),
                ):
                    planned = await _asgi_request(
                        app,
                        "POST",
                        ROUTE,
                        headers=_browser_mutation_headers(sessions, session),
                        payload=payload,
                    )
                    self.assertEqual(planned.status_code, 202 if reused else 409, planned.text)
                    if reused:
                        applied = await _asgi_request(
                            app,
                            "POST",
                            ROUTE,
                            headers={
                                **_browser_mutation_headers(sessions, session),
                                "Idempotency-Key": "name-reuse",
                            },
                            payload={
                                **payload,
                                "mode": "apply",
                                "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"],
                            },
                        )
                        self.assertEqual(applied.status_code, 202, applied.text)
                    else:
                        self.assertEqual(
                            planned.json()["error"]["code"], ProductRepositoryOwnershipError.code
                        )
                self.assertEqual(store.read_product_profile_record(runtime.product), runtime)
                self.assertEqual(len(store.list_product_profile_records()), 2 if reused else 1)

    async def test_generic_web_plan_refuses_inert_ownership_before_resource_planning(self) -> None:
        for kind in ("collision", "existing-key", "distinct"):
            with (
                self.subTest(kind=kind),
                TemporaryDirectory() as directory,
                closing(
                    PostgresRecordStore(
                        database_url=sqlite_database_url(Path(directory) / "db.sqlite")
                    )
                ) as store,
            ):
                store.ensure_schema()
                inert = inert_profile()
                store.write_product_profile_record(inert)
                before = store.list_product_profile_records()
                generic: dict[str, object] = {
                    "product": inert.product if kind == "existing-key" else "runtime",
                    "display_name": "Runtime",
                    "repository": inert.repository if kind == "collision" else "example/runtime",
                    "repository_id": "42",
                    "repository_owner_id": "7",
                    "image_repository": "ghcr.io/example/runtime",
                    "runtime_port": 3000,
                    "health_path": "/healthz",
                    "preview_base_url": "https://preview.example.com",
                }
                rule = _product_config_policy(
                    action="generic_web_onboarding.plan",
                    product="launchplane",
                    context="launchplane",
                ).model_dump(mode="json")["github_actions"][0]
                sessions = HumanSessionManager(
                    config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
                )
                session = sessions.issue(_github_human_identity())
                app = create_launchplane_fastapi_app(
                    verifier=StubVerifier(identity()),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate(
                        {
                            "github_actions": [
                                {
                                    **rule,
                                    "actions": [
                                        "generic_web_onboarding.plan",
                                        "product_onboarding.apply",
                                    ],
                                }
                            ]
                        }
                    ),
                    record_store_factory=lambda: store,
                    human_session_manager=sessions,
                )
                payload: dict[str, object] = {
                    "product": "launchplane",
                    "mode": "dry_run",
                    "generic_web": generic,
                }
                human = await _asgi_request(
                    app,
                    "POST",
                    ROUTE,
                    headers=_browser_mutation_headers(sessions, session),
                    payload=payload,
                )
                self.assertEqual(human.status_code, 403, human.text)
                response = await _asgi_request(
                    app,
                    "POST",
                    ROUTE,
                    headers={"Authorization": "Bearer valid-token"},
                    payload=payload,
                )
                self.assertEqual(
                    response.status_code,
                    {"collision": 409, "existing-key": 400, "distinct": 202}[kind],
                    response.text,
                )
                if kind == "collision":
                    self.assertEqual(
                        response.json()["error"]["code"], ProductRepositoryOwnershipError.code
                    )
                elif kind == "distinct":
                    self.assertTrue(response.json()["result"]["plan_sha256"])
                manifest = build_generic_web_onboarding_manifest(
                    intent=GenericWebOnboardingIntent.model_validate(generic),
                    target_id="fixture-target",
                )
                if kind != "distinct":
                    with (
                        patch(
                            "control_plane.workflows.product_onboarding.build_provider_target_records",
                            side_effect=AssertionError("Rejected profiles must not plan resources"),
                        ),
                        self.assertRaises(
                            ProductRepositoryOwnershipError
                            if kind == "collision"
                            else ProductClassificationChangeError
                        ),
                    ):
                        plan_product_onboarding_authority_bundle(
                            record_store=store, manifest=manifest
                        )
                self.assertEqual(store.list_product_profile_records(), before)
                self.assertEqual(store.list_physical_provider_target_records(), ())
                self.assertEqual(store.list_runtime_environment_records(), ())
                self.assertEqual(store.list_secret_bindings(), ())
                self.assertEqual(store.list_repository_inventory_records(), ())

    async def test_stale_inert_inventory_does_not_block_runtime_settings(self) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            runtime = LaunchplaneProductProfileRecord.model_validate(
                {
                    **product_profile_payload("example-site"),
                    "repository": "example/runtime",
                    "preview": {},
                }
            )
            inert = inert_profile()
            store.write_repository_inventory_record(
                _inventory(repository_id=42, repository=runtime.repository)
            )
            store.write_repository_inventory_record(
                _inventory(repository_id=43, repository=inert.repository)
            )
            store.write_product_profile_record(runtime)
            store.write_product_profile_record(inert)
            sessions = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = sessions.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    actions=("product_profile.write", "product_profile.read"),
                    product=runtime.product,
                    context="launchplane",
                ),
                human_session_manager=sessions,
                record_store_factory=lambda: store,
            )
            for renamed in (False, True):
                if renamed:
                    store.write_repository_inventory_record(
                        _inventory(
                            repository_id=43,
                            repository="example/renamed-inert",
                            inventory_revision=2,
                        )
                    )
                payload: dict[str, object] = {
                    "display_name": "After rename" if renamed else "Before rename",
                    "reason": "Update the runtime label",
                }
                route = f"/v1/product-profiles/{runtime.product}/display-name"
                planned = await _asgi_request(
                    app,
                    "POST",
                    route,
                    headers=_browser_mutation_headers(sessions, session),
                    payload=payload,
                )
                self.assertEqual(planned.status_code, 202, planned.text)
                response = await _asgi_request(
                    app,
                    "POST",
                    route,
                    headers={
                        **_browser_mutation_headers(sessions, session),
                        "Idempotency-Key": str(renamed),
                    },
                    payload={
                        **payload,
                        "mode": "apply",
                        "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"],
                    },
                )
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(
                    store.read_product_profile_record(runtime.product).display_name,
                    payload["display_name"],
                )
                self.assertEqual(store.read_product_profile_record(inert.product), inert)

    async def test_unsupported_client_registration_explains_the_domain_refusal(self) -> None:
        manifest = {**inert_manifest(), "release_on_acceptance": "held"}
        with self.assertRaises(ValidationError) as invalid:
            ProductOnboardingManifest.model_validate(manifest)
        domain_error = invalid.exception.errors()[0]["ctx"]["error"]
        with _database_app(LaunchplaneAuthzPolicy(), director_agent=True) as (store, _, app):
            response = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={"Authorization": "Bearer reader-token"},
                payload={"product": "launchplane", "mode": "dry_run", "manifest": manifest},
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(
                response.json()["error"]["code"], NonruntimeClientUnsupportedError.code
            )
            self.assertEqual(response.json()["error"]["message"], str(domain_error))
            self.assertEqual(store.list_product_profile_records(), ())

        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            retired = LaunchplaneProductProfileRecord.model_validate(
                {**inert_profile().model_dump(mode="json"), "lifecycle_state": "retired"}
            )
            profile = updated_product_owner_profile(
                profile=retired,
                resolved_owner=ProductOwnerIdentity(github_login="other-client", github_id="5678"),
                updated_at=retired.updated_at,
            )
            store.write_product_profile_record(profile)
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_product_config_policy(
                    action="product_profile.write", product=profile.product, context="launchplane"
                ),
                record_store_factory=lambda: store,
            )
            payload = profile.model_dump(mode="json")
            for changes, status in (({}, 202), ({"lifecycle_state": "active"}, 400)):
                with self.subTest(changes=changes):
                    response = await _asgi_request(
                        app,
                        "POST",
                        "/v1/product-profiles",
                        headers={"Authorization": "Bearer valid-token"},
                        payload={**payload, **changes},
                    )
                    self.assertEqual(response.status_code, status, response.text)
                    if changes:
                        self.assertEqual(
                            response.json()["error"]["code"], NonruntimeClientUnsupportedError.code
                        )
                        self.assertEqual(response.json()["error"]["message"], str(domain_error))
                    self.assertEqual(store.read_product_profile_record(profile.product), profile)

    async def test_expected_config_cannot_poison_nonruntime_profile_inventory(self) -> None:
        with _database_app(LaunchplaneAuthzPolicy(), director_agent=True) as (store, _, app):
            profile = inert_profile()
            store.write_product_profile_record(profile)
            for mode in ("dry-run", "apply"):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/product-profiles/expected-config/apply",
                    headers={"Authorization": "Bearer reader-token", "Idempotency-Key": mode},
                    payload={
                        "product": profile.product,
                        "mode": mode,
                        "reason": "Declare requirements",
                        "runtime_environment_keys": [{"key": "APP_MODE"}],
                    },
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(store.list_product_profile_records(), (profile,))

    async def test_full_profile_cannot_move_standing_acceptance_between_repositories(self) -> None:
        for managed_runtime in (False, True):
            with (
                self.subTest(managed_runtime=managed_runtime),
                TemporaryDirectory() as directory,
                closing(
                    PostgresRecordStore(
                        database_url=sqlite_database_url(Path(directory) / "db.sqlite")
                    )
                ) as store,
            ):
                store.ensure_schema()
                profile = inert_profile()
                if managed_runtime:
                    profile = LaunchplaneProductProfileRecord.model_validate(
                        {
                            **product_profile_payload(profile.product),
                            "owner": profile.owner.model_dump(mode="json"),
                            "release_on_acceptance": profile.release_on_acceptance,
                            "preview": {},
                        }
                    )
                app = create_launchplane_fastapi_app(
                    verifier=StubVerifier(identity()),
                    authz_policy=_product_config_policy(
                        action="product_profile.write",
                        product=profile.product,
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                for state in ("active", "retired"):
                    profile = profile.model_copy(update={"lifecycle_state": state})
                    store.write_product_profile_record(profile)
                    for changes in (
                        {"repository": "example/another"},
                        {"repository_id": "42", "repository_owner_id": "7"},
                    ):
                        with self.subTest(state=state, changes=changes):
                            response = await _asgi_request(
                                app,
                                "POST",
                                "/v1/product-profiles",
                                headers={"Authorization": "Bearer valid-token"},
                                payload={**profile.model_dump(mode="json"), **changes},
                            )
                            self.assertEqual(response.status_code, 409, response.text)
                            self.assertEqual(
                                store.read_product_profile_record(profile.product), profile
                            )

    async def test_registration_cannot_record_standing_from_machine_identity(self) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_product_config_policy(
                    action="product_onboarding.apply", product="launchplane", context="launchplane"
                ),
                record_store_factory=lambda: store,
            )
            headers = {"Authorization": "Bearer valid-token"}
            payload: dict[str, object] = {
                "product": "launchplane",
                "mode": "dry_run",
                "manifest": inert_manifest(),
            }
            dry = await _asgi_request(app, "POST", ROUTE, headers=headers, payload=payload)
            self.assertEqual(dry.status_code, 202, dry.text)
            refused = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={**headers, "Idempotency-Key": "machine-registration"},
                payload={
                    **payload,
                    "mode": "apply",
                    "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
                },
            )
            self.assertEqual(refused.status_code, 403, refused.text)
            self.assertEqual(store.list_product_profile_records(), ())

    async def test_inert_plan_uses_existing_plan_authority_without_apply(self) -> None:
        payload: dict[str, object] = {
            "product": "launchplane",
            "mode": "dry_run",
            "manifest": inert_manifest(),
        }
        for action, expected in (
            ("product_onboarding.plan", 202),
            ("generic_web_onboarding.plan", 403),
        ):
            with (
                self.subTest(action=action),
                TemporaryDirectory() as directory,
                closing(
                    PostgresRecordStore(
                        database_url=sqlite_database_url(Path(directory) / "db.sqlite")
                    )
                ) as store,
            ):
                store.ensure_schema()
                app = create_launchplane_fastapi_app(
                    verifier=StubVerifier(identity()),
                    authz_policy=_product_config_policy(
                        action=action, product="launchplane", context="launchplane"
                    ),
                    record_store_factory=lambda: store,
                )
                headers = {"Authorization": "Bearer valid-token"}
                dry = await _asgi_request(app, "POST", ROUTE, headers=headers, payload=payload)
                self.assertEqual(dry.status_code, expected, dry.text)
                apply = await _asgi_request(
                    app,
                    "POST",
                    ROUTE,
                    headers=headers,
                    payload={**payload, "mode": "apply", "reviewed_plan_sha256": "0" * 64},
                )
                self.assertEqual(apply.status_code, 403, apply.text)
                self.assertEqual(store.list_product_profile_records(), ())
                self.assertEqual(store.list_physical_provider_target_records(), ())
                self.assertEqual(store.list_runtime_environment_records(), ())
                self.assertEqual(store.list_secret_bindings(), ())
        with _database_app(LaunchplaneAuthzPolicy(schema_version=2), director_agent=True) as (
            store,
            _,
            app,
        ):
            headers = {"Authorization": "Bearer reader-token"}
            with patch.object(
                store,
                "write_idempotency_record",
                side_effect=AssertionError("A dry run cannot persist an apply receipt"),
            ):
                dry = await _asgi_request(
                    app,
                    "POST",
                    ROUTE,
                    headers={**headers, "Idempotency-Key": "director-plan"},
                    payload=payload,
                )
            self.assertEqual(dry.status_code, 202, dry.text)
            apply = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers=headers,
                payload={**payload, "mode": "apply", "reviewed_plan_sha256": "0" * 64},
            )
            self.assertEqual(apply.status_code, 403, apply.text)
            self.assertEqual(store.list_product_profile_records(), ())
            self.assertEqual(store.list_physical_provider_target_records(), ())
            self.assertEqual(store.list_runtime_environment_records(), ())
            self.assertEqual(store.list_secret_bindings(), ())

    async def test_reviewed_registration_is_write_free_until_apply_and_replays(self) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            sessions = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = sessions.issue(_github_human_identity())
            rule = _github_human_product_config_policy(
                actions=(
                    "product_onboarding.apply",
                    "product_profile.read",
                    "product_environment.read",
                    "product_profile.write",
                ),
                product="launchplane",
                context="launchplane",
            ).model_dump(mode="json")["github_humans"][0]
            machine_rule = _product_config_policy(
                action="product_profile.write", product="launchplane", context="launchplane"
            ).model_dump(mode="json")["github_actions"][0]
            machine_rule["actions"] = ["product_profile.write", "product_onboarding.apply"]
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_humans": [
                            {**rule, "products": ["launchplane", "desktop-tool", "another"]}
                        ],
                        "github_actions": [
                            {**machine_rule, "products": ["launchplane", "desktop-tool", "another"]}
                        ],
                    }
                ),
                record_store_factory=lambda: store,
                human_session_manager=sessions,
            )
            unreviewed = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers={"Authorization": "Bearer valid-token"},
                payload=inert_profile().model_dump(mode="json"),
            )
            self.assertEqual(unreviewed.status_code, 409, unreviewed.text)
            payload: dict[str, object] = {
                "product": "launchplane",
                "mode": "dry_run",
                "manifest": {
                    **inert_manifest(),
                    "owner": {"github_login": "DiReCtOr", "github_id": "1234"},
                },
            }
            dry = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers=_browser_mutation_headers(sessions, session),
                payload=payload,
            )
            self.assertEqual(dry.status_code, 202, dry.text)
            canonical_manifest = ProductOnboardingManifest.model_validate(inert_manifest())
            self.assertEqual(
                dry.json()["result"]["product_profile"]["owner"],
                canonical_manifest.owner.model_dump(mode="json"),
            )
            self.assertEqual(
                dry.json()["result"]["plan_sha256"],
                product_onboarding_manifest_plan_sha256(canonical_manifest),
            )
            self.assertEqual(store.list_product_profile_records(), ())
            apply = {
                **payload,
                "mode": "apply",
                "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
            }
            missing_key = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers=_browser_mutation_headers(sessions, session),
                payload=apply,
            )
            self.assertEqual(missing_key.status_code, 400, missing_key.text)
            changed = {**apply, "manifest": {**inert_manifest(), "display_name": "Changed"}}
            refused = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={
                    **_browser_mutation_headers(sessions, session),
                    "Idempotency-Key": "stale",
                },
                payload=changed,
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(store.list_product_profile_records(), ())
            with patch(
                "control_plane.http_app.github_api_request",
                return_value={"type": "User", "login": "director", "id": 5678},
            ):
                mismatched = await _asgi_request(
                    app,
                    "POST",
                    ROUTE,
                    headers={
                        **_browser_mutation_headers(sessions, session),
                        "Idempotency-Key": "wrong-client",
                    },
                    payload=apply,
                )
            self.assertEqual(mismatched.status_code, 400, mismatched.text)
            self.assertEqual(store.list_product_profile_records(), ())
            accepted = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={
                    **_browser_mutation_headers(sessions, session),
                    "Idempotency-Key": "register",
                },
                payload=apply,
            )
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.assertEqual(
                store.read_product_profile_record("desktop-tool").owner, canonical_manifest.owner
            )
            self.assertEqual(
                accepted.json()["result"]["product_profile"]["owner"],
                canonical_manifest.owner.model_dump(mode="json"),
            )
            replay = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={
                    **_browser_mutation_headers(sessions, session),
                    "Idempotency-Key": "register",
                },
                payload=apply,
            )
            self.assertEqual(replay.json()["original_trace_id"], accepted.json()["trace_id"])
            self.assertEqual(replay.json()["result"], accepted.json()["result"])
            for missed_profile in (False, True):
                with (
                    self.subTest(missed_profile=missed_profile),
                    patch.object(
                        store,
                        "read_idempotency_record",
                        side_effect=chain((None,), repeat(DEFAULT)),
                        wraps=store.read_idempotency_record,
                    ),
                    patch.object(
                        store,
                        "read_product_profile_record",
                        side_effect=chain((None,), repeat(DEFAULT)),
                        wraps=store.read_product_profile_record,
                    )
                    if missed_profile
                    else nullcontext(),
                ):
                    raced_replay = await _asgi_request(
                        app,
                        "POST",
                        ROUTE,
                        headers={
                            **_browser_mutation_headers(sessions, session),
                            "Idempotency-Key": "register",
                        },
                        payload=apply,
                    )
                self.assertEqual(raced_replay.status_code, 202, raced_replay.text)
                self.assertEqual(
                    raced_replay.json()["original_trace_id"], accepted.json()["trace_id"]
                )
                self.assertEqual(raced_replay.json()["result"], accepted.json()["result"])
            readback = store.read_product_profile_record("desktop-tool")
            self.assertEqual(readback.repository, "example/desktop-tool")
            self.assertFalse(readback.is_managed_runtime)
            conflicting = {**product_profile_payload("another"), "repository": readback.repository}
            refused = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers={"Authorization": "Bearer valid-token"},
                payload=conflicting,
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(refused.json()["error"]["code"], ProductRepositoryOwnershipError.code)
            runtime_onboarding: dict[str, object] = {
                "product": "launchplane",
                "manifest": {
                    "product": "another",
                    "display_name": "Another",
                    "repository": readback.repository,
                    "image_repository": "ghcr.io/example/another",
                    "lanes": [{"context": "another", "instance": "testing"}],
                },
            }
            browser_runtime = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={
                    **_browser_mutation_headers(sessions, session),
                    "Idempotency-Key": "browser-runtime",
                },
                payload=runtime_onboarding,
            )
            self.assertEqual(browser_runtime.status_code, 403, browser_runtime.text)
            refused = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "runtime-collision",
                },
                payload=runtime_onboarding,
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(refused.json()["error"]["code"], ProductRepositoryOwnershipError.code)
            self.assertEqual(store.list_physical_provider_target_records(), ())
            self.assertEqual(store.list_runtime_environment_records(), ())
            self.assertEqual(store.list_secret_bindings(), ())
            profile_read = await _asgi_request(
                app,
                "GET",
                "/v1/product-profiles/desktop-tool",
                headers=_browser_mutation_headers(sessions, session),
            )
            self.assertEqual(profile_read.status_code, 200, profile_read.text)
            self.assertEqual(
                profile_read.json()["profile"]["classification"], readback.classification
            )
            mapping_read = await _asgi_request(
                app,
                "GET",
                "/v1/repo-product-mapping",
                headers=_browser_mutation_headers(sessions, session),
            )
            self.assertEqual(mapping_read.status_code, 200, mapping_read.text)
            self.assertEqual(
                mapping_read.json()["mapping"]["repositories"][0]["product"], readback.product
            )
            self.assertEqual(readback.owner, inert_profile().owner)
            self.assertEqual(readback.release_on_acceptance, inert_profile().release_on_acceptance)
            with self.assertRaises(ValueError):
                plan_product_onboarding_authority_bundle(
                    record_store=store,
                    manifest=ProductOnboardingManifest.model_validate(inert_manifest()),
                )
            current = store.read_product_profile_record(readback.product)
            converted = {
                **product_profile_payload(current.product),
                "repository": current.repository,
                "owner": current.owner.model_dump(mode="json"),
                "release_on_acceptance": current.release_on_acceptance,
            }
            refused = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers={"Authorization": "Bearer valid-token"},
                payload=converted,
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(refused.json()["error"]["code"], ProductClassificationChangeError.code)
            self.assertEqual(store.read_product_profile_record(current.product), current)
            retired = current.model_copy(update={"lifecycle_state": "retired"})
            retirement = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers={"Authorization": "Bearer valid-token", "Idempotency-Key": "retire-inert"},
                payload=retired.model_dump(mode="json"),
            )
            self.assertEqual(retirement.status_code, 202, retirement.text)
            old_read = await _asgi_request(
                app,
                "GET",
                f"/v1/product-profiles/{current.product}",
                headers=_browser_mutation_headers(sessions, session),
            )
            self.assertEqual(old_read.status_code, 200, old_read.text)
            self.assertEqual(
                LaunchplaneProductProfileRecord.model_validate(old_read.json()["profile"]),
                retired,
            )
            no_mapping = await _asgi_request(
                app,
                "GET",
                "/v1/repo-product-mapping",
                headers=_browser_mutation_headers(sessions, session),
            )
            self.assertEqual(no_mapping.status_code, 200, no_mapping.text)
            self.assertEqual(no_mapping.json()["mapping"]["repositories"], [])
            replacement = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "runtime-replacement",
                },
                payload=runtime_onboarding,
            )
            self.assertEqual(replacement.status_code, 202, replacement.text)
            new_read = await _asgi_request(
                app,
                "GET",
                "/v1/product-profiles/another",
                headers=_browser_mutation_headers(sessions, session),
            )
            self.assertEqual(new_read.status_code, 200, new_read.text)
            new_profile = LaunchplaneProductProfileRecord.model_validate(new_read.json()["profile"])
            self.assertTrue(new_profile.is_managed_runtime)
            self.assertFalse(new_profile.owner.is_set)
            self.assertNotEqual(new_profile.release_on_acceptance, retired.release_on_acceptance)
            self.assertEqual(store.read_product_profile_record(current.product), retired)
            new_mapping = await _asgi_request(
                app,
                "GET",
                "/v1/repo-product-mapping",
                headers=_browser_mutation_headers(sessions, session),
            )
            self.assertEqual(new_mapping.status_code, 200, new_mapping.text)
            self.assertEqual(
                [entry["product"] for entry in new_mapping.json()["mapping"]["repositories"]],
                [new_profile.product],
            )

    async def test_ambiguous_repository_reads_fail_without_selecting_a_product(self) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            safe = inert_profile()
            store.write_product_profile_record(safe)
            store.write_repository_inventory_record(
                _inventory(repository_id=42, repository=safe.repository)
            )
            store.write_repository_inventory_record(
                _inventory(repository_id=43, repository="example/shared")
            )
            for product in ("first", "second"):
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        {
                            **product_profile_payload(product),
                            "repository": "example/shared",
                        }
                    )
                )
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_product_config_policy(
                    action="product_environment.read", product="launchplane", context="launchplane"
                ),
                record_store_factory=lambda: store,
            )
            headers = {"Authorization": "Bearer valid-token"}
            mapping = await _asgi_request(app, "GET", "/v1/repo-product-mapping", headers=headers)
            self.assertEqual(mapping.status_code, 200, mapping.text)
            self.assertEqual(
                mapping.json()["mapping"]["ambiguous_repositories"], ["example/shared"]
            )
            self.assertEqual(
                [entry["product"] for entry in mapping.json()["mapping"]["repositories"]],
                [safe.product],
            )
            context = await _asgi_request(app, "GET", "/v1/agent/context", headers=headers)
            self.assertEqual(context.status_code, 200, context.text)
            section = context.json()["context"]["sections"]["repo_product_mapping"]
            self.assertEqual(section["status"], "available")
            self.assertEqual(section["payload"]["mapping"], mapping.json()["mapping"])

    async def test_standing_metadata_preserves_signed_admin_and_current_profile_guards(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as directory,
            closing(
                PostgresRecordStore(database_url=sqlite_database_url(Path(directory) / "db.sqlite"))
            ) as store,
        ):
            store.ensure_schema()
            profile = inert_profile().model_copy(
                update={
                    "owner": ProductOwnerProfile(github_login="example-client", github_id="1234")
                }
            )
            store.write_product_profile_record(profile)
            policy = _product_config_policy(
                action="product_profile.write", product=profile.product, context="launchplane"
            )
            machine = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            route = f"/v1/product-profiles/{profile.product}/production-use"
            payload: dict[str, object] = {
                "production_use": "unknown",
                "release_on_acceptance": "director_standing",
                "reason": "The recorded Client is the Director.",
            }
            headers = {"Authorization": "Bearer valid-token"}
            dry = await _asgi_request(machine, "POST", route, headers=headers, payload=payload)
            self.assertEqual(dry.status_code, 202, dry.text)
            apply = {
                **payload,
                "mode": "apply",
                "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
            }
            denied = await _asgi_request(
                machine,
                "POST",
                route,
                headers={**headers, "Idempotency-Key": "machine"},
                payload=apply,
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            sessions = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = sessions.issue(_github_human_identity())
            human = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    action="product_profile.write", product=profile.product, context="launchplane"
                ),
                record_store_factory=lambda: store,
                human_session_manager=sessions,
            )
            browser_headers = {
                **_browser_mutation_headers(sessions, session),
                "Idempotency-Key": "human",
            }
            accepted = await _asgi_request(
                human, "POST", route, headers=browser_headers, payload=apply
            )
            self.assertEqual(accepted.status_code, 202, accepted.text)
            recorded = store.read_product_profile_record(profile.product)
            self.assertEqual(recorded.release_on_acceptance, "director_standing")
            self.assertFalse(recorded.is_managed_runtime)
            store.write_product_profile_record(
                recorded.model_copy(
                    update={
                        "owner": ProductOwnerProfile(
                            github_login="changed-client", github_id="5678"
                        ),
                        "release_on_acceptance": "director_standing",
                    }
                )
            )
            stale = await _asgi_request(
                human,
                "POST",
                route,
                headers={
                    **_browser_mutation_headers(sessions, session),
                    "Idempotency-Key": "stale-owner",
                },
                payload=apply,
            )
            self.assertEqual(stale.status_code, 409, stale.text)
