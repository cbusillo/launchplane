from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

from click import Command
from click.testing import CliRunner, Result
from pydantic import ValidationError

from control_plane.authz_repository_scope import build_authz_repository_scope_response
from control_plane.cli import main
from control_plane.contracts.authz_access_read import AuthzRepositoryScopeReadRequest
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
)
from control_plane.contracts.repository_human_admission import RepositoryHumanRolePolicyRecord
from control_plane.contracts.tenant_merge_eligibility import (
    TenantRepositoryClassificationRecord,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane import secrets as control_plane_secrets


REPOSITORY = "example/private-product"
REPOSITORY_ID = "123456"
REPOSITORY_OWNER_ID = "654321"
CLI_MAIN = cast(Command, main)


class _Store:
    def __init__(
        self,
        *,
        product_profiles: tuple[LaunchplaneProductProfileRecord, ...] = (),
        role_policies: tuple[RepositoryHumanRolePolicyRecord, ...] = (),
        classifications: tuple[TenantRepositoryClassificationRecord, ...] = (),
        work_requests: tuple[EveryCodeWorkRequestRecord, ...] = (),
    ) -> None:
        self.product_profiles = product_profiles
        self.role_policies = role_policies
        self.classifications = classifications
        self.work_requests = work_requests

    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        del driver_id
        return self.product_profiles

    def list_repository_human_role_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RepositoryHumanRolePolicyRecord, ...]:
        del repository_id, repository_owner_id, repository, product, context, status
        return self.role_policies if limit is None else self.role_policies[:limit]

    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]:
        del repository_id
        return self.classifications if limit is None else self.classifications[:limit]

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]:
        records = tuple(
            record
            for record in self.work_requests
            if (not state or record.state == state)
            and (not repository or record.repository == repository)
        )
        if limit is None:
            return records[offset:]
        return records[offset : offset + limit]


class _CliStore(_Store):
    def __init__(
        self,
        *,
        active_policy_records: tuple[LaunchplaneAuthzPolicyRecord, ...],
        product_profiles: tuple[LaunchplaneProductProfileRecord, ...] = (),
        role_policies: tuple[RepositoryHumanRolePolicyRecord, ...] = (),
        classifications: tuple[TenantRepositoryClassificationRecord, ...] = (),
        work_requests: tuple[EveryCodeWorkRequestRecord, ...] = (),
    ) -> None:
        super().__init__(
            product_profiles=product_profiles,
            role_policies=role_policies,
            classifications=classifications,
            work_requests=work_requests,
        )
        self.active_policy_records = active_policy_records
        self.closed = False
        self.mutation_attempts = 0

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = tuple(
            record for record in self.active_policy_records if not status or record.status == status
        )
        return records if limit is None else records[:limit]

    def close(self) -> None:
        self.closed = True

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        if name.startswith(("write_", "compare_and_write_", "reserve_", "append_", "delete_")):
            self.mutation_attempts += 1
            raise AssertionError(f"Unexpected mutation method lookup: {name}")
        raise AttributeError(name)


