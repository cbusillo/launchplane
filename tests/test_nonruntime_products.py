"""Repository/Client identity without runtime authority, through retained surfaces."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from itertools import chain, repeat
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import DEFAULT, Mock, patch

import click
from pydantic import ValidationError

from control_plane.artifact_retention import product_retention_dry_run
from control_plane.artifact_scan import qualify_product_artifact
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductClassificationChangeError,
    ProductOwnerProfile,
)
from control_plane.contracts.product_environment_read_model import build_product_site_overview
from control_plane.contracts.product_reconcile import ProductReconcileRequestRecord
from control_plane.contracts.repo_product_mapping_read_model import (
    build_repo_product_mapping_from_records,
)
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
)
from control_plane.testing_activation import read_product_compose
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    resolve_generic_web_profile_lane,
)
from control_plane.product_promotion_http import resolve_product_promotion_target
from control_plane.release_invitation import publish_release_invitation
from control_plane.workflows.product_onboarding import plan_product_onboarding_authority_bundle
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
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
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
        updated_at=NOW,
        source="test",
    )


def inert_manifest() -> dict[str, object]:
    return {
        "classification": "active_awareness",
        "product": "desktop-tool",
        "display_name": "Desktop tool",
        "repository": "Example/Desktop-Tool",
        "driver_id": "",
    }


def assert_registration_race(case: unittest.TestCase, store: PostgresRecordStore) -> None:
    """Independent transactions compete for both a key and a normalized repository."""
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


class NonruntimeProductTests(unittest.TestCase):
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
                    store.write_product_profile_record(other)
                    with self.assertRaises(ProductRepositoryOwnershipError):
                        store.compare_and_write_product_profile_record(
                            expected_record=other,
                            replacement_record=other.model_copy(
                                update={"repository": profile.repository}
                            ),
                        )
                    self.assertEqual(store.read_product_profile_record(other.product), other)

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
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                **_product_config_policy(
                                    action="product_onboarding.apply",
                                    product="launchplane",
                                    context="launchplane",
                                ).model_dump(mode="json")["github_actions"][0],
                                "products": ["launchplane", "desktop-tool", "another"],
                                "actions": [
                                    "product_onboarding.apply",
                                    "product_profile.read",
                                    "product_environment.read",
                                    "product_profile.write",
                                ],
                            }
                        ]
                    }
                ),
                record_store_factory=lambda: store,
            )
            headers = {"Authorization": "Bearer valid-token"}
            unreviewed = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers=headers,
                payload=inert_profile().model_dump(mode="json"),
            )
            self.assertEqual(unreviewed.status_code, 409, unreviewed.text)
            payload: dict[str, object] = {
                "product": "launchplane",
                "mode": "dry_run",
                "manifest": inert_manifest(),
            }
            dry = await _asgi_request(app, "POST", ROUTE, headers=headers, payload=payload)
            self.assertEqual(dry.status_code, 202, dry.text)
            self.assertEqual(store.list_product_profile_records(), ())
            apply = {
                **payload,
                "mode": "apply",
                "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
            }
            missing_key = await _asgi_request(app, "POST", ROUTE, headers=headers, payload=apply)
            self.assertEqual(missing_key.status_code, 400, missing_key.text)
            changed = {**apply, "manifest": {**inert_manifest(), "display_name": "Changed"}}
            refused = await _asgi_request(
                app, "POST", ROUTE, headers={**headers, "Idempotency-Key": "stale"}, payload=changed
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(store.list_product_profile_records(), ())
            accepted = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={**headers, "Idempotency-Key": "register"},
                payload=apply,
            )
            self.assertEqual(accepted.status_code, 202, accepted.text)
            replay = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={**headers, "Idempotency-Key": "register"},
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
                        headers={**headers, "Idempotency-Key": "register"},
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
                app, "POST", "/v1/product-profiles", headers=headers, payload=conflicting
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
            refused = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={**headers, "Idempotency-Key": "runtime-collision"},
                payload=runtime_onboarding,
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(refused.json()["error"]["code"], ProductRepositoryOwnershipError.code)
            self.assertEqual(store.list_physical_provider_target_records(), ())
            self.assertEqual(store.list_runtime_environment_records(), ())
            self.assertEqual(store.list_secret_bindings(), ())
            profile_read = await _asgi_request(
                app, "GET", "/v1/product-profiles/desktop-tool", headers=headers
            )
            self.assertEqual(profile_read.status_code, 200, profile_read.text)
            self.assertEqual(
                profile_read.json()["profile"]["classification"], readback.classification
            )
            mapping_read = await _asgi_request(
                app, "GET", "/v1/repo-product-mapping", headers=headers
            )
            self.assertEqual(mapping_read.status_code, 200, mapping_read.text)
            self.assertEqual(
                mapping_read.json()["mapping"]["repositories"][0]["product"], readback.product
            )
            owner_route = f"/v1/product-profiles/{readback.product}/owner"
            owner_payload: dict[str, object] = {
                "github_login": "example-client",
                "reason": "Record this product's Client.",
            }
            with (
                patch(
                    "control_plane.http_app.resolve_launchplane_github_token",
                    return_value="managed-fixture-token",
                ),
                patch(
                    "control_plane.http_app.github_api_request",
                    return_value={"type": "User", "login": "example-client", "id": 1234},
                ),
            ):
                owner_dry = await _asgi_request(
                    app, "POST", owner_route, headers=headers, payload=owner_payload
                )
                self.assertEqual(owner_dry.status_code, 202, owner_dry.text)
                self.assertFalse(store.read_product_profile_record(readback.product).owner.is_set)
                owner_apply = await _asgi_request(
                    app,
                    "POST",
                    owner_route,
                    headers={**headers, "Idempotency-Key": "client"},
                    payload={**owner_payload, "mode": "apply"},
                )
                self.assertEqual(owner_apply.status_code, 202, owner_apply.text)
            self.assertEqual(
                store.read_product_profile_record(readback.product).owner.github_login,
                "example-client",
            )
            with self.assertRaises(ValueError):
                plan_product_onboarding_authority_bundle(
                    record_store=store,
                    manifest=ProductOnboardingManifest.model_validate(inert_manifest()),
                )

            # Reuse the existing full-profile service for an inert record's lifecycle.
            current = store.read_product_profile_record(readback.product)
            # Standing setup; the separate signed-human test proves its service authority.
            current = current.model_copy(update={"release_on_acceptance": "director_standing"})
            store.write_product_profile_record(current)
            converted = {
                **product_profile_payload(current.product),
                "repository": current.repository,
                "owner": current.owner.model_dump(mode="json"),
                "release_on_acceptance": current.release_on_acceptance,
            }
            refused = await _asgi_request(
                app, "POST", "/v1/product-profiles", headers=headers, payload=converted
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(refused.json()["error"]["code"], ProductClassificationChangeError.code)
            self.assertEqual(store.read_product_profile_record(current.product), current)
            retired = current.model_copy(update={"lifecycle_state": "retired"})
            retirement = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles",
                headers={**headers, "Idempotency-Key": "retire-inert"},
                payload=retired.model_dump(mode="json"),
            )
            self.assertEqual(retirement.status_code, 202, retirement.text)
            old_read = await _asgi_request(
                app, "GET", f"/v1/product-profiles/{current.product}", headers=headers
            )
            self.assertEqual(old_read.status_code, 200, old_read.text)
            self.assertEqual(
                LaunchplaneProductProfileRecord.model_validate(old_read.json()["profile"]),
                retired,
            )
            no_mapping = await _asgi_request(
                app, "GET", "/v1/repo-product-mapping", headers=headers
            )
            self.assertEqual(no_mapping.status_code, 200, no_mapping.text)
            self.assertEqual(no_mapping.json()["mapping"]["repositories"], [])
            replacement = await _asgi_request(
                app,
                "POST",
                ROUTE,
                headers={**headers, "Idempotency-Key": "runtime-replacement"},
                payload=runtime_onboarding,
            )
            self.assertEqual(replacement.status_code, 202, replacement.text)
            new_read = await _asgi_request(
                app, "GET", "/v1/product-profiles/another", headers=headers
            )
            self.assertEqual(new_read.status_code, 200, new_read.text)
            new_profile = LaunchplaneProductProfileRecord.model_validate(new_read.json()["profile"])
            self.assertTrue(new_profile.is_managed_runtime)
            self.assertFalse(new_profile.owner.is_set)
            self.assertEqual(
                new_profile.release_on_acceptance, inert_profile().release_on_acceptance
            )
            self.assertNotEqual(new_profile.release_on_acceptance, retired.release_on_acceptance)
            self.assertEqual(store.read_product_profile_record(current.product), retired)
            new_mapping = await _asgi_request(
                app, "GET", "/v1/repo-product-mapping", headers=headers
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
                        "release_on_acceptance": "held",
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
