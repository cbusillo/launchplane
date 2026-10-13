"""Saved cleanup intent cannot delete a reopened or replaced preview."""

import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_lifecycle_cleanup_record import PreviewLifecycleCleanupRecord
from unittest.mock import patch

from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
)
from control_plane import generic_web_preview_http as preview_http
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.preview_lifecycle_cleanup_routes import (
    PreviewLifecycleSweepEnvelope,
    build_preview_lifecycle_sweep,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDestroyResult,
    GenericWebPreviewRefreshRequest,
    GenericWebPreviewRefreshResult,
    GenericWebPreviewInventoryItem,
    GenericWebPreviewInventoryResult,
)
from control_plane.workflows.preview_desired_state import build_preview_desired_state_record
from control_plane.workflows.preview_lifecycle_cleanup import build_preview_lifecycle_cleanup_record
from tests.http_app_test_support import _asgi_request
from tests.test_generic_web_preview import _profile
from tests import test_http_app_preview as http_support

WORKFLOW = "control_plane.workflows.preview_lifecycle_cleanup."


def saved_fixture(
    root: Path, profile: LaunchplaneProductProfileRecord
) -> tuple[FilesystemRecordStore, PreviewRecord, PreviewLifecyclePlanRecord]:
    store = FilesystemRecordStore(root / "state")
    store.write_product_profile_record(profile)
    preview = PreviewRecord(
        preview_id="preview-fixture",
        context=profile.preview.context,
        anchor_repo=profile.repository.partition("/")[2],
        anchor_pr_number=42,
        anchor_pr_url="https://example.test/pull/42",
        preview_label="fixture/pr-42",
        canonical_url="https://pr-42.example.test",
        state="active",
        created_at="2026-10-01T00:00:00Z",
        updated_at="2026-10-01T00:00:00Z",
        eligible_at="2026-10-01T00:00:00Z",
        serving_generation_id="old-generation",
    )
    store.write_preview_record(preview)
    plan = PreviewLifecyclePlanRecord(
        plan_id="old-cleanup",
        product=profile.product,
        context=profile.preview.context,
        planned_at="2026-10-01T00:01:00Z",
        source="test",
        status="pass",
        inventory_scan_id="inventory-fixture",
        actual_slugs=("pr-42",),
        orphaned_slugs=("pr-42",),
    )
    store.write_preview_lifecycle_plan_record(plan)
    return store, preview, plan


def desired(
    profile: LaunchplaneProductProfileRecord, *, reopened: bool = False, error: str = ""
) -> PreviewDesiredStateRecord:
    return build_preview_desired_state_record(
        product=profile.product,
        context=profile.preview.context,
        source="test",
        discovered_at="2026-10-13T00:00:00Z",
        repository=profile.repository,
        anchor_repo=profile.repository.partition("/")[2],
        preview_slug_prefix="pr-",
        desired_previews=(
            PreviewLifecycleDesiredPreview(
                preview_slug="pr-42",
                anchor_pr_number=42,
                anchor_repo=profile.repository.partition("/")[2],
                head_sha="b" * 40,
            ),
        )
        if reopened
        else (),
        error_message=error,
    )


def destroyed(profile: LaunchplaneProductProfileRecord) -> GenericWebPreviewDestroyResult:
    return GenericWebPreviewDestroyResult(
        destroy_status="pass",
        destroy_started_at="2026-10-13T00:01:00Z",
        destroy_finished_at="2026-10-13T00:01:01Z",
        product=profile.product,
        context=profile.preview.context,
        preview_slug="pr-42",
        application_name="fixture-pr-42",
        application_id="target-fixture",
    )


def cleanup(
    root: Path,
    store: FilesystemRecordStore,
    plan: PreviewLifecyclePlanRecord,
    profile: LaunchplaneProductProfileRecord,
) -> PreviewLifecycleCleanupRecord:
    return build_preview_lifecycle_cleanup_record(
        plan=plan,
        requested_at="2026-10-13T00:01:00Z",
        source="test",
        apply=True,
        destroy_reason="orphan",
        control_plane_root=root,
        record_store=store,
        timeout_seconds=300,
        driver_id="generic-web",
        profile=profile,
    )