class AuthzRepositoryScopeTests(unittest.TestCase):
    def test_cli_reads_redacted_scope_without_mutation_or_authorization_claim(self) -> None:
        store = _CliStore(
            active_policy_records=(_policy_record(repository=REPOSITORY),),
            product_profiles=(_product_profile(),),
            role_policies=(_role_policy(),),
            classifications=(_classification(),),
            work_requests=(_work_request(),),
        )

        result = self._invoke_cli(store=store)

        self.assertEqual(result.exit_code, 0, result.output)
        response = json.loads(result.stdout)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["coverage"]["state"], "complete")
        self.assertEqual(response["candidates"][0]["state"], "matched")
        self.assertNotIn(REPOSITORY, result.output)
        self.assertNotIn(REPOSITORY_ID, result.output)
        self.assertNotIn(REPOSITORY_OWNER_ID, result.output)
        self.assertIn("configured PostgreSQL credentials", result.stderr)
        self.assertIn("does not prove", result.stderr)
        self.assertEqual(store.mutation_attempts, 0)
        self.assertTrue(store.closed)

    def test_cli_fails_closed_when_active_policy_is_missing(self) -> None:
        store = _CliStore(active_policy_records=())

        result = self._invoke_cli(store=store)

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("active authz policy is unavailable", result.output)
        self.assertTrue(store.closed)

    def test_cli_fails_closed_when_active_policy_is_ambiguous(self) -> None:
        policy_record = _policy_record(repository=REPOSITORY)
        store = _CliStore(active_policy_records=(policy_record, policy_record))

        result = self._invoke_cli(store=store)

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Multiple active Launchplane authz policy records", result.output)
        self.assertTrue(store.closed)

    def test_cli_rejects_malformed_request_before_opening_store(self) -> None:
        result = self._invoke_cli(
            store=None,
            request_payload={"candidates": [{"repository": "not-exact"}]},
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("request JSON is invalid", result.output)

    def test_cli_rejects_non_object_request_without_exposing_local_path(self) -> None:
        result = self._invoke_cli(store=None, request_payload=[])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("request JSON is invalid", result.output)
        self.assertNotIn("repository-scope.json", result.output)

    def test_cli_rejects_non_utf8_request_before_opening_store(self) -> None:
        result = self._invoke_cli(store=None, request_bytes=b"\xff")

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("request JSON is invalid", result.output)

    def test_cli_rejects_deeply_nested_request_before_opening_store(self) -> None:
        result = self._invoke_cli(
            store=None,
            request_bytes=(b'{"candidates":' + b'{"a":' * 100_000 + b"0" + b"}" * 100_000 + b"}"),
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("request JSON is invalid", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_cli_reports_unavailable_store_without_leaking_connection_details(self) -> None:
        result = self._invoke_cli(store=OSError("postgresql://secret@example.invalid/private"))

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("repository-scope evidence is unavailable", result.output)
        self.assertNotIn("secret", result.output)
        self.assertNotIn("example.invalid", result.output)

    @staticmethod
    def _invoke_cli(
        *,
        store: _CliStore | OSError | None,
        request_payload: object | None = None,
        request_bytes: bytes | None = None,
    ) -> Result:
        payload = (
            {
                "candidates": [
                    {
                        "repository": REPOSITORY,
                        "repository_id": REPOSITORY_ID,
                        "repository_owner_id": REPOSITORY_OWNER_ID,
                    }
                ]
            }
            if request_payload is None
            else request_payload
        )
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            request_file = Path(temporary_directory_name) / "repository-scope.json"
            if request_bytes is None:
                request_file.write_text(json.dumps(payload), encoding="utf-8")
            else:
                request_file.write_bytes(request_bytes)
            constructor_patch = (
                patch(
                    "control_plane.cli_policy_profiles.PostgresRecordStore",
                    side_effect=store,
                )
                if isinstance(store, OSError)
                else patch(
                    "control_plane.cli_policy_profiles.PostgresRecordStore",
                    return_value=store,
                )
            )
            with constructor_patch as postgres_store:
                with _handle_key("repository-scope-cli-key"):
                    result = CliRunner().invoke(
                        CLI_MAIN,
                        [
                            "authz-policies",
                            "repository-scope-evidence",
                            "--database-url",
                            "postgresql+psycopg://localhost/launchplane",
                            "--request-file",
                            str(request_file),
                        ],
                    )
        if store is None:
            postgres_store.assert_not_called()
        return result

    def test_complete_scope_matches_candidates_without_identity_leakage(self) -> None:
        policy_record = _policy_record(repository=REPOSITORY)
        store = _Store(
            product_profiles=(_product_profile(),),
            role_policies=(_role_policy(),),
            classifications=(_classification(),),
            work_requests=(_work_request(),),
        )
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {
                "candidates": [
                    {
                        "repository": REPOSITORY,
                        "repository_id": REPOSITORY_ID,
                        "repository_owner_id": REPOSITORY_OWNER_ID,
                    }
                ]
            }
        )

        with _handle_key("repository-scope-key-one"):
            first = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=policy_record,
                store=store,
            )
            repeated = build_authz_repository_scope_response(
                trace_id="trace-two",
                generated_at="2026-08-20T22:31:00+00:00",
                request=request,
                active_policy_record=policy_record,
                store=store,
            )

        self.assertEqual(first.coverage.state, "complete")
        self.assertEqual(first.coverage.repository_count, 1)
        self.assertEqual(first.coverage.matched_repository_count, 1)
        self.assertEqual(first.coverage.gaps, ())
        self.assertEqual(first.candidates[0].state, "matched")
        self.assertEqual(
            first.candidates[0].memberships,
            ("product", "repository_record", "work_graph", "authorization_chain"),
        )
        self.assertEqual(first.candidates[0].handle, repeated.candidates[0].handle)
        self.assertEqual(first.handle_generation, repeated.handle_generation)
        serialized = json.dumps(first.model_dump(mode="json"), sort_keys=True)
        for secret_value in (
            REPOSITORY,
            REPOSITORY_ID,
            REPOSITORY_OWNER_ID,
            "example-product",
            "deploy.write",
        ):
            self.assertNotIn(secret_value, serialized)

    def test_unmatched_scope_is_partial_without_disclosing_unknown_identity(self) -> None:
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {"candidates": [{"repository": "example/known-public"}]}
        )
        with _handle_key("repository-scope-key-one"):
            response = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=_policy_record(repository=REPOSITORY),
                store=_Store(product_profiles=(_product_profile(),)),
            )

        self.assertEqual(response.coverage.state, "partial")
        self.assertEqual(response.coverage.unmatched_repository_count, 1)
        self.assertEqual(response.candidates[0].state, "not_found")
        self.assertEqual(response.candidates[0].handle, "")
        self.assertEqual(
            {(gap.source, gap.reason_code, gap.count) for gap in response.coverage.gaps},
            {
                ("candidate", "candidate_coverage_incomplete", 1),
                ("candidate", "candidate_repository_missing", 1),
            },
        )
        self.assertNotIn(REPOSITORY, json.dumps(response.model_dump(mode="json")))

    def test_case_variant_reports_gap_without_hiding_authorization_membership(self) -> None:
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {"candidates": [{"repository": REPOSITORY}]}
        )
        with _handle_key("repository-scope-key-one"):
            response = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=_policy_record(repository="Example/Private-Product"),
                store=_Store(product_profiles=(_product_profile(),)),
            )

        self.assertEqual(response.coverage.state, "partial")
        self.assertEqual(response.candidates[0].state, "matched")
        self.assertEqual(
            response.candidates[0].memberships,
            ("product", "authorization_chain"),
        )
        self.assertIn(
            ("authorization_chain", "repository_case_variant", 1),
            {(gap.source, gap.reason_code, gap.count) for gap in response.coverage.gaps},
        )

    def test_lowercased_repository_records_do_not_create_false_case_variant(self) -> None:
        canonical_repository = "ExampleOrg/Private-Product"
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {"candidates": [{"repository": canonical_repository}]}
        )
        with _handle_key("repository-scope-key-one"):
            response = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=_policy_record(repository=canonical_repository),
                store=_Store(
                    product_profiles=(
                        _product_profile().model_copy(update={"repository": canonical_repository}),
                    ),
                    role_policies=(
                        _role_policy().model_copy(
                            update={"repository": canonical_repository.casefold()}
                        ),
                    ),
                    classifications=(
                        _classification().model_copy(
                            update={"repository": canonical_repository.casefold()}
                        ),
                    ),
                ),
            )

        self.assertEqual(response.coverage.state, "complete")
        self.assertEqual(
            response.candidates[0].memberships,
            ("product", "repository_record", "authorization_chain"),
        )

    def test_stale_records_cannot_make_authorization_membership_complete(self) -> None:
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {"candidates": [{"repository": REPOSITORY}]}
        )
        with _handle_key("repository-scope-key-one"):
            response = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=_policy_record(repository=REPOSITORY),
                store=_Store(
                    product_profiles=(_product_profile(lifecycle_state="retired"),),
                    work_requests=(_work_request(state="done"),),
                ),
            )

        self.assertEqual(response.coverage.state, "partial")
        self.assertEqual(response.candidates[0].memberships, ("authorization_chain",))
        self.assertIn(
            ("authorization_chain", "stale_authorization_membership", 1),
            {(gap.source, gap.reason_code, gap.count) for gap in response.coverage.gaps},
        )

    def test_handle_generation_changes_with_managed_secret_root(self) -> None:
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {"candidates": [{"repository": REPOSITORY}]}
        )
        with _handle_key("repository-scope-key-one"):
            first = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=_policy_record(repository=REPOSITORY),
                store=_Store(product_profiles=(_product_profile(),)),
            )
        with _handle_key("repository-scope-key-two"):
            rotated = build_authz_repository_scope_response(
                trace_id="trace-two",
                generated_at="2026-08-20T22:31:00+00:00",
                request=request,
                active_policy_record=_policy_record(repository=REPOSITORY),
                store=_Store(product_profiles=(_product_profile(),)),
            )

        self.assertNotEqual(first.handle_generation, rotated.handle_generation)
        self.assertNotEqual(first.candidates[0].handle, rotated.candidates[0].handle)

    def test_schema_v1_active_policy_repository_membership_remains_readable(self) -> None:
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {"candidates": [{"repository": REPOSITORY}]}
        )
        with _handle_key("repository-scope-key-one"):
            response = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=_policy_record(
                    repository=REPOSITORY,
                    schema_version=1,
                ),
                store=_Store(product_profiles=(_product_profile(),)),
            )

        self.assertEqual(response.coverage.state, "complete")
        self.assertEqual(
            response.candidates[0].memberships,
            ("product", "authorization_chain"),
        )

    def test_source_truncation_is_bounded_and_partial(self) -> None:
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {"candidates": [{"repository": REPOSITORY}]}
        )
        repeated_work_request = _work_request()
        with _handle_key("repository-scope-key-one"):
            response = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=_policy_record(repository=REPOSITORY),
                store=_Store(
                    product_profiles=(_product_profile(),),
                    work_requests=(repeated_work_request,) * 1001,
                ),
            )

        self.assertEqual(response.coverage.state, "partial")
        self.assertEqual(response.coverage.source_counts.work_graph, 1000)
        self.assertIn(
            ("work_graph", "source_truncated", 1),
            {(gap.source, gap.reason_code, gap.count) for gap in response.coverage.gaps},
        )

    def test_work_graph_source_cap_applies_across_states(self) -> None:
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {"candidates": [{"repository": REPOSITORY}]}
        )
        with _handle_key("repository-scope-key-one"):
            response = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=_policy_record(repository=REPOSITORY),
                store=_Store(
                    product_profiles=(_product_profile(),),
                    work_requests=(_work_request(),) * 600
                    + (
                        _work_request(
                            state="running",
                            lease_expires_at="2026-08-20T23:00:00Z",
                        ),
                    )
                    * 600,
                ),
            )

        self.assertEqual(response.coverage.state, "partial")
        self.assertEqual(response.coverage.source_counts.work_graph, 1000)
        self.assertIn(
            ("work_graph", "source_truncated", 1),
            {(gap.source, gap.reason_code, gap.count) for gap in response.coverage.gaps},
        )

    def test_expired_active_work_request_lease_is_partial(self) -> None:
        request = AuthzRepositoryScopeReadRequest.model_validate(
            {"candidates": [{"repository": REPOSITORY}]}
        )
        with _handle_key("repository-scope-key-one"):
            response = build_authz_repository_scope_response(
                trace_id="trace-one",
                generated_at="2026-08-20T22:30:00+00:00",
                request=request,
                active_policy_record=_policy_record(repository=REPOSITORY),
                store=_Store(
                    product_profiles=(_product_profile(),),
                    work_requests=(
                        _work_request(
                            state="running",
                            lease_expires_at="2026-08-20T22:00:00Z",
                        ),
                    ),
                ),
            )

        self.assertEqual(response.coverage.state, "partial")
        self.assertIn(
            ("work_graph", "stale_work_graph_record", 1),
            {(gap.source, gap.reason_code, gap.count) for gap in response.coverage.gaps},
        )

    def test_candidate_contract_is_exact_bounded_and_unique(self) -> None:
        invalid_payloads = (
            {"candidates": [{"repository": "not-a-repository"}]},
            {"candidates": [{"repository": "example/*"}]},
            {
                "candidates": [
                    {"repository": REPOSITORY},
                    {"repository": REPOSITORY.upper()},
                ]
            },
            {
                "candidates": [
                    {
                        "repository": REPOSITORY,
                        "repository_id": REPOSITORY_ID,
                    }
                ]
            },
            {"candidates": [{"repository": f"example/repository-{index}"} for index in range(101)]},
        )
        for payload in invalid_payloads:
            with self.subTest(payload_size=len(payload["candidates"])):
                with self.assertRaises(ValidationError):
                    AuthzRepositoryScopeReadRequest.model_validate(payload)


