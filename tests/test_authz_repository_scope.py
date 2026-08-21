from __future__ import annotations

import base64
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.authz_repository_scope import build_authz_repository_scope_response
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


class AuthzRepositoryScopeTests(unittest.TestCase):
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
