from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.postgres_preview import effective_postgres_preview_profile
from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.drivers.postgres_preview_transport import (
    PostgresPreviewDestroyEnvelope,
    PostgresPreviewRefreshEnvelope,
    apply_postgres_preview_destroy_result,
    apply_postgres_preview_refresh_result,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.workflows.postgres_preview import (
    PostgresPreviewDestroyRequest,
    PostgresPreviewDestroyResult,
    PostgresPreviewRefreshConfigError,
    PostgresPreviewRefreshResult,
    _build_preview_runtime_identity,
)
from tests.test_generic_web_preview_extensions import _driver_profile
from tests.test_postgres_preview import _refresh_request
from tests.test_product_retirement import _Store, _observation, _plan


def _site(
    product: str, *, app: str, database: str, slug: str = "pr-{number}", postgres: bool = True
) -> LaunchplaneProductProfileRecord:
    payload = _driver_profile().model_dump(mode="json")
    payload.update(product=product, repository=f"example/{product}")
    payload["lanes"][0]["context"] = product
    payload["preview"].update(
        context=f"{product}-previews",
        app_name_prefix=app,
        transport_database_prefix=database,
        slug_template=slug,
    )
    if not postgres:
        payload["preview"].update(
            data_transport_mode="none", transport_extension="", generated_secret_env={}
        )
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _preview(profile: LaunchplaneProductProfileRecord) -> PreviewRecord:
    return PreviewRecord(
        preview_id=f"{profile.product}-pr-7",
        context=profile.preview.context,
        anchor_repo=profile.repository.partition("/")[2],
        anchor_pr_number=7,
        anchor_pr_url=f"https://github.com/{profile.repository}/pull/7",
        preview_label="pr-7",
        canonical_url="https://preview.example.test",
        state="pending",
        created_at="2026-10-28T12:00:00Z",
        updated_at="2026-10-28T12:00:00Z",
        eligible_at="2026-10-28T12:00:00Z",
    )


class PreviewNamespaceTests(unittest.TestCase):
    def test_destroy_preflight_keeps_resources_unrecorded_and_destroyed_cleanup_is_inert(
        self,
    ) -> None:
        for backend in ("filesystem", "postgres"):
            with self.subTest(backend=backend), self.store(backend) as store:
                profile = _driver_profile()
                store.write_product_profile_record(profile)
                preview = _preview(profile)
                request = PostgresPreviewDestroyEnvelope(
                    product=profile.product,
                    destroy=PostgresPreviewDestroyRequest(
                        context=preview.context,
                        anchor_repo=preview.anchor_repo,
                        anchor_pr_number=preview.anchor_pr_number,
                        preview_slug=preview.preview_label,
                        destroy_reason="closed",
                    ),
                )
                with patch(
                    "control_plane.drivers.postgres_preview_transport.execute_postgres_preview_destroy",
                    return_value=PostgresPreviewDestroyResult(
                        destroy_status="fail",
                        destroy_started_at=preview.created_at,
                        destroy_finished_at=preview.updated_at,
                        application_name="",
                        application_id="",
                        preview_url="",
                        error_message="Missing testing template",
                    ),
                ) as execute:
                    records, result = apply_postgres_preview_destroy_result(
                        control_plane_root=Path("."),
                        record_store=store,
                        request=request,
                        execution_profile=profile,
                    )
                    self.assertEqual(records, {})
                    self.assertEqual(result["destroy_status"], "fail")
                    self.assertEqual(store.list_preview_records(), ())
                    store.write_preview_record(
                        preview.model_copy(
                            update={"state": "destroyed", "destroyed_at": preview.updated_at}
                        )
                    )
                    execute.reset_mock()
                    _, result = apply_postgres_preview_destroy_result(
                        control_plane_root=Path("."),
                        record_store=store,
                        request=request,
                        execution_profile=profile,
                    )
                    self.assertEqual(result["destroy_status"], "pass")
                    execute.assert_not_called()

    def test_retirement_reads_preserve_legacy_profile_hash_fences(self) -> None:
        payload = _driver_profile().model_dump(mode="json")
        payload["preview"]["transport_extension"] = "verireel-postgres"
        payload["preview"].pop("generated_secret_env")
        payload["preview"]["preview_url_env_keys"] = []
        payload["preview"]["preview_domain_env_keys"] = []
        payload["lifecycle_state"] = "retiring"
        payload["preview"]["enabled"] = False
        profile = LaunchplaneProductProfileRecord.model_validate(
            payload, context={"stored_product_profile": True}
        )
        profile_payload = profile.model_dump(mode="json")
        digest = canonical_sha256(profile_payload)
        plan = _plan(_Store(), _observation())
        checkpoint = plan.model_copy(
            update={
                "record_id": "legacy-retiring-checkpoint",
                "mode": "apply",
                "outcome": "started",
                "reviewed_plan_record_id": plan.plan_record_id,
                "reviewed_plan_sha256": plan.plan_sha256,
                "profile_record": profile,
                "mutation_evidence": plan.mutation_evidence.model_copy(
                    update={"retiring_profile_sha256": digest}
                ),
            }
        )
        for backend in ("filesystem", "postgres"):
            with self.subTest(backend=backend), self.store(backend) as store:
                store.write_product_retirement_record(checkpoint)
                for loaded in (
                    store.read_product_retirement_record(checkpoint.record_id),
                    *store.list_product_retirement_records(product=checkpoint.product),
                ):
                    assert loaded is not None and loaded.profile_record is not None
                    self.assertEqual(loaded.profile_record.model_dump(mode="json"), profile_payload)
                    self.assertEqual(
                        canonical_sha256(loaded.profile_record.model_dump(mode="json")),
                        loaded.mutation_evidence.retiring_profile_sha256,
                    )

    @contextmanager
    def store(self, backend: str) -> Iterator[FilesystemRecordStore | PostgresRecordStore]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = (
                FilesystemRecordStore(root)
                if backend == "filesystem"
                else PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'records.db'}")
            )
            if isinstance(store, PostgresRecordStore):
                store.ensure_schema()
            try:
                yield store
            finally:
                if isinstance(store, PostgresRecordStore):
                    store.close()

    def test_rendered_application_database_and_default_prefix_collisions_refuse_every_write_path(
        self,
    ) -> None:
        pairs = (
            (
                _site("first", app="acme", database="first_"),
                _site("second", app="acme-pr", database="second_", slug="{number}"),
            ),
            (
                _site("first", app="first", database="shop_"),
                _site("second", app="second", database="shop_pr_", slug="{number}"),
            ),
            (
                _site("acme", app="", database="", postgres=False),
                _site("second", app="acme-preview", database="second_"),
            ),
        )
        for backend in ("filesystem", "postgres"):
            for first, conflict in pairs:
                with (
                    self.subTest(backend=backend, first=first.product, conflict=conflict.preview),
                    self.store(backend) as store,
                ):
                    store.write_product_profile_record(first)
                    with self.assertRaises(ValueError):
                        store.write_product_profile_record(conflict)
                    second = _site("second", app="distinct", database="distinct_")
                    store.write_product_profile_record(second)
                    with self.assertRaises(ValueError):
                        store.compare_and_write_product_profile_record(
                            expected_record=second, replacement_record=conflict
                        )
                    with self.assertRaises(ValueError):
                        store.write_product_authority_bundle(
                            ProductAuthorityBundle(
                                product_profiles=(conflict,), expected_product_profiles=(second,)
                            )
                        )
                    self.assertEqual(store.read_product_profile_record(second.product), second)

    def test_bundle_checks_final_namespaces_and_cleanup_precedes_declaration_changes(self) -> None:
        for backend in ("filesystem", "postgres"):
            with self.subTest(backend=backend), self.store(backend) as store:
                first = _site("first", app="first", database="first_")
                second = _site("second", app="second", database="second_")
                store.write_product_profile_record(first)
                store.write_product_profile_record(second)
                moved_first = first.model_copy(
                    update={
                        "preview": first.preview.model_copy(
                            update={
                                "app_name_prefix": "second",
                                "transport_database_prefix": "second_",
                            }
                        )
                    }
                )
                moved_second = second.model_copy(
                    update={
                        "preview": second.preview.model_copy(
                            update={
                                "app_name_prefix": "first",
                                "transport_database_prefix": "first_",
                            }
                        )
                    }
                )
                store.write_product_authority_bundle(
                    ProductAuthorityBundle(
                        product_profiles=(moved_first, moved_second),
                        expected_product_profiles=(first, second),
                    )
                )
                self.assertEqual(store.read_product_profile_record(first.product), moved_first)
                preview = _preview(moved_first)
                store.write_preview_record(preview)
                renamed = moved_first.model_copy(
                    update={
                        "preview": moved_first.preview.model_copy(
                            update={"app_name_suffix": "-other"}
                        )
                    }
                )
                for write in (
                    lambda: store.write_product_profile_record(renamed),
                    lambda: store.compare_and_write_product_profile_record(
                        expected_record=moved_first, replacement_record=renamed
                    ),
                    lambda: store.write_product_authority_bundle(
                        ProductAuthorityBundle(
                            product_profiles=(renamed,), expected_product_profiles=(moved_first,)
                        )
                    ),
                ):
                    with self.assertRaises(ValueError):
                        write()
                disabled = moved_first.model_copy(
                    update={"preview": moved_first.preview.model_copy(update={"enabled": False})}
                )
                store.write_product_profile_record(disabled)
                store.write_preview_record(
                    preview.model_copy(
                        update={"state": "destroyed", "destroyed_at": preview.updated_at}
                    )
                )
                store.write_product_profile_record(renamed)
                self.assertEqual(store.read_product_profile_record(first.product), renamed)

    def test_profile_authority_is_rechecked_when_pending_evidence_is_published(self) -> None:
        for backend in ("filesystem", "postgres"):
            with self.subTest(backend=backend), self.store(backend) as store:
                profile = _driver_profile()
                store.write_product_profile_record(profile)
                changed = profile.model_copy(
                    update={
                        "preview": profile.preview.model_copy(update={"app_name_suffix": "-other"})
                    }
                )
                store.write_product_profile_record(changed)
                with self.assertRaises(ValueError):
                    store.write_preview_record(_preview(profile), expected_profile=profile)
                self.assertEqual(store.list_preview_records(), ())

    def test_preflight_failure_leaves_no_resource_and_prepared_refresh_freezes_names(self) -> None:
        for backend in ("filesystem", "postgres"):
            with self.subTest(backend=backend), self.store(backend) as store:
                profile = _driver_profile()
                store.write_product_profile_record(profile)
                request = PostgresPreviewRefreshEnvelope(
                    product=profile.product, refresh=_refresh_request()
                )
                with patch(
                    "control_plane.drivers.postgres_preview_transport.execute_postgres_preview_refresh",
                    side_effect=PostgresPreviewRefreshConfigError("Missing testing template"),
                ):
                    records, result = apply_postgres_preview_refresh_result(
                        control_plane_root=Path("."),
                        record_store=store,
                        request=request,
                        execution_profile=profile,
                    )
                self.assertEqual(records, {})
                self.assertEqual(result["refresh_status"], "fail")
                self.assertEqual(store.list_preview_records(), ())

                def execute(**kwargs: object) -> PostgresPreviewRefreshResult:
                    prepare = kwargs["prepare_preview"]
                    assert callable(prepare)
                    prepare()
                    pending = store.list_preview_records()[0]
                    self.assertEqual(pending.state, "pending")
                    generation = store.list_preview_generation_records(
                        preview_id=pending.preview_id
                    )[0]
                    self.assertEqual(generation.state, "deploying")
                    renamed = profile.model_copy(
                        update={
                            "preview": profile.preview.model_copy(
                                update={"transport_database_prefix": "other_"}
                            )
                        }
                    )
                    with self.assertRaises(ValueError):
                        store.write_product_profile_record(renamed)
                    return PostgresPreviewRefreshResult(
                        refresh_status="pass",
                        refresh_started_at=pending.created_at,
                        refresh_finished_at=pending.updated_at,
                        application_name="fixture-preview",
                        application_id="fixture",
                        preview_url=request.refresh.preview_url,
                        runtime_identity=_build_preview_runtime_identity(
                            request=request.refresh,
                            product=profile.product,
                            preview_id=pending.preview_id,
                            preview_generation_id=generation.generation_id,
                        ),
                    )

                with patch(
                    "control_plane.drivers.postgres_preview_transport.execute_postgres_preview_refresh",
                    side_effect=execute,
                ):
                    records, result = apply_postgres_preview_refresh_result(
                        control_plane_root=Path("."),
                        record_store=store,
                        request=request,
                        execution_profile=profile,
                    )
                self.assertEqual(result["refresh_status"], "pass")
                self.assertEqual(
                    len(
                        store.list_preview_generation_records(preview_id=str(records["preview_id"]))
                    ),
                    1,
                )

    def test_legacy_execution_adaptation_preserves_raw_profile_and_fixed_endpoint(self) -> None:
        baseline = _driver_profile()
        payload = baseline.model_dump(mode="json")
        payload["runtime_port"] = 8081
        payload["health_path"] = "/different"
        payload["preview"]["transport_extension"] = "verireel-postgres"
        payload["preview"]["domain_certificate_type"] = "letsencrypt"
        payload["preview"].pop("generated_secret_env")
        raw = LaunchplaneProductProfileRecord.model_validate(
            payload, context={"stored_product_profile": True}
        )
        before = raw.model_dump_json()
        effective = effective_postgres_preview_profile(raw)
        self.assertEqual(raw.model_dump_json(), before)
        self.assertEqual(effective.runtime_port, baseline.runtime_port)
        self.assertEqual(effective.health_path, baseline.health_path)
        self.assertEqual(
            effective.preview.domain_certificate_type, baseline.preview.domain_certificate_type
        )
        self.assertEqual(
            effective.preview.generated_secret_env, baseline.preview.generated_secret_env
        )
