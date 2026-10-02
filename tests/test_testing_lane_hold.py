import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from httpx2 import Response
from pydantic import ValidationError

from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetPolicies,
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_lane_hold import (
    TESTING_HOLD_APPLY_ROUTE,
    TESTING_HOLD_ROUTE,
    TestingHoldApplyRequest,
    TestingHoldPlan,
    TestingHoldRefusal,
    TestingHoldStale,
    apply_testing_hold_plan,
    build_testing_hold_plan,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import request as http_request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import sqlite_database_url

_PRODUCT = "odoo-tenant-cm"
_CONTEXT = "cm"
_TIMESTAMP = "2026-09-30T12:00:00Z"
_HOLD_REASON = "CM staff are testing the new repair intake flow."
_ALLOWANCE = DokployTargetIntegrationAllowance(
    integration="repairshopr", kind="pre_live", reason="CM prod is not live yet."
)


def _seed_target(
    store: FilesystemRecordStore | PostgresRecordStore,
    *,
    instance: str = "testing",
    hold: DokployTargetStaffTestingHold | None = None,
) -> None:
    store.write_dokploy_target_record(
        DokployTargetRecord(
            context=_CONTEXT,
            instance=instance,
            policies=DokployTargetPolicies(
                integration_allowances=(_ALLOWANCE,), staff_testing_hold=hold
            ),
            updated_at=_TIMESTAMP,
        )
    )


def _read_hold(
    store: FilesystemRecordStore | PostgresRecordStore,
) -> DokployTargetStaffTestingHold | None:
    return store.read_dokploy_target_record(
        context_name=_CONTEXT, instance_name="testing"
    ).policies.staff_testing_hold


def _payload(
    *,
    hold: bool = True,
    reason: str = _HOLD_REASON,
    instance: str = "testing",
    mode: str = "dry-run",
    reviewed_plan_sha256: str = "",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "product": _PRODUCT,
        "context": _CONTEXT,
        "instance": instance,
        "mode": mode,
        "hold": hold,
        "reason": reason,
    }
    if reviewed_plan_sha256:
        payload["reviewed_plan_sha256"] = reviewed_plan_sha256
    return payload


def _request(**kwargs: object) -> TestingHoldApplyRequest:
    return TestingHoldApplyRequest.model_validate(_payload(**kwargs))  # type: ignore[arg-type]


def _apply(
    store: FilesystemRecordStore | PostgresRecordStore, *, actor: str = "operator", **kwargs: object
) -> TestingHoldPlan:
    plan, _ = build_testing_hold_plan(record_store=store, request=_request(**kwargs), actor=actor)
    return apply_testing_hold_plan(
        record_store=store,
        request=_request(mode="apply", reviewed_plan_sha256=plan.plan_sha256, **kwargs),
        actor=actor,
    )


def _postgres_store(root: Path) -> PostgresRecordStore:
    store = PostgresRecordStore(database_url=sqlite_database_url(root / "launchplane.sqlite3"))
    store.ensure_schema()
    return store


class StaffTestingHoldContractTests(unittest.TestCase):
    def test_hold_requires_a_reason_and_strips_values(self) -> None:
        hold = DokployTargetStaffTestingHold(
            reason=f"  {_HOLD_REASON} ", recorded_by=" operator ", recorded_at=f" {_TIMESTAMP}"
        )

        self.assertEqual(
            (hold.reason, hold.recorded_by, hold.recorded_at),
            (_HOLD_REASON, "operator", _TIMESTAMP),
        )
        for reason in ("", "   "):
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                DokployTargetStaffTestingHold(reason=reason)
        with self.assertRaises(ValidationError):
            DokployTargetStaffTestingHold.model_validate({"reason": "r", "until": _TIMESTAMP})

    def test_records_without_a_hold_still_load(self) -> None:
        record = DokployTargetRecord.model_validate(
            {"context": _CONTEXT, "instance": "testing", "updated_at": _TIMESTAMP}
        )

        self.assertIsNone(record.policies.staff_testing_hold)


class TestingHoldPlanTests(unittest.TestCase):
    def test_dry_run_plans_the_hold_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store)

            plan, _ = build_testing_hold_plan(
                record_store=store, request=_request(), actor="operator"
            )
            again, _ = build_testing_hold_plan(
                record_store=store, request=_request(), actor="someone-else"
            )
            stored = _read_hold(store)

        self.assertEqual((plan.action, plan.changed), ("set", True))
        self.assertEqual(plan.plan_sha256, again.plan_sha256)
        self.assertIsNone(stored)

    def test_apply_sets_the_hold_and_keeps_other_policies(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store)

            applied = _apply(store)
            stored = store.read_dokploy_target_record(
                context_name=_CONTEXT, instance_name="testing"
            )

        assert stored.policies.staff_testing_hold is not None
        self.assertEqual(stored.policies.staff_testing_hold.reason, _HOLD_REASON)
        self.assertEqual(stored.policies.staff_testing_hold.recorded_by, "operator")
        self.assertEqual(stored.policies.integration_allowances, (_ALLOWANCE,))
        self.assertEqual(stored.source_label, "service:testing-hold")
        self.assertTrue(applied.read_back_matches)

    def test_unchanged_hold_keeps_its_recorder(self) -> None:
        existing = DokployTargetStaffTestingHold(
            reason=_HOLD_REASON, recorded_by="first-operator", recorded_at=_TIMESTAMP
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store, hold=existing)

            unchanged, _ = build_testing_hold_plan(
                record_store=store, request=_request(), actor="second-operator"
            )
            reworded, _ = build_testing_hold_plan(
                record_store=store,
                request=_request(reason="Staff are testing checkout."),
                actor="second-operator",
            )

        self.assertEqual((unchanged.action, unchanged.after), ("unchanged", existing))
        self.assertEqual(reworded.action, "update")
        assert reworded.after is not None
        self.assertEqual(reworded.after.recorded_by, "second-operator")

    def test_apply_refuses_a_stale_digest_and_a_target_changed_after_review(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store)
            plan, _ = build_testing_hold_plan(
                record_store=store, request=_request(), actor="operator"
            )
            with self.assertRaises(TestingHoldStale):
                apply_testing_hold_plan(
                    record_store=store,
                    request=_request(mode="apply", reviewed_plan_sha256="f" * 64),
                    actor="operator",
                )
            current = store.read_dokploy_target_record(
                context_name=_CONTEXT, instance_name="testing"
            )
            store.write_dokploy_target_record(
                current.model_copy(update={"domains": ("cm-testing.example.com",)})
            )
            with self.assertRaises(TestingHoldStale):
                apply_testing_hold_plan(
                    record_store=store,
                    request=_request(mode="apply", reviewed_plan_sha256=plan.plan_sha256),
                    actor="operator",
                )
            stored = _read_hold(store)

        self.assertIsNone(stored)

    def test_retried_apply_after_a_lost_receipt_reports_already_applied(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store)
            plan, _ = build_testing_hold_plan(
                record_store=store, request=_request(), actor="operator"
            )
            apply_request = _request(mode="apply", reviewed_plan_sha256=plan.plan_sha256)
            first = apply_testing_hold_plan(
                record_store=store, request=apply_request, actor="operator"
            )
            retried = apply_testing_hold_plan(
                record_store=store, request=apply_request, actor="operator"
            )

        self.assertTrue(first.applied)
        self.assertEqual((retried.applied, retried.changed), (False, False))
        self.assertTrue(retried.read_back_matches)

    def test_refuses_other_lanes_and_a_missing_target_record(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store, instance="prod")
            with self.assertRaises(TestingHoldRefusal) as not_testing:
                build_testing_hold_plan(
                    record_store=store, request=_request(instance="prod"), actor="operator"
                )
            with self.assertRaises(TestingHoldRefusal) as missing:
                build_testing_hold_plan(record_store=store, request=_request(), actor="operator")

        self.assertEqual(not_testing.exception.code, "not_testing_lane")
        self.assertEqual(missing.exception.code, "target_record_missing")

    def test_request_requires_a_reason_and_a_digest_to_apply(self) -> None:
        for payload in (
            _payload(reason=" "),
            _payload(mode="apply"),
            {**_payload(), "until": _TIMESTAMP},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                TestingHoldApplyRequest.model_validate(payload)

    def test_lifting_the_hold_requests_a_testing_reconcile(self) -> None:
        with TemporaryDirectory() as directory:
            store = _postgres_store(Path(directory))
            self.addCleanup(store.close)
            _seed_target(
                store,
                hold=DokployTargetStaffTestingHold(reason=_HOLD_REASON, recorded_by="operator"),
            )

            lifted = _apply(store, hold=False, reason="Staff testing finished.")
            stored = _read_hold(store)
            requests = store.list_product_reconcile_requests()

        self.assertEqual(lifted.action, "clear")
        self.assertTrue(lifted.reconcile_requested)
        self.assertIsNone(stored)
        self.assertEqual(
            [(request.target_key, request.state) for request in requests],
            [(f"{_PRODUCT}:testing", "pending")],
        )

    def test_setting_the_hold_requests_no_reconcile(self) -> None:
        with TemporaryDirectory() as directory:
            store = _postgres_store(Path(directory))
            self.addCleanup(store.close)
            _seed_target(store)

            held = _apply(store)
            requests = store.list_product_reconcile_requests()

        self.assertFalse(held.reconcile_requested)
        self.assertEqual(requests, ())


class TestingHoldRouteTests(unittest.IsolatedAsyncioTestCase):
    _WORKFLOW_REF = "cbusillo/launchplane/.github/workflows/operator.yml@refs/heads/main"

    def _identity(self) -> GitHubActionsIdentity:
        return _identity(
            repository="cbusillo/launchplane",
            workflow_ref=self._WORKFLOW_REF,
            event_name="workflow_dispatch",
        )

    def _app(self, store: PostgresRecordStore, root: Path, *actions: str) -> FastAPI:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [self._WORKFLOW_REF],
                        "event_names": ["workflow_dispatch"],
                        "products": [_PRODUCT],
                        "contexts": [_CONTEXT],
                        "actions": list(actions),
                    }
                ]
            }
        )
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=policy,
            record_store_factory=lambda: store,
            control_plane_root_path=root,
        )

    def _store(self, root: Path) -> PostgresRecordStore:
        store = _postgres_store(root)
        self.addCleanup(store.close)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload_with_prod_lane())
        )
        _seed_target(store)
        _seed_target(store, instance="prod")
        return store

    @staticmethod
    async def _post(
        app: FastAPI, payload: dict[str, object], *, idempotency_key: str = ""
    ) -> Response:
        headers = {"Authorization": "Bearer valid-token"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await http_request(
            app, "POST", TESTING_HOLD_APPLY_ROUTE, headers=headers, payload=payload
        )

    @staticmethod
    async def _get(app: FastAPI, instance: str = "testing") -> Response:
        return await http_request(
            app,
            "GET",
            f"{TESTING_HOLD_ROUTE}?product={_PRODUCT}&context={_CONTEXT}&instance={instance}",
            headers={"Authorization": "Bearer valid-token"},
        )

    async def test_hold_and_lift_through_the_routes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            app = self._app(store, root, "product_config.plan", "product_config.apply")

            dry_run = await self._post(app, _payload())
            digest = dry_run.json()["result"]["plan_sha256"]
            missing_key = await self._post(app, _payload(mode="apply", reviewed_plan_sha256=digest))
            apply_payload = _payload(mode="apply", reviewed_plan_sha256=digest)
            applied = await self._post(app, apply_payload, idempotency_key="cm-testing-hold")
            replayed = await self._post(app, apply_payload, idempotency_key="cm-testing-hold")
            held = await self._get(app)
            lift = await self._post(app, _payload(hold=False, reason="Staff testing finished."))
            lifted = await self._post(
                app,
                _payload(
                    hold=False,
                    reason="Staff testing finished.",
                    mode="apply",
                    reviewed_plan_sha256=lift.json()["result"]["plan_sha256"],
                ),
                idempotency_key="cm-testing-hold-lift",
            )
            requests = store.list_product_reconcile_requests()

        self.assertEqual(dry_run.status_code, 202)
        self.assertEqual(missing_key.json()["error"]["code"], "idempotency_key_required")
        self.assertEqual(applied.status_code, 202)
        self.assertEqual(applied.json()["result"]["action"], "set")
        self.assertTrue(replayed.json()["replayed"])
        self.assertEqual(held.status_code, 200)
        self.assertEqual(held.json()["result"]["hold"]["reason"], _HOLD_REASON)
        self.assertEqual(lifted.status_code, 202)
        self.assertEqual(lifted.json()["result"]["action"], "clear")
        self.assertTrue(lifted.json()["result"]["reconcile_requested"])
        self.assertEqual([request.target_key for request in requests], [f"{_PRODUCT}:testing"])

    async def test_operator_agent_can_set_but_not_lift_a_hold(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "schema_version": 2,
                        "local_operators": [
                            {
                                "subjects": ["operator-agent"],
                                "token_labels": ["operator-agent-token"],
                                "products": [_PRODUCT],
                                "contexts": [_CONTEXT],
                                "instances": ["testing"],
                                "actions": ["product_config.plan", "product_config.apply"],
                            }
                        ],
                    }
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="operator-agent",
                    local_operator_token_label="operator-agent-token",
                ),
            )
            headers = {"Authorization": "Bearer local-operator-token"}
            hold = await http_request(
                app, "POST", TESTING_HOLD_APPLY_ROUTE, headers=headers, payload=_payload()
            )
            lift = await http_request(
                app,
                "POST",
                TESTING_HOLD_APPLY_ROUTE,
                headers=headers,
                payload=_payload(hold=False, reason="Staff testing finished."),
            )

        self.assertEqual(hold.status_code, 202, hold.text)
        self.assertEqual(lift.status_code, 403, lift.text)
        self.assertEqual(lift.json()["error"]["code"], "local_operator_lane_scope_required")

    async def test_plan_and_apply_require_their_actions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            no_plan = self._app(store, root, "product_environment.read")
            plan_only = self._app(store, root, "product_config.plan")

            denied_read = await self._get(no_plan)
            denied_dry_run = await self._post(no_plan, _payload())
            dry_run = await self._post(plan_only, _payload())
            denied_apply = await self._post(
                plan_only,
                _payload(
                    mode="apply", reviewed_plan_sha256=dry_run.json()["result"]["plan_sha256"]
                ),
                idempotency_key="cm-testing-hold-denied",
            )
            stored = _read_hold(store)

        self.assertEqual(denied_read.status_code, 403)
        self.assertEqual(denied_dry_run.status_code, 403)
        self.assertEqual(dry_run.status_code, 202)
        self.assertEqual(denied_apply.status_code, 403)
        self.assertIsNone(stored)

    async def test_refuses_other_lanes_without_echoing_input(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            app = self._app(store, root, "product_config.plan")

            prod = await self._post(app, _payload(instance="prod"))
            prod_read = await self._get(app, instance="prod")
            invalid = await self._post(app, {**_payload(), "secret": "hunter2"})

        self.assertEqual(prod.status_code, 409)
        self.assertEqual(prod.json()["error"]["code"], "testing_hold_not_testing_lane")
        self.assertEqual(prod_read.json()["error"]["code"], "testing_hold_not_testing_lane")
        self.assertEqual(invalid.status_code, 400)
        self.assertNotIn("hunter2", invalid.text)


if __name__ == "__main__":
    unittest.main()
