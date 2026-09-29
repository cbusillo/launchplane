import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetPolicies,
    DokployTargetRecord,
    DokployTargetRecordChanged,
    DokployTargetShopifyPolicy,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.integration_allowances import (
    INTEGRATION_ALLOWANCES_APPLY_ROUTE,
    INTEGRATION_ALLOWANCES_ROUTE,
    IntegrationAllowancesApplyRequest,
    IntegrationAllowancesRefusal,
    IntegrationAllowancesStale,
    apply_integration_allowances_plan,
    build_integration_allowances_plan,
)
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _AsgiResponse, _asgi_request
from tests.support.auth import _identity, _StubVerifier
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import _sqlite_database_url

_PRODUCT = "odoo-tenant-cm"
_CONTEXT = "cm"
_TIMESTAMP = "2026-09-29T12:00:00Z"
_PROTECTED_KEY = "cm-real-store"


def _seed_target(
    store: FilesystemRecordStore,
    *,
    instance: str = "testing",
    allowances: tuple[DokployTargetIntegrationAllowance, ...] = (),
) -> None:
    store.write_dokploy_target_record(
        DokployTargetRecord(
            context=_CONTEXT,
            instance=instance,
            policies=DokployTargetPolicies(
                shopify=DokployTargetShopifyPolicy(protected_store_keys=(_PROTECTED_KEY,)),
                integration_allowances=allowances,
            ),
            updated_at=_TIMESTAMP,
        )
    )


def _request_payload(
    *,
    instance: str = "testing",
    mode: str = "dry-run",
    reviewed_plan_sha256: str = "",
    allowances: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "product": _PRODUCT,
        "context": _CONTEXT,
        "instance": instance,
        "mode": mode,
        "reason": "CM testing is the working instance until CM prod is live.",
        "allowances": allowances
        if allowances is not None
        else [
            {
                "integration": "fishbowl",
                "kind": "read_only_source",
                "reason": "Imports from Fishbowl with a read-only account.",
                "evidence": "SELECT and SHOW VIEW grant read on 2026-09-28.",
            },
            {
                "integration": "repairshopr",
                "kind": "pre_live",
                "reason": "CM prod is not live on Launchplane yet.",
            },
        ],
    }
    if reviewed_plan_sha256:
        payload["reviewed_plan_sha256"] = reviewed_plan_sha256
    return payload


def _request(**kwargs: object) -> IntegrationAllowancesApplyRequest:
    return IntegrationAllowancesApplyRequest.model_validate(_request_payload(**kwargs))  # type: ignore[arg-type]


