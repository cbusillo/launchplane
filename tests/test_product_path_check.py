import unittest

from fastapi import FastAPI
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.production_backup_authority import (
    ProductionBackupAuthorityReadModel,
)
from control_plane.contracts.release_review import ReleaseReviewStatus
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_path_check import (
    PathCheckInputs,
    Unread,
    build_product_path_check,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy, LocalOperatorPolicyRule
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_request, _local_operator_bearer_config
from tests.support.auth import _StubVerifier, _identity
from tests.support.profiles import _generic_site_profile_payload


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())


def _backup(state: str) -> ProductionBackupAuthorityReadModel:
    return ProductionBackupAuthorityReadModel.model_validate(
        {
            "product": "example-site",
            "context": "example-site",
            "instance": "prod",
            "promotion_action": "generic_web_prod_promotion.execute",
            "state": state,
            "ready": state == "ready",
            "summary": "Fixed summary.",
            "generated_at": "2026-10-02T00:00:00Z",
        }
    )


def _steps(check: object) -> dict[str, tuple[str, str, str]]:
    return {step.step_id: (step.state, step.code, step.fix) for step in getattr(check, "steps")}


class ProductPathCheckTests(unittest.TestCase):
    def test_promote_reports_every_blocker_at_once(self) -> None:
        # 2026-09-27: no usable grant (occurrence 3) and no backup policy
        # (occurrence 6) surfaced one at a time; one check names both.
        check = build_product_path_check(
            product="example-site",
            path="promote",
            inputs=PathCheckInputs(
                profile=_profile(),
                promotion_action="generic_web_prod_promotion.dispatch",
                promotion_allowed=False,
                release_review=ReleaseReviewStatus(required=True, approved=False),
                backup_authority=_backup("missing"),
                latest_promotion=None,
            ),
        )

        self.assertEqual((check.state, check.blocked_count), ("blocked", 3))
        self.assertEqual(
            _steps(check),
            {
                "profile_lane": ("clear", "lane_recorded", "none"),
                "promotion_grant": ("blocked", "caller_lacks_promotion_grant", "grant"),
                "client_acceptance": (
                    "blocked",
                    "client_acceptance_pending",
                    "client_acceptance",
                ),
                "backup_authority": ("blocked", "backup_missing", "owner_approval"),
                "previous_promotion": ("clear", "no_previous_promotion", "none"),
            },
        )

    def test_promote_is_clear_when_every_step_is(self) -> None:
        check = build_product_path_check(
            product="example-site",
            path="promote",
            inputs=PathCheckInputs(
                profile=_profile(),
                promotion_allowed=True,
                release_review=ReleaseReviewStatus(required=False, approved=True),
                backup_authority=_backup("ready"),
                latest_promotion=None,
            ),
        )

        self.assertEqual((check.state, check.blocked_count, check.unknown_count), ("clear", 0, 0))

    def test_an_unread_step_is_unknown_never_clear(self) -> None:
        check = build_product_path_check(
            product="example-site",
            path="promote",
            inputs=PathCheckInputs(
                profile=_profile(),
                promotion_allowed=True,
                release_review=Unread("release_review_unread"),
                backup_authority=_backup("ready"),
                latest_promotion=Unread("promotion_records_unread"),
            ),
        )

        self.assertEqual((check.state, check.unknown_count), ("unknown", 2))
        self.assertEqual(
            _steps(check)["client_acceptance"], ("unknown", "release_review_unread", "wait")
        )

    def test_testing_names_the_check_that_stopped_the_last_deploy(self) -> None:
        # 2026-10-02: CM testing failed three times before any code said why.
        for code, fix in (
            ("deploy_blocked.provider_only_keys", "by_hand"),
            ("deploy_blocked.provider_target_unreadable", "wait"),
            ("plan_not_ready.volume_authority_drift", "by_hand"),
            ("operation_authorization_revoked", "grant"),
            ("unexpected.click_exception", "code"),
        ):
            with self.subTest(code=code):
                check = build_product_path_check(
                    product="example-site",
                    path="testing",
                    inputs=PathCheckInputs(
                        profile=_profile(),
                        testing_hold_active=False,
                        testing_reconcile_plan={
                            "last_failed_operation_id": "operation-1",
                            "last_failed_error_code": code,
                            "last_failed_error_summary": "Fixed summary.",
                        },
                    ),
                )

                step = check.steps[-1]
                self.assertEqual((step.state, step.code, step.fix), ("blocked", code, fix))
                self.assertEqual(step.record_ids, ("operation-1",))

    def test_testing_hold_and_deployed_release(self) -> None:
        check = build_product_path_check(
            product="example-site",
            path="testing",
            inputs=PathCheckInputs(
                profile=_profile(),
                testing_hold_active=True,
                testing_reconcile_plan={"deployed_operation_id": "operation-2"},
            ),
        )

        self.assertEqual(
            _steps(check),
            {
                "profile_lane": ("clear", "lane_recorded", "none"),
                "testing_hold": ("blocked", "testing_hold_active", "wait"),
                "testing_deploy": ("clear", "already_deployed", "none"),
            },
        )

    def test_retired_product_is_blocked(self) -> None:
        payload = _generic_site_profile_payload()
        payload["lifecycle_state"] = "retired"
        payload["preview"] = {"enabled": False, "context": "example-site-preview"}
        check = build_product_path_check(
            product="example-site",
            path="testing",
            inputs=PathCheckInputs(
                profile=LaunchplaneProductProfileRecord.model_validate(payload),
            ),
        )

        self.assertEqual(check.steps[0].code, "product_not_active")


class ProductPathCheckHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_needs_product_read_and_answers_for_the_caller(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.db'}"
            )
            store.ensure_schema()
            store.write_product_profile_record(_profile())

            def app_with(actions: tuple[str, ...]) -> FastAPI:
                return create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy(
                        local_operators=(
                            LocalOperatorPolicyRule(
                                subjects=("local-owner-agent",),
                                token_labels=("local-owner-read",),
                                products=("example-site",),
                                contexts=("example-site",),
                                actions=actions,
                            ),
                        )
                    ),
                    bearer_identity_config=_local_operator_bearer_config(),
                    record_store_factory=lambda: store,
                )

            headers = {"Authorization": "Bearer local-operator-token"}
            route = "/v1/products/example-site/path-check?path=testing"
            allowed = await _asgi_request(
                app_with(("product_environment.read",)), "GET", route, headers=headers
            )
            denied = await _asgi_request(
                app_with(("deployment.read",)), "GET", route, headers=headers
            )
            missing = await _asgi_request(
                app_with(("product_environment.read",)),
                "GET",
                "/v1/products/no-such-product/path-check?path=testing",
                headers=headers,
            )
            store.close()

        self.assertEqual(allowed.status_code, 200, allowed.text)
        check = allowed.json()["check"]
        self.assertEqual(check["path"], "testing")
        self.assertEqual(
            [(step["step_id"], step["state"]) for step in check["steps"]],
            [
                ("profile_lane", "clear"),
                ("testing_hold", "clear"),
                ("testing_deploy", "unknown"),
            ],
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