def _policy_record(
    *,
    repository: str,
    schema_version: int = 2,
) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": schema_version,
            "github_actions": [
                {
                    "repository": repository,
                    "repository_id": REPOSITORY_ID,
                    "repository_owner_id": REPOSITORY_OWNER_ID,
                    "actions": ["deploy.write"],
                }
            ],
        }
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        status="active",
        source="test:authz-repository-scope",
        updated_at="2026-08-20T22:00:00+00:00",
        policy_sha256=digest,
        policy=policy,
    )


def _product_profile(
    *,
    lifecycle_state: str = "active",
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord.model_validate(
        {
            "lifecycle_state": lifecycle_state,
            "product": "example-product",
            "display_name": "Example Product",
            "repository": REPOSITORY,
            "repository_id": REPOSITORY_ID,
            "repository_owner_id": REPOSITORY_OWNER_ID,
            "driver_id": "generic-web",
            "image": ProductImageProfile().model_dump(mode="json"),
            "updated_at": "2026-08-20T21:00:00+00:00",
            "source": "test:authz-repository-scope",
        }
    )


def _role_policy() -> RepositoryHumanRolePolicyRecord:
    return RepositoryHumanRolePolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        product="example-product",
        context="prod",
        role_policy_revision=1,
        repository_owner_github_ids=(1001,),
        manager_primary_github_ids=(1002,),
        effective_at="2026-08-20T20:00:00Z",
        source="test:authz-repository-scope",
        reason="repository scope test",
    )


