from typing import Any
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

        self.enterContext(patch.object(WebsiteIndexingExecution, "observe", native))

    def production_use(self) -> str:
        return self.store.read_product_profile_record(PRODUCT).production_use

    def accept_prelaunch(self) -> Any:
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

    def test_failed_state_compare_restores_protection_and_stops_forward_retry(self) -> None:
        from types import SimpleNamespace
        from control_plane.odoo_website_indexing import complete_first_live

        accepted = self.accept_prelaunch()
        (backup,) = self.advance()
        self.finish(backup)
        (promotion,) = self.advance()
        self.finish(promotion)
        self.set_prod(accepted.checklist.candidate.artifact_id)
        profile = self.store.read_product_profile_record(PRODUCT)
        with patch.object(
            self.store,
            "compare_and_write_product_profile_record",
            return_value=SimpleNamespace(status="conflict"),
        ):
            with self.assertRaisesRegex(ValueError, "restored"):
                complete_first_live(self.store, self.root, profile, accepted)
        self.assertEqual(self.production_use(), "prelaunch")
        self.assertTrue(self.protected)
        with self.assertRaisesRegex(ValueError, "reversed"):
            complete_first_live(self.store, self.root, profile, accepted)
        self.assertTrue(self.protected)
        view = read_client_release_run(store=self.store, profile=profile, decision=accepted)
        assert view is not None
        self.assertEqual(view.state, "stopped")

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
