from typing import Any
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from collections.abc import Callable
from unittest.mock import patch

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.odoo_website_indexing import WebsiteIndexingExecution
from control_plane.client_release import read_client_release_run
from tests import test_client_release as release

PRODUCT = release.PRODUCT


class PrelaunchReleaseTests(release.ClientReleaseFixture):
    def setUp(self) -> None:
        super().setUp()
        self.switch("promote")
        profile = self.store.read_product_profile_record(PRODUCT)
        self.store.write_product_profile_record(
            profile.model_copy(update={"production_use": "prelaunch"})
        )
        self.enterContext(
            patch(
                "control_plane.odoo_website_indexing.production_origin",
                return_value="https://example.test",
            )
        )
        self.protected = True
        test = self

        def native(
            execution: WebsiteIndexingExecution,
            *,
            reviewed: dict[str, Any] | None = None,
            before_mutation: Callable[[], None] | None = None,
        ) -> dict[str, Any]:
            protect = (execution.desired_state or test.production_use()) == "prelaunch"
            plan: dict[str, Any] = {
                "binding_sha256": "f" * 64,
                "database_uuid": "synthetic-database",
                "native": {
                    "website_id": 7,
                    "public_origin": "https://example.test",
                    "production_use": "prelaunch" if protect else "live",
                    "field": "custom_code_head",
                    "before_sha256": ("a" if test.protected else "b") * 64,
                    "after_sha256": ("a" if protect else "b") * 64,
                    "changed": test.protected != protect,
                    "managed_noindex_before": test.protected,
                    "managed_noindex_after": protect,
                },
            }
            result = {
                **plan,
                "plan_sha256": canonical_json_sha256(plan),
                "committed": reviewed is not None,
            }
            if reviewed is not None:
                if execution.require_authority is not None:
                    execution.require_authority()
                assert before_mutation is not None
                before_mutation()
                test.protected = protect
            return result

        self.native_observe = native
        self.enterContext(patch.object(WebsiteIndexingExecution, "observe", native))

    def production_use(self) -> str:
        return self.store.read_product_profile_record(PRODUCT).production_use

    def accept_prelaunch(self) -> ReleaseReviewDecisionRecord:
        from tests import test_release_review

        current = self.store.read_product_profile_record(PRODUCT)
        with patch.object(test_release_review, "profile", return_value=current):
            return super().accept()

    def test_accepted_candidate_becomes_live_only_after_last_passing_release_step(self) -> None:
        accepted = self.accept_prelaunch()
        self.assertEqual(accepted.checklist.first_live_origin, "https://example.test")
        (backup,) = self.advance()
        self.assertTrue(self.protected)
        self.finish(backup)
        (promotion,) = self.advance()
        self.assertEqual(self.production_use(), "prelaunch")
        self.assertTrue(self.protected)
        self.finish(promotion)
        self.set_prod(accepted.checklist.candidate.artifact_id)
        self.advance()
        self.assertEqual(self.production_use(), "live")
        self.assertFalse(self.protected)

    def test_failed_release_keeps_prelaunch_protection(self) -> None:
        self.accept_prelaunch()
        (backup,) = self.advance()
        self.finish(backup)
        (promotion,) = self.advance()
        self.finish(promotion, "fail")
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.production_use(), "prelaunch")
        self.assertTrue(self.protected)

    def test_held_release_does_not_start_or_remove_protection(self) -> None:
        self.accept_prelaunch()
        self.switch("held")
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.production_use(), "prelaunch")
        self.assertTrue(self.protected)

    def test_drill_rollback_remains_prelaunch_until_final_promotion_passes(self) -> None:
        self.switch("promote_with_rollback_drill")
        accepted = self.accept_prelaunch()
        for artifact in (
            None,
            accepted.checklist.candidate.artifact_id,
            accepted.checklist.production.artifact_id,
            None,
        ):
            (operation,) = self.advance()
            self.finish(operation)
            if artifact is not None:
                self.set_prod(artifact)
            self.assertEqual(self.production_use(), "prelaunch")
            self.assertTrue(self.protected)
        (repromotion,) = self.advance()
        self.finish(repromotion, "fail")
        self.assertEqual(self.advance(), ())
        self.assertEqual(self.production_use(), "prelaunch")
        self.assertTrue(self.protected)

    def complete_deployment(self) -> Any:
        accepted = self.accept_prelaunch()
        (backup,) = self.advance()
        self.finish(backup)
        (promotion,) = self.advance()
        self.finish(promotion)
        self.set_prod(accepted.checklist.candidate.artifact_id)
        return accepted

    def test_unrelated_profile_edit_retries_atomic_state_completion(self) -> None:
        from control_plane.odoo_website_indexing import complete_first_live

        accepted = self.complete_deployment()
        compare = self.store.compare_and_write_product_profile_record
        calls = 0

        def edit(**kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                edited_profile = self.store.read_product_profile_record(PRODUCT)
                self.store.write_product_profile_record(
                    edited_profile.model_copy(update={"display_name": "Renamed"})
                )
            return compare(**kwargs)

        with patch.object(self.store, "compare_and_write_product_profile_record", side_effect=edit):
            complete_first_live(
                self.store, self.root, self.store.read_product_profile_record(PRODUCT), accepted
            )
        self.assertEqual(calls, 2)
        self.assertEqual(self.production_use(), "live")
        self.assertFalse(self.protected)
        profile = self.store.read_product_profile_record(PRODUCT)
        self.assertEqual(profile.display_name, "Renamed")
        self.store.write_product_profile_record(
            profile.model_copy(update={"production_use": "prelaunch"})
        )
        self.protected = True
        complete_first_live(
            self.store, self.root, self.store.read_product_profile_record(PRODUCT), accepted
        )
        self.assertEqual(self.production_use(), "prelaunch")
        self.assertTrue(self.protected)

    def test_hold_after_native_commit_restores_protection_and_stops_forward_retry(self) -> None:
        from control_plane.odoo_website_indexing import complete_first_live

        accepted = self.complete_deployment()
        compare = self.store.compare_and_write_product_profile_record

        def hold(**kwargs: Any) -> Any:
            self.switch("held")
            return compare(**kwargs)

        with patch.object(self.store, "compare_and_write_product_profile_record", side_effect=hold):
            complete_first_live(
                self.store, self.root, self.store.read_product_profile_record(PRODUCT), accepted
            )
        self.assertEqual(self.production_use(), "prelaunch")
        self.assertTrue(self.protected)
        complete_first_live(
            self.store, self.root, self.store.read_product_profile_record(PRODUCT), accepted
        )
        view = read_client_release_run(
            store=self.store,
            profile=self.store.read_product_profile_record(PRODUCT),
            decision=accepted,
        )
        assert view is not None
        self.assertEqual(view.state, "stopped")

    def test_interrupted_state_write_requires_fresh_native_after_image(self) -> None:
        from control_plane.odoo_website_indexing import complete_first_live, WebsiteIndexingUnknown

        accepted = self.complete_deployment()
        with patch.object(
            self.store,
            "compare_and_write_product_profile_record",
            side_effect=RuntimeError("interrupted"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                complete_first_live(
                    self.store, self.root, self.store.read_product_profile_record(PRODUCT), accepted
                )
        self.assertFalse(self.protected)
        self.assertEqual(self.production_use(), "prelaunch")
        self.protected = True
        with self.assertRaisesRegex(WebsiteIndexingUnknown, "after-image"):
            complete_first_live(
                self.store, self.root, self.store.read_product_profile_record(PRODUCT), accepted
            )
        self.assertEqual(self.production_use(), "prelaunch")
        self.assertTrue(self.protected)

    def test_worker_restores_prelaunch_after_interrupted_commit_followed_by_hold(self) -> None:
        from control_plane.odoo_website_indexing import complete_first_live

        accepted = self.complete_deployment()
        with patch.object(
            self.store,
            "compare_and_write_product_profile_record",
            side_effect=RuntimeError("interrupted"),
        ):
            with self.assertRaises(RuntimeError):
                complete_first_live(
                    self.store, self.root, self.store.read_product_profile_record(PRODUCT), accepted
                )
        self.assertFalse(self.protected)
        self.switch("held")
        self.advance()
        self.assertTrue(self.protected)
        self.assertEqual(self.production_use(), "prelaunch")

    def test_interrupted_inverse_receipt_is_adopted_without_another_write(self) -> None:
        from control_plane.odoo_website_indexing import (
            complete_first_live,
            FIRST_LIVE_SCOPE,
            FIRST_LIVE_ROUTE,
        )

        accepted = self.complete_deployment()
        observe = self.native_observe
        writes = []

        def interrupted(execution: WebsiteIndexingExecution, **kwargs: Any) -> dict[str, Any]:
            result = observe(execution, **kwargs)
            if kwargs.get("reviewed") is not None:
                writes.append(execution.desired_state)
                if execution.desired_state == "live":
                    self.switch("held")
                else:
                    raise RuntimeError("lost inverse receipt")
            return result

        with patch.object(WebsiteIndexingExecution, "observe", interrupted):
            with self.assertRaisesRegex(RuntimeError, "lost inverse receipt"):
                complete_first_live(
                    self.store, self.root, self.store.read_product_profile_record(PRODUCT), accepted
                )
        self.assertTrue(self.protected)
        self.assertEqual(writes, ["live", "prelaunch"])
        self.advance()
        inverse = self.store.read_idempotency_record(
            scope=FIRST_LIVE_SCOPE,
            route_path=FIRST_LIVE_ROUTE,
            idempotency_key=accepted.record_id + ":inverse",
        )
        assert inverse is not None
        self.assertEqual(inverse.state, "completed")
        self.assertEqual(self.production_use(), "prelaunch")
        self.assertTrue(self.protected)

    def test_release_payload_reasserts_recorded_state_after_restore(self) -> None:
        import base64
        import json
        from control_plane.workflows.odoo_post_deploy import (
            execute_odoo_post_deploy,
            OdooPostDeployRequest,
        )
        from control_plane.dokploy import DokployTargetDefinition
        from tests.test_odoo_post_deploy import _module_update_evidence as module_update_evidence

        def maintenance(**kwargs: Any) -> dict[str, str]:
            payload = json.loads(
                base64.b64decode(
                    kwargs["workflow_environment_overrides"]["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"]
                )
            )
            indexing = payload["website_indexing"]
            self.assertEqual(
                indexing,
                {
                    "website_id": 7,
                    "public_origin": "https://example.test",
                    "production_use": self.production_use(),
                },
            )
            self.protected = indexing["production_use"] == "prelaunch"
            return module_update_evidence(
                website_indexing_production_use=indexing["production_use"]
            )

        with (
            patch(
                "control_plane.workflows.odoo_post_deploy._resolve_compose_target_definition",
                return_value=DokployTargetDefinition(
                    context=release.CONTEXT, instance="prod", public_hosts=("example.test",)
                ),
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                return_value=("synthetic", "synthetic"),
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                side_effect=maintenance,
            ),
        ):
            for state in ("prelaunch", "live"):
                profile = self.store.read_product_profile_record(PRODUCT)
                self.store.write_product_profile_record(
                    profile.model_copy(update={"production_use": state})
                )
                self.protected = state != "prelaunch"
                result = execute_odoo_post_deploy(
                    control_plane_root=self.root,
                    record_store=self.store,
                    request=OdooPostDeployRequest(context=release.CONTEXT, instance="prod"),
                    artifact_id="artifact-prod",
                    release_operation=True,
                    rollback=True,
                )
                self.assertEqual(result.post_deploy_status, "pass", result.error_message)
                self.assertEqual(self.protected, state == "prelaunch")

    def test_completed_deployment_waits_for_indexing_before_reporting_success(self) -> None:
        accepted = self.accept_prelaunch()
        (backup,) = self.advance()
        self.finish(backup)
        (promotion,) = self.advance()
        self.finish(promotion)
        self.set_prod(accepted.checklist.candidate.artifact_id)
        view = read_client_release_run(
            store=self.store,
            profile=self.store.read_product_profile_record(PRODUCT),
            decision=accepted,
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.state, "waiting")

    def test_acceptance_without_first_live_binding_never_starts_a_prelaunch_release(self) -> None:
        from control_plane.release_review import checklist_digest

        accepted = self.accept_prelaunch()
        checklist = accepted.checklist.model_copy(update={"first_live_origin": ""})
        self.store.write_release_review_decision_record(
            accepted.model_copy(
                update={"checklist": checklist, "checklist_digest": checklist_digest(checklist)}
            )
        )
        self.assertEqual(self.advance(), ())
        self.assertTrue(self.protected)