def _classification() -> TenantRepositoryClassificationRecord:
    return TenantRepositoryClassificationRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        product="example-product",
        context="prod",
        classification_kind="engineering",
        classification_revision=1,
        classified_at="2026-08-20T20:30:00Z",
        source="test:authz-repository-scope",
        reason="repository scope test",
    )


def _work_request(
    *,
    state: str = "queued",
    lease_expires_at: str = "",
) -> EveryCodeWorkRequestRecord:
    payload: dict[str, object] = {
        "request_id": "repository-scope-work-request",
        "source": "manual",
        "state": state,
        "repository": REPOSITORY,
        "issue_number": 2199,
        "issue_url": "https://example.invalid/issues/2199",
        "trigger_label": "every-code",
        "queued_at": "2026-08-20T19:00:00Z",
        "updated_at": "2026-08-20T19:00:00Z",
    }
    if state == "done":
        payload.update(
            {
                "claimed_at": "2026-08-20T19:01:00Z",
                "claimed_by_host": "test-host",
                "started_at": "2026-08-20T19:02:00Z",
                "finished_at": "2026-08-20T19:03:00Z",
            }
        )
    elif state == "running":
        payload.update(
            {
                "claimed_at": "2026-08-20T19:01:00Z",
                "claimed_by_host": "test-host",
                "lease_expires_at": lease_expires_at,
                "fencing_token": 1,
                "attempt": 1,
                "started_at": "2026-08-20T19:02:00Z",
            }
        )
    return EveryCodeWorkRequestRecord.model_validate(payload)


def _handle_key(value: str):  # type: ignore[no-untyped-def]
    encoded_key = base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest())
    return patch.dict(
        os.environ,
        {
            control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: json.dumps(
                {
                    "active_key_id": "repository-scope-key",
                    "keys": {"repository-scope-key": encoded_key.decode()},
                }
            )
        },
        clear=True,
    )
