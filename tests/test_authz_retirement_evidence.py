from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import event

from control_plane.authz_retirement_evidence import build_retirement_evidence
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.authz_retirement_evidence import (
    AuthzRetirementEvidenceRequest,
    AuthzWorkflowEvidenceCandidate,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.support.http import lifespan_client
from tests.test_authz_administration_read import (
    _app,
    _browser_headers,
    _database_app,
    _github_human_policy,
    _human_session_manager,
    _local_admin_policy,
    _operator_read_policy,
    _record,
)


def _request(
    record: LaunchplaneAuthzPolicyRecord, **changes: object
) -> AuthzRetirementEvidenceRequest:
    return AuthzRetirementEvidenceRequest.model_validate(
        {
            "policy_record_id": record.record_id,
            "policy_revision": record.revision,
            "policy_sha256": record.policy_sha256,
            "managed_set_ids": ["operator.odoo.test", "operator.odoo-test", "operator.missing"],
            "workflows": [
                {
                    "repository": "example/product",
                    "workflow_path": "example/product/.github/workflows/obsolete.yml",
                }
            ],
            **changes,
        }
    )


def _workflow_policy(
    *selectors: str, managed: bool = False, repository: str = "example/product", job: bool = False
) -> LaunchplaneAuthzPolicy:
    rule: dict[str, object] = {
        "repository": repository,
        "job_workflow_refs" if job else "workflow_refs": selectors,
        "products": ["private-product"],
        "contexts": ["private-context"],
        "actions": ["private.deploy"],
    }
    if managed:
        rule.update(managed_set_id="operator.odoo.test", managed_rule_id="private-rule")
    return LaunchplaneAuthzPolicy.model_validate({"schema_version": 2, "github_actions": [rule]})


class RetirementEvidenceTests(unittest.TestCase):
    def test_exact_aliases_and_absent_sets_do_not_prove_origin_or_independence(self) -> None:
        record = _record(policy=_workflow_policy(managed=True))
        result = build_retirement_evidence(
            active=record, records=(record,), request=_request(record), trace_id="trace"
        )
        self.assertEqual(
            [item.presence for item in result.managed_sets], ["present", "absent", "absent"]
        )
        self.assertTrue(
            all(
                item.import_origin == "unknown" and item.independent_maintenance == "unknown"
                for item in result.managed_sets
            )
        )
        self.assertTrue(
            all("import_origin_not_attested" in item.reason_codes for item in result.managed_sets)
        )
        serialized = result.model_dump_json()
        for private in (
            "private-product",
            "private-context",
            "private.deploy",
            "private-rule",
            "example/product",
            "operator.odoo",
        ):
            self.assertNotIn(private, serialized)

    def test_workflow_paths_are_compared_across_refs_without_exposing_selectors(self) -> None:
        for selectors, managed, repository, expected in (
            ((), False, "example/product", "potential_dependency"),
            (
                ("example/product/.github/workflows/obsolete.yml@refs/heads/removed",),
                False,
                "example/product",
                "potential_dependency",
            ),
            (
                ("example/*/.github/workflows/*.yml@*",),
                False,
                "example/product",
                "potential_dependency",
            ),
            (
                ("example/product/.github/workflows/retained.yml@*",),
                False,
                "example/product",
                "no_matching_rule",
            ),
            ((), True, "example/product", "no_matching_rule"),
            ((), False, "other/product", "no_matching_rule"),
            (("*",), False, "example/product", "unknown"),
            (("malformed@",), False, "example/product", "unknown"),
        ):
            with self.subTest(selectors=selectors, managed=managed, repository=repository):
                record = _record(
                    policy=_workflow_policy(*selectors, managed=managed, repository=repository)
                )
                result = build_retirement_evidence(
                    active=record, records=(record,), request=_request(record), trace_id="trace"
                )
                evidence = result.unmanaged_workflows[0]
                self.assertEqual(evidence.state, expected)
                self.assertEqual(bool(evidence.rule_sha256s), expected == "potential_dependency")
                self.assertNotIn("private.deploy", evidence.model_dump_json())

    def test_reusable_workflow_binding_is_independent_of_caller_binding(self) -> None:
        record = _record(
            policy=_workflow_policy("shared/workers/.github/workflows/obsolete.yml@*", job=True)
        )
        request = _request(
            record,
            workflows=[
                {
                    "repository": "example/product",
                    "workflow_path": "shared/workers/.github/workflows/obsolete.yml",
                    "binding": "job_workflow_ref",
                }
            ],
        )
        result = build_retirement_evidence(
            active=record, records=(record,), request=request, trace_id="trace"
        )
        self.assertEqual(result.unmanaged_workflows[0].state, "potential_dependency")

    def test_bound_audit_proves_recorded_transition_but_never_secret_origin(self) -> None:
        previous = _record(policy=LaunchplaneAuthzPolicy(schema_version=2), status="superseded")
        current = _record(policy=_workflow_policy(managed=True), revision=2)
        audit: dict[str, object] = {
            "operation": "managed_rule_set_reconcile",
            "mode": "apply",
            "changed": True,
            "source": current.source,
            "managed_set_id": "operator.odoo.test",
            "previous_policy_record_id": previous.record_id,
            "previous_revision": previous.revision,
            "previous_policy_sha256": previous.policy_sha256,
            "new_policy_record_id": current.record_id,
            "new_revision": current.revision,
            "new_policy_sha256": current.policy_sha256,
            "operator": {
                "type": "github_actions",
                "subject": "private-subject",
                "workflow_ref": "private-workflow",
            },
        }
        current.audit = audit
        result = build_retirement_evidence(
            active=current, records=(current, previous), request=_request(current), trace_id="trace"
        )
        evidence = result.managed_sets[0]
        self.assertEqual(result.history.state, "complete")
        self.assertEqual(len(evidence.observations), 1)
        self.assertTrue(evidence.observations[0].introduced)
        self.assertEqual(evidence.observations[0].caller_type, "github_actions")
        self.assertEqual(evidence.import_origin, "unknown")
        for field in audit:
            with self.subTest(field=field):
                current.audit = {**audit, field: "unbound"}
                changed = build_retirement_evidence(
                    active=current,
                    records=(current, previous),
                    request=_request(current),
                    trace_id="trace",
                )
                observations = changed.managed_sets[0].observations
                if field == "operator":
                    self.assertEqual(observations[0].caller_type, "unknown")
                else:
                    self.assertEqual(observations, ())
        self.assertNotIn("private-subject", evidence.model_dump_json())

    def test_incomplete_ambiguous_and_truncated_history_stays_explicit(self) -> None:
        current = _record(policy=_workflow_policy(managed=True), revision=4)
        earlier = _record(
            policy=LaunchplaneAuthzPolicy(schema_version=2), revision=2, status="superseded"
        )
        for records, gap in (
            ((), "history_active_record_mismatch"),
            ((earlier,), "history_active_record_mismatch"),
            ((current, earlier), "history_revision_gap_or_ambiguity"),
            ((current, current), "history_revision_gap_or_ambiguity"),
            ((current,), "history_baseline_missing"),
        ):
            with self.subTest(gap=gap, records=len(records)):
                result = build_retirement_evidence(
                    active=current, records=records, request=_request(current), trace_id="trace"
                )
                self.assertEqual(result.history.state, "partial")
                self.assertIn(gap, result.history.reason_codes)
                self.assertEqual(result.managed_sets[0].import_origin, "unknown")
        with patch(
            "control_plane.authz_retirement_evidence.AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT", 1
        ):
            result = build_retirement_evidence(
                active=current,
                records=(current, earlier),
                request=_request(current),
                trace_id="trace",
            )
        self.assertIn("history_truncated", result.history.reason_codes)

    def test_policy_limit_cannot_prove_absence(self) -> None:
        record = _record(policy=_workflow_policy())
        with patch("control_plane.authz_retirement_evidence.AUTHZ_RETIREMENT_MAX_RULES", 0):
            result = build_retirement_evidence(
                active=record, records=(record,), request=_request(record), trace_id="trace"
            )
        self.assertTrue(all(item.presence == "unknown" for item in result.managed_sets))
        self.assertEqual(result.unmanaged_workflows[0].state, "unknown")

    def test_malformed_or_broad_comparison_scope_is_refused(self) -> None:
        record = _record(policy=_workflow_policy())
        invalid_scopes: tuple[dict[str, object], ...] = (
            {"managed_set_ids": [], "workflows": []},
            {"managed_set_ids": ["operator.*"]},
            {"managed_set_ids": ["operator.same", "operator.same"]},
            {"managed_set_ids": [" Operator.Mixed "]},
            {
                "workflows": [
                    {
                        "repository": "example/*",
                        "workflow_path": "example/product/.github/workflows/obsolete.yml",
                    }
                ]
            },
            {
                "workflows": [
                    {
                        "repository": "example/product",
                        "workflow_path": "example/product/.github/workflows/../obsolete.yml",
                    }
                ]
            },
            {"policy_sha256": "invalid"},
            {"policy": {}},
        )
        for changes in invalid_scopes:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                _request(record, **changes)
        candidate = AuthzWorkflowEvidenceCandidate(
            repository="example/product",
            workflow_path="example/product/.github/workflows/obsolete.yml",
        )
        with self.assertRaises(ValidationError):
            _request(record, workflows=[candidate, candidate])


class RetirementEvidenceHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_operator_read_authority_is_inert_and_redacted(self) -> None:
        with _database_app(_operator_read_policy()) as (store, record, app):
            before = store.list_authz_policy_records()
            async with lifespan_client(app) as client:

                def require_select(
                    _connection: object,
                    _cursor: object,
                    statement: str,
                    _parameters: object,
                    _context: object,
                    _executemany: bool,
                ) -> None:
                    self.assertTrue(statement.lstrip().upper().startswith("SELECT"), statement)

                event.listen(store._engine, "before_cursor_execute", require_select)
                with patch.object(
                    store,
                    "write_authz_denial_record",
                    side_effect=AssertionError("read must not persist"),
                ):
                    response = await client.post(
                        "/v1/authz-policies/retirement-evidence",
                        json=_request(record).model_dump(mode="json"),
                        headers={"Authorization": "Bearer reader-token"},
                    )
                event.remove(store._engine, "before_cursor_execute", require_select)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(store.list_authz_policy_records(), before)
            self.assertEqual(response.json()["policy"]["policy_sha256"], record.policy_sha256)
            self.assertNotIn("record-reader", json.dumps(response.json()))

    async def test_anonymous_and_malformed_scope_errors_are_no_store(self) -> None:
        with _database_app(_operator_read_policy()) as (_, record, app):
            async with lifespan_client(app) as client:
                anonymous = await client.post(
                    "/v1/authz-policies/retirement-evidence",
                    json=_request(record).model_dump(mode="json"),
                )
                self.assertEqual(anonymous.status_code, 401, anonymous.text)
                malformed = _request(record).model_dump(mode="json")
                malformed["managed_set_ids"] = ["operator.*"]
                refused = await client.post(
                    "/v1/authz-policies/retirement-evidence",
                    json=malformed,
                    headers={"Authorization": "Bearer reader-token"},
                )
                self.assertEqual(refused.status_code, 400, refused.text)
                self.assertEqual(refused.json()["error"]["code"], "invalid_request")
                for response in (anonymous, refused):
                    self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_runtime_and_fresh_database_authority_are_both_required(self) -> None:
        allowed = _local_admin_policy()
        denied = _local_admin_policy(administration_action="unrelated.read")
        for runtime, database in ((allowed, denied), (denied, allowed)):
            with (
                self.subTest(runtime=runtime),
                _database_app(database, runtime_policy=runtime) as (store, record, app),
            ):
                async with lifespan_client(app) as client:
                    with patch.object(
                        store,
                        "write_authz_denial_record",
                        side_effect=AssertionError("denial must not persist"),
                    ):
                        response = await client.post(
                            "/v1/authz-policies/retirement-evidence",
                            json=_request(record).model_dump(mode="json"),
                            headers={"Authorization": "Bearer admin-token"},
                        )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_stale_binding_and_drift_during_history_read_are_refused(self) -> None:
        with _database_app(_operator_read_policy()) as (store, record, app):
            async with lifespan_client(app) as client:
                for changes in (
                    {"policy_revision": record.revision + 1},
                    {"policy_sha256": "0" * 64},
                    {"policy_record_id": "other-record"},
                ):
                    response = await client.post(
                        "/v1/authz-policies/retirement-evidence",
                        json=_request(record, **changes).model_dump(mode="json"),
                        headers={"Authorization": "Bearer reader-token"},
                    )
                    self.assertEqual(response.status_code, 409, response.text)
                original = store.list_authz_policy_records
                calls = 0

                def drift(
                    *, status: str = "", limit: int | None = None
                ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
                    nonlocal calls
                    if status == "active":
                        calls += 1
                        return (
                            record if calls == 1 else _record(policy=record.policy, revision=2),
                        )
                    return original(status=status, limit=limit)

                with patch.object(store, "list_authz_policy_records", side_effect=drift):
                    response = await client.post(
                        "/v1/authz-policies/retirement-evidence",
                        json=_request(record).model_dump(mode="json"),
                        headers={"Authorization": "Bearer reader-token"},
                    )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "authz_evidence_policy_changed")

    async def test_browser_read_requires_origin_csrf_and_does_not_renew_session(self) -> None:
        manager, sessions = _human_session_manager()
        identity = GitHubHumanIdentity(
            login="browser-admin",
            github_id=101,
            name="",
            email="",
            organizations=frozenset(),
            teams=frozenset(),
            role="admin",
        )
        headers, session_id, _ = _browser_headers(manager, identity)
        with _database_app(_github_human_policy(), human_session_manager=manager) as (
            _,
            record,
            app,
        ):
            before = sessions.read_session(session_id)
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/authz-policies/retirement-evidence",
                    headers=headers,
                    json=_request(record).model_dump(mode="json"),
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("set-cookie", response.headers)
                for bad in (
                    {**headers, "Origin": "https://attacker.example"},
                    {key: value for key, value in headers.items() if key != "X-CSRF-Token"},
                    {key: value for key, value in headers.items() if key != "Origin"},
                ):
                    response = await client.post(
                        "/v1/authz-policies/retirement-evidence",
                        headers=bad,
                        json=_request(record).model_dump(mode="json"),
                    )
                    self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(sessions.read_session(session_id), before)

    async def test_missing_database_and_active_policy_fail_closed(self) -> None:
        record = _record(policy=_operator_read_policy())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app = _app(
                root=root, store=FilesystemRecordStore(root / "state"), runtime_record=record
            )
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/authz-policies/retirement-evidence",
                    headers={"Authorization": "Bearer reader-token"},
                    json=_request(record).model_dump(mode="json"),
                )
            self.assertEqual(response.status_code, 503, response.text)
        with _database_app(_operator_read_policy()) as (store, record, app):
            async with lifespan_client(app) as client:
                for records, expected in (((), 503), ((record, record), 409)):
                    with patch.object(store, "list_authz_policy_records", return_value=records):
                        response = await client.post(
                            "/v1/authz-policies/retirement-evidence",
                            headers={"Authorization": "Bearer reader-token"},
                            json=_request(record).model_dump(mode="json"),
                        )
                    self.assertEqual(response.status_code, expected, response.text)