class PreviewCleanupSafetyTests(unittest.TestCase):
    def test_saved_intent_refuses_reopened_unknown_and_newer_lifecycles(self) -> None:
        profile = _profile().model_copy(
            update={
                "preview": _profile().preview.model_copy(update={"slug_template": "pr-{number}"}),
            }
        )
        for scenario in (
            "reopened",
            "unknown",
            "replacement",
            "same_second",
            "invalid_time",
            "during_read",
        ):
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                root = Path(directory)
                store, preview, plan = saved_fixture(root, profile)
                if scenario in {"replacement", "same_second", "invalid_time"}:
                    updated = {
                        "replacement": "2026-10-09T00:00:00Z",
                        "same_second": plan.planned_at,
                        "invalid_time": "unknown",
                    }[scenario]
                    preview = preview.model_copy(
                        update={
                            "updated_at": updated,
                            "serving_generation_id": "replacement-generation",
                        }
                    )
                    store.write_preview_record(preview)

                def reread(**_kwargs: Any) -> PreviewDesiredStateRecord:
                    if scenario == "during_read":
                        store.write_preview_record(
                            preview.model_copy(
                                update={
                                    "serving_generation_id": "replacement-generation",
                                    "updated_at": "2026-10-09T00:00:00Z",
                                }
                            )
                        )
                    return desired(
                        profile,
                        reopened=scenario == "reopened",
                        error="incomplete PR inventory" if scenario == "unknown" else "",
                    )

                with (
                    patch(
                        WORKFLOW + "discover_generic_web_preview_desired_state", side_effect=reread
                    ),
                    patch(
                        WORKFLOW + "_execute_generic_web_preview_destroy_unserialized"
                    ) as destroy,
                    patch(WORKFLOW + "apply_generic_web_preview_driver_destroy") as extension,
                ):
                    result = cleanup(root, store, plan, profile)
                self.assertEqual(result.status, "blocked")
                self.assertEqual(result.destroyed_slugs, ())
                destroy.assert_not_called()
                extension.assert_not_called()
                self.assertEqual(store.read_preview_record(preview.preview_id).state, "active")

    def test_fresh_orphan_cleanup_serializes_validation_effect_and_terminal_evidence(self) -> None:
        profile = _profile().model_copy(
            update={
                "preview": _profile().preview.model_copy(update={"slug_template": "pr-{number}"}),
            }
        )
        held: list[str] = []

        class SerializedStore(FilesystemRecordStore):
            @contextmanager
            def serialize_preview_refresh(
                self, *, preview_id: str, **_kwargs: object
            ) -> Iterator[None]:
                held.append(preview_id)
                try:
                    yield
                finally:
                    held.remove(preview_id)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, preview, plan = saved_fixture(root, profile)
            store = SerializedStore(root / "state")

            def effect(**_kwargs: Any) -> GenericWebPreviewDestroyResult:
                self.assertIn(f"{profile.product}:{profile.preview.context}:pr-42", held)
                return destroyed(profile)

            with (
                patch(
                    WORKFLOW + "discover_generic_web_preview_desired_state",
                    return_value=desired(profile),
                ),
                patch(
                    WORKFLOW + "_execute_generic_web_preview_destroy_unserialized",
                    side_effect=effect,
                ) as destroy,
            ):
                result = cleanup(root, store, plan, profile)
            self.assertEqual(result.status, "pass")
            self.assertEqual(result.destroyed_slugs, plan.orphaned_slugs)
            self.assertEqual(store.read_preview_record(preview.preview_id).state, "destroyed")
            destroy.assert_called_once()
            self.assertEqual(held, [])

    def test_cleanup_waits_for_refresh_evidence_before_checking_replacement(self) -> None:
        profile = _profile().model_copy(
            update={
                "preview": _profile().preview.model_copy(update={"slug_template": "pr-{number}"}),
            }
        )
        writing = threading.Event()
        release = threading.Event()
        cleanup_progress = threading.Event()
        locks: dict[str, threading.RLock] = {}

        class SerializedStore(FilesystemRecordStore):
            @contextmanager
            def serialize_preview_refresh(
                self, *, preview_id: str, **_kwargs: Any
            ) -> Iterator[None]:
                lock = locks.setdefault(preview_id, threading.RLock())
                if not lock.acquire(blocking=False):
                    if threading.current_thread().name.startswith("cleanup"):
                        cleanup_progress.set()
                    lock.acquire()
                try:
                    yield
                finally:
                    lock.release()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, preview, plan = saved_fixture(root, profile)
            store = SerializedStore(root / "state")
            replacement = preview.model_copy(
                update={
                    "updated_at": (
                        datetime.fromisoformat(plan.planned_at) + timedelta(seconds=1)
                    ).isoformat(),
                    "serving_generation_id": "replacement-generation",
                }
            )
            result = GenericWebPreviewRefreshResult(
                refresh_status="pass",
                refresh_started_at=plan.planned_at,
                refresh_finished_at=replacement.updated_at,
                product=profile.product,
                context=profile.preview.context,
                preview_slug="pr-42",
                application_name="fixture-pr-42",
                preview_url=preview.canonical_url,
            )

            def write_evidence(**_kwargs: Any) -> dict[str, object]:
                writing.set()
                if not release.wait(5):
                    raise TimeoutError("fixture did not release evidence writer")
                store.write_preview_record(replacement)
                return {}

            with (
                patch(
                    "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                    return_value=result,
                ),
                patch(
                    "control_plane.generic_web_preview_http._apply_generic_web_preview_refresh_records",
                    side_effect=write_evidence,
                ),
                patch(
                    WORKFLOW + "discover_generic_web_preview_desired_state",
                    return_value=desired(profile),
                ),
                patch(
                    WORKFLOW + "_execute_generic_web_preview_destroy_unserialized",
                    return_value=destroyed(profile),
                ) as destroy,
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="refresh") as refresh_worker,
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="cleanup") as cleanup_worker,
            ):
                refreshing = refresh_worker.submit(
                    preview_http.apply_generic_web_preview_refresh_result,
                    control_plane_root=root,
                    record_store=store,
                    profile=profile,
                    request=preview_http.GenericWebPreviewRefreshEnvelope(
                        product=profile.product,
                        refresh=GenericWebPreviewRefreshRequest(
                            product=profile.product,
                            anchor_pr_number=42,
                            preview_slug="pr-42",
                            preview_url=preview.canonical_url,
                            image_reference="ghcr.io/example/fixture:sha-fixture",
                        ),
                    ),
                )
                try:
                    self.assertTrue(writing.wait(5))
                    cleaning = cleanup_worker.submit(cleanup, root, store, plan, profile)
                    cleaning.add_done_callback(lambda _future: cleanup_progress.set())
                    self.assertTrue(cleanup_progress.wait(5))
                    destroy.assert_not_called()
                finally:
                    release.set()
                refreshing.result(timeout=5)
                self.assertEqual(cleaning.result(timeout=5).status, "blocked")
                destroy.assert_not_called()
                self.assertEqual(store.read_preview_record(preview.preview_id), replacement)

    def test_sweep_rechecks_reopen_and_generation_movement_after_discovery(self) -> None:
        profile = _profile().model_copy(
            update={
                "preview": _profile().preview.model_copy(update={"slug_template": "pr-{number}"}),
            }
        )
        for scenario in ("reopened", "replacement", "orphan"):
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                root = Path(directory)
                store, preview, _ = saved_fixture(root, profile)

                def apply_discovery(**_kwargs: Any) -> PreviewDesiredStateRecord:
                    if scenario == "replacement":
                        replacement_at = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
                        store.write_preview_record(
                            preview.model_copy(
                                update={
                                    "updated_at": replacement_at,
                                    "serving_generation_id": "replacement",
                                }
                            )
                        )
                    return desired(profile, reopened=scenario == "reopened")

                with (
                    patch(
                        "control_plane.preview_lifecycle_cleanup_routes.execute_generic_web_preview_inventory",
                        return_value=GenericWebPreviewInventoryResult(
                            product=profile.product,
                            context=profile.preview.context,
                            source="test",
                            app_name_prefix="fixture",
                            previews=(
                                GenericWebPreviewInventoryItem(
                                    applicationId="fixture",
                                    applicationName="fixture-pr-42",
                                    previewSlug="pr-42",
                                ),
                            ),
                        ),
                    ),
                    patch(
                        "control_plane.preview_lifecycle_cleanup_routes.discover_generic_web_preview_desired_state",
                        return_value=desired(profile),
                    ),
                    patch(
                        WORKFLOW + "discover_generic_web_preview_desired_state",
                        side_effect=apply_discovery,
                    ),
                    patch(
                        WORKFLOW + "_execute_generic_web_preview_destroy_unserialized",
                        return_value=destroyed(profile),
                    ) as destroy,
                ):
                    result = build_preview_lifecycle_sweep(
                        control_plane_root=root,
                        record_store=store,
                        request=PreviewLifecycleSweepEnvelope(
                            apply=True, source="test", destroy_reason="orphan"
                        ),
                        requested_profiles=(profile,),
                    )
                if scenario == "orphan":
                    self.assertEqual(result["status"], "pass")
                    destroy.assert_called_once()
                    self.assertEqual(
                        store.read_preview_record(preview.preview_id).state, "destroyed"
                    )
                else:
                    self.assertEqual(result["status"], "fail")
                    destroy.assert_not_called()
                    self.assertEqual(store.read_preview_record(preview.preview_id).state, "active")


class SavedCleanupHttpSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_plan_apply_blocks_reopen_and_replacement_then_allows_fresh_orphan(
        self,
    ) -> None:
        helpers = http_support.FastApiPreviewLifecycleCleanupTests()
        base = _profile()
        profile = base.model_copy(
            update={
                "product": "verireel",
                "repository": "every/verireel",
                "preview": base.preview.model_copy(
                    update={"context": "verireel-testing", "slug_template": "pr-{number}"}
                ),
            }
        )
        for scenario in ("reopened", "replacement", "fresh_orphan"):
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                root = Path(directory)
                store, preview, plan = saved_fixture(root, profile)
                preview = preview.model_copy(
                    update={
                        "updated_at": "2026-10-09T00:00:00Z",
                        "serving_generation_id": "replacement",
                    }
                )
                store.write_preview_record(preview)
                if scenario == "fresh_orphan":
                    plan = plan.model_copy(
                        update={"plan_id": "fresh-cleanup", "planned_at": "2026-10-09T00:01:00Z"}
                    )
                    store.write_preview_lifecycle_plan_record(plan)
                if scenario == "reopened":
                    store.write_preview_record(
                        preview.model_copy(update={"updated_at": "2026-10-01T00:00:00Z"})
                    )
                app = helpers._cleanup_app(
                    root=root,
                    state_dir=root / "state",
                    policy=helpers._cleanup_policy(["preview_lifecycle.cleanup"]),
                )
                with (
                    patch(
                        WORKFLOW + "discover_generic_web_preview_desired_state",
                        return_value=desired(profile, reopened=scenario == "reopened"),
                    ),
                    patch(
                        WORKFLOW + "_execute_generic_web_preview_destroy_unserialized",
                        return_value=destroyed(profile),
                    ) as destroy,
                ):
                    response = await _asgi_request(
                        app,
                        "POST",
                        "/v1/previews/lifecycle-cleanup",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": f"cleanup:{scenario}",
                        },
                        payload={
                            "product": profile.product,
                            "context": profile.preview.context,
                            "plan_id": plan.plan_id,
                            "source": "test",
                            "apply": True,
                        },
                    )
                self.assertEqual(response.status_code, 202)
                if scenario == "fresh_orphan":
                    self.assertEqual(response.json()["result"]["status"], "pass")
                    destroy.assert_called_once()
                    self.assertEqual(
                        store.read_preview_record(preview.preview_id).state, "destroyed"
                    )
                else:
                    self.assertEqual(response.json()["result"]["status"], "blocked")
                    destroy.assert_not_called()
                    self.assertEqual(store.read_preview_record(preview.preview_id).state, "active")