class IntegrationAllowanceContractTests(unittest.TestCase):
    def test_normalizes_and_sorts_allowances(self) -> None:
        policies = DokployTargetPolicies(
            integration_allowances=(
                DokployTargetIntegrationAllowance(
                    integration=" RepairShopr ", kind="pre_live", reason="Not live yet."
                ),
                DokployTargetIntegrationAllowance(
                    integration="cm_data", kind="pre_live", reason="Not live yet."
                ),
            )
        )

        self.assertEqual(
            [allowance.integration for allowance in policies.integration_allowances],
            ["cm_data", "repairshopr"],
        )

    def test_rejects_duplicate_integration_and_bad_names(self) -> None:
        allowance = DokployTargetIntegrationAllowance(
            integration="fishbowl", kind="pre_live", reason="Not live yet."
        )
        with self.assertRaises(ValidationError):
            DokployTargetPolicies(integration_allowances=(allowance, allowance))
        for name in ("", "cm-data", "1fishbowl", "fish bowl"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                DokployTargetIntegrationAllowance(integration=name, kind="pre_live", reason="r")

    def test_read_only_source_requires_evidence_and_every_kind_requires_reason(self) -> None:
        with self.assertRaises(ValidationError):
            DokployTargetIntegrationAllowance(
                integration="fishbowl", kind="read_only_source", reason="Imports only."
            )
        with self.assertRaises(ValidationError):
            DokployTargetIntegrationAllowance(integration="fishbowl", kind="pre_live", reason=" ")

    def test_records_without_allowances_still_load(self) -> None:
        record = DokployTargetRecord.model_validate(
            {"context": _CONTEXT, "instance": "testing", "updated_at": _TIMESTAMP}
        )

        self.assertEqual(record.policies.integration_allowances, ())


class IntegrationAllowancesPlanTests(unittest.TestCase):
    def test_dry_run_returns_diff_and_stable_digest_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store)

            plan, _replacement = build_integration_allowances_plan(
                record_store=store, request=_request(), actor="operator"
            )
            again, _ = build_integration_allowances_plan(
                record_store=store, request=_request(), actor="someone-else"
            )
            stored = store.read_dokploy_target_record(
                context_name=_CONTEXT, instance_name="testing"
            )

        self.assertTrue(plan.changed)
        self.assertEqual(
            [(change.integration, change.action) for change in plan.changes],
            [("fishbowl", "add"), ("repairshopr", "add")],
        )
        self.assertEqual(plan.plan_sha256, again.plan_sha256)
        self.assertEqual(stored.policies.integration_allowances, ())

    def test_apply_requires_matching_digest(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store)

            with self.assertRaises(IntegrationAllowancesStale):
                apply_integration_allowances_plan(
                    record_store=store,
                    request=_request(mode="apply", reviewed_plan_sha256="f" * 64),
                    actor="operator",
                )

    def test_apply_writes_reads_back_and_keeps_other_policies(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store)
            plan, _ = build_integration_allowances_plan(
                record_store=store, request=_request(), actor="operator"
            )

            applied = apply_integration_allowances_plan(
                record_store=store,
                request=_request(mode="apply", reviewed_plan_sha256=plan.plan_sha256),
                actor="operator",
            )
            stored = store.read_dokploy_target_record(
                context_name=_CONTEXT, instance_name="testing"
            )

        self.assertTrue(applied.applied)
        self.assertTrue(applied.read_back_matches)
        self.assertEqual(
            [(item.integration, item.kind, item.recorded_by) for item in applied.read_back],
            [("fishbowl", "read_only_source", "operator"), ("repairshopr", "pre_live", "operator")],
        )
        self.assertEqual(stored.policies.shopify.protected_store_keys, (_PROTECTED_KEY,))
        self.assertEqual(stored.source_label, "service:integration-allowances")

    def test_unchanged_allowance_keeps_its_recorder_and_empty_list_removes(self) -> None:
        existing = DokployTargetIntegrationAllowance(
            integration="fishbowl",
            kind="read_only_source",
            reason="Imports from Fishbowl with a read-only account.",
            evidence="SELECT and SHOW VIEW grant read on 2026-09-28.",
            recorded_by="first-operator",
            recorded_at=_TIMESTAMP,
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store, allowances=(existing,))
            keep = [
                {
                    "integration": "fishbowl",
                    "kind": "read_only_source",
                    "reason": existing.reason,
                    "evidence": existing.evidence,
                }
            ]
            unchanged, _ = build_integration_allowances_plan(
                record_store=store, request=_request(allowances=keep), actor="second-operator"
            )
            removal, _ = build_integration_allowances_plan(
                record_store=store, request=_request(allowances=[]), actor="second-operator"
            )

        self.assertFalse(unchanged.changed)
        change = unchanged.changes[0]
        self.assertEqual(change.action, "unchanged")
        self.assertIsNotNone(change.after)
        assert change.after is not None
        self.assertEqual(change.after.recorded_by, "first-operator")
        self.assertEqual(
            [(c.integration, c.action) for c in removal.changes], [("fishbowl", "remove")]
        )

    def test_retried_apply_after_a_lost_receipt_reports_already_applied(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store)
            plan, _ = build_integration_allowances_plan(
                record_store=store, request=_request(), actor="operator"
            )
            apply_request = _request(mode="apply", reviewed_plan_sha256=plan.plan_sha256)
            first = apply_integration_allowances_plan(
                record_store=store, request=apply_request, actor="operator"
            )
            retried = apply_integration_allowances_plan(
                record_store=store, request=apply_request, actor="operator"
            )

        self.assertTrue(first.applied)
        self.assertFalse(retried.applied)
        self.assertFalse(retried.changed)
        self.assertTrue(retried.read_back_matches)

    def test_apply_refuses_a_target_changed_after_review(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            _seed_target(store)
            plan, _ = build_integration_allowances_plan(
                record_store=store, request=_request(), actor="operator"
            )
            current = store.read_dokploy_target_record(
                context_name=_CONTEXT, instance_name="testing"
            )
            store.write_dokploy_target_record(
                current.model_copy(update={"domains": ("cm-testing.example.com",)})
            )

            with self.assertRaises(IntegrationAllowancesStale):
                apply_integration_allowances_plan(
                    record_store=store,
                    request=_request(mode="apply", reviewed_plan_sha256=plan.plan_sha256),
                    actor="operator",
                )
            stored = store.read_dokploy_target_record(
                context_name=_CONTEXT, instance_name="testing"
            )

        self.assertEqual(stored.domains, ("cm-testing.example.com",))
        self.assertEqual(stored.policies.integration_allowances, ())

    def test_compare_and_write_refuses_a_changed_record_in_both_stores(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stores: tuple[FilesystemRecordStore | PostgresRecordStore, ...] = (
                FilesystemRecordStore(state_dir=root / "state"),
                PostgresRecordStore(
                    database_url=_sqlite_database_url(root / "launchplane.sqlite3")
                ),
            )
            for store in stores:
                with self.subTest(store=type(store).__name__):
                    if isinstance(store, PostgresRecordStore):
                        store.ensure_schema()
                    _seed_target(store)  # type: ignore[arg-type]
                    reviewed = store.read_dokploy_target_record(
                        context_name=_CONTEXT, instance_name="testing"
                    )
                    changed = reviewed.model_copy(update={"domains": ("a.example.com",)})
                    store.write_dokploy_target_record(changed)
                    with self.assertRaises(DokployTargetRecordChanged):
                        store.compare_and_write_dokploy_target_record(
                            expected_record=reviewed,
                            replacement_record=reviewed.model_copy(update={"domains": ()}),
                        )
                    written = store.compare_and_write_dokploy_target_record(
                        expected_record=changed,
                        replacement_record=changed.model_copy(update={"domains": ()}),
                    )
                    self.assertEqual(
                        store.read_dokploy_target_record(
                            context_name=_CONTEXT, instance_name="testing"
                        ),
                        written,
                    )
            for store in stores:
                if isinstance(store, PostgresRecordStore):
                    store.close()

    def test_refusals(self) -> None:
        cases = (
            ("prod", _request_payload(instance="prod"), "production_lane"),
            (
                "pr-12",
                _request_payload(
                    instance="pr-12",
                    allowances=[{"integration": "shopify", "kind": "pre_live", "reason": "r"}],
                ),
                "pre_live_not_allowed",
            ),
        )
        for instance, payload, code in cases:
            with self.subTest(code=code), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(state_dir=Path(directory))
                _seed_target(store, instance=instance)
                with self.assertRaises(IntegrationAllowancesRefusal) as raised:
                    build_integration_allowances_plan(
                        record_store=store,
                        request=IntegrationAllowancesApplyRequest.model_validate(payload),
                        actor="operator",
                    )
                self.assertEqual(raised.exception.code, code)

    def test_missing_target_record_is_refused(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            with self.assertRaises(IntegrationAllowancesRefusal) as raised:
                build_integration_allowances_plan(
                    record_store=store, request=_request(), actor="operator"
                )

        self.assertEqual(raised.exception.code, "target_record_missing")

    def test_request_rejects_apply_without_digest_and_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            IntegrationAllowancesApplyRequest.model_validate(_request_payload(mode="apply"))
        payload = _request_payload()
        payload["protected_store_keys"] = ["x"]
        with self.assertRaises(ValidationError):
            IntegrationAllowancesApplyRequest.model_validate(payload)


class IntegrationAllowancesRouteTests(unittest.IsolatedAsyncioTestCase):
    _WORKFLOW_REF = "cbusillo/launchplane/.github/workflows/operator.yml@refs/heads/main"

    def _identity(self) -> GitHubActionsIdentity:
        return _identity(
            repository="cbusillo/launchplane",
            workflow_ref=self._WORKFLOW_REF,
            event_name="workflow_dispatch",
        )

    def _policy(self, *actions: str) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
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

    @staticmethod
    def _store(root: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=root / "state")
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload_with_prod_lane())
        )
        _seed_target(store)
        return store

    def _app(self, store: FilesystemRecordStore, root: Path, *actions: str) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=self._policy(*actions),
            record_store_factory=lambda: store,
            control_plane_root_path=root,
        )

    @staticmethod
    async def _post(
        app: FastAPI, payload: dict[str, object], *, idempotency_key: str = ""
    ) -> _AsgiResponse:
        headers = {"Authorization": "Bearer valid-token"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await _asgi_request(
            app, "POST", INTEGRATION_ALLOWANCES_APPLY_ROUTE, headers=headers, payload=payload
        )

    @staticmethod
    async def _get(app: FastAPI, instance: str = "testing") -> _AsgiResponse:
        return await _asgi_request(
            app,
            "GET",
            f"{INTEGRATION_ALLOWANCES_ROUTE}?product={_PRODUCT}&context={_CONTEXT}"
            f"&instance={instance}",
            headers={"Authorization": "Bearer valid-token"},
        )

    async def test_dry_run_apply_replay_and_read(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            app = self._app(store, root, "product_config.plan", "product_config.apply")

            dry_run = await self._post(app, _request_payload())
            plan = dry_run.json()["result"]
            missing_key = await self._post(
                app, _request_payload(mode="apply", reviewed_plan_sha256=plan["plan_sha256"])
            )
            stale = await self._post(
                app,
                _request_payload(mode="apply", reviewed_plan_sha256="f" * 64),
                idempotency_key="cm-testing-allowances-stale",
            )
            apply_payload = _request_payload(mode="apply", reviewed_plan_sha256=plan["plan_sha256"])
            applied = await self._post(
                app, apply_payload, idempotency_key="cm-testing-allowances-apply"
            )
            replayed = await self._post(
                app, apply_payload, idempotency_key="cm-testing-allowances-apply"
            )
            read = await self._get(app)

        self.assertEqual(dry_run.status_code, 202)
        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json()["error"]["code"], "idempotency_key_required")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "stale")
        self.assertEqual(applied.status_code, 202)
        self.assertTrue(applied.json()["result"]["read_back_matches"])
        self.assertTrue(replayed.json()["replayed"])
        self.assertEqual(read.status_code, 200)
        self.assertEqual(
            [item["integration"] for item in read.json()["result"]["allowances"]],
            ["fishbowl", "repairshopr"],
        )

    async def test_plan_and_apply_require_their_actions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            no_plan = self._app(store, root, "product_environment.read")
            plan_only = self._app(store, root, "product_config.plan")

            denied_read = await self._get(no_plan)
            denied_dry_run = await self._post(no_plan, _request_payload())
            dry_run = await self._post(plan_only, _request_payload())
            denied_apply = await self._post(
                plan_only,
                _request_payload(
                    mode="apply", reviewed_plan_sha256=dry_run.json()["result"]["plan_sha256"]
                ),
                idempotency_key="cm-testing-allowances-denied",
            )

        self.assertEqual(denied_read.status_code, 403)
        self.assertEqual(denied_dry_run.status_code, 403)
        self.assertEqual(dry_run.status_code, 202)
        self.assertEqual(denied_apply.status_code, 403)

    async def test_refuses_a_lane_the_product_does_not_own(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            store.write_dokploy_target_record(
                DokployTargetRecord(context="opw", instance="testing", updated_at=_TIMESTAMP)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [self._WORKFLOW_REF],
                            "event_names": ["workflow_dispatch"],
                            "products": [_PRODUCT],
                            "contexts": [_CONTEXT, "opw"],
                            "actions": ["product_config.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            payload = _request_payload()
            payload["context"] = "opw"
            dry_run = await self._post(app, payload)
            read = await _asgi_request(
                app,
                "GET",
                f"{INTEGRATION_ALLOWANCES_ROUTE}?product={_PRODUCT}&context=opw&instance=testing",
                headers={"Authorization": "Bearer valid-token"},
            )
            stored = store.read_dokploy_target_record(context_name="opw", instance_name="testing")

        self.assertIn(dry_run.status_code, {403, 404})
        self.assertIn(read.status_code, {403, 404})
        self.assertEqual(stored.policies.integration_allowances, ())

    async def test_refusal_and_invalid_request_do_not_echo_input(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            _seed_target(store, instance="prod")
            app = self._app(store, root, "product_config.plan")

            refused = await self._post(app, _request_payload(instance="prod"))
            payload = _request_payload()
            payload["allowances"] = [
                {"integration": "fishbowl", "kind": "pre_live", "reason": "r", "value": "hunter2"}
            ]
            invalid = await self._post(app, payload)
            missing = await self._get(app, instance="dev")

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["error"]["code"], "integration_allowances_production_lane")
        self.assertEqual(invalid.status_code, 400)
        self.assertNotIn("hunter2", invalid.text)
        self.assertIn(missing.status_code, {403, 404})


if __name__ == "__main__":
    unittest.main()
