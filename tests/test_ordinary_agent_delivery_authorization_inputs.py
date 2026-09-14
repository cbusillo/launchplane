from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from control_plane.authz_candidate_preparation import (
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
    ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicy, MergeTrainPolicyRecord
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.http_routes.privileged_operations import (
    PrivilegedOperationRouteDependencies,
    register_privileged_operation_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.ordinary_agent_delivery_authorization_inputs import (
    MAX_AUTHORIZATION_INPUT_MERGE_TARGETS,
    MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS,
    read_ordinary_agent_delivery_authorization_candidate_inputs,
)
from control_plane.provider_delivery_inspection_profile import (
    PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY,
    PROVIDER_DELIVERY_INSPECTION_INTEGRATION,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LocalAdminIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.merge_train_policy_fixtures import (
    build_test_merge_train_policy,
    build_test_merge_train_policy_record,
)
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url
from tests.test_privileged_operation_http import _human, _policy, _policy_record


_ROUTE = "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/inputs"


class _ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class _ReadStore:
    def __init__(
        self,
        *,
        inventories: tuple[object, ...] = (),
        merge_policies: tuple[object, ...] = (),
        inventory_error: Exception | None = None,
        merge_policy_error: Exception | None = None,
    ) -> None:
        self.inventories = inventories
        self.merge_policies = merge_policies
        self.inventory_error = inventory_error
        self.merge_policy_error = merge_policy_error
        self.inventory_reads = 0
        self.merge_policy_reads = 0

    def list_repository_inventory_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        self.inventory_reads += 1
        if self.inventory_error is not None:
            raise self.inventory_error
        records = tuple(
            record
            for record in self.inventories
            if not repository_id or getattr(record, "repository_id", "") == repository_id
        )
        return records if limit is None else records[:limit]

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        self.merge_policy_reads += 1
        if self.merge_policy_error is not None:
            raise self.merge_policy_error
        records = tuple(
            record
            for record in self.merge_policies
            if not status or getattr(record, "status", "") == status
        )
        return records if limit is None else records[:limit]

    def __getattr__(self, name: str) -> object:
        if name.startswith(("write_", "create_", "apply_", "compare_and_write_")):
            raise AssertionError(f"read-only input projection attempted {name}")
        raise AttributeError(name)


class _InspectionReadStore(_ReadStore):
    def __init__(
        self,
        *,
        runtime_records: tuple[object, ...] = (),
        secret_records: tuple[object, ...] = (),
        secret_bindings: tuple[object, ...] = (),
        runtime_error: Exception | None = None,
        secret_error: Exception | None = None,
        binding_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.runtime_records = runtime_records
        self.secret_records = secret_records
        self.secret_bindings = secret_bindings
        self.runtime_error = runtime_error
        self.secret_error = secret_error
        self.binding_error = binding_error
        self.runtime_metadata_reads = 0
        self.secret_metadata_reads = 0
        self.binding_metadata_reads = 0

    def list_provider_delivery_inspection_setup_runtime_records(
        self,
    ) -> tuple[object, ...]:
        self.runtime_metadata_reads += 1
        if self.runtime_error is not None:
            raise self.runtime_error
        return self.runtime_records

    def list_provider_delivery_inspection_setup_secret_records(self) -> tuple[object, ...]:
        self.secret_metadata_reads += 1
        if self.secret_error is not None:
            raise self.secret_error
        return self.secret_records

    def list_provider_delivery_inspection_setup_secret_bindings(
        self,
    ) -> tuple[object, ...]:
        self.binding_metadata_reads += 1
        if self.binding_error is not None:
            raise self.binding_error
        return self.secret_bindings

    def read_secret_version(self, version_id: str) -> object:
        raise AssertionError(f"inspection setup metadata read secret version {version_id}")


def _inventory(
    *,
    repository_id: int,
    repository: str,
    revision: int = 1,
    state: str = "tracked",
    reason: str = "test inventory",
) -> RepositoryInventoryRecord:
    return RepositoryInventoryRecord.model_validate(
        {
            "repository_id": str(repository_id),
            "repository_owner_id": "9001",
            "repository": repository,
            "inventory_state": state,
            "inventory_revision": revision,
            "recorded_at": f"2026-09-{revision + 1:02d}T12:00:00Z",
            "source": "test",
            "reason": reason,
            "supersedes_record_id": (
                f"repository-inventory-{repository_id}-r{revision - 1}" if revision > 1 else None
            ),
        }
    )


def _merge_policy_with_branches(
    *, repository: str, branches: tuple[str, ...]
) -> MergeTrainPolicyRecord:
    template = build_test_merge_train_policy(repository=repository).policies[0]
    policy = MergeTrainPolicy(
        policies=tuple(template.model_copy(update={"base_branch": branch}) for branch in branches)
    )
    return MergeTrainPolicyRecord(
        record_id="merge-train-policy-multiple-branches",
        source="test",
        updated_at="2026-09-12T12:00:00Z",
        policy=policy,
    )


def _inspection_runtime(
    *, app_id: object = 42, include_app_id: bool = True
) -> RuntimeEnvironmentRecord:
    env: dict[str, object] = {"UNRELATED_RUNTIME_VALUE": "not-returned"}
    if include_app_id:
        env[PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY] = app_id
    return RuntimeEnvironmentRecord.model_validate(
        {
            "scope": "context",
            "context": "launchplane",
            "instance": "",
            "env": env,
            "updated_at": "2026-09-12T12:00:00Z",
        }
    )


def _inspection_secret(
    *, secret_id: str = "inspection-secret", current_version_id: str = "inspection-version-v1"
) -> SecretRecord:
    return SecretRecord(
        secret_id=secret_id,
        scope="context",
        context="launchplane",
        integration=PROVIDER_DELIVERY_INSPECTION_INTEGRATION,
        name="private provider inspection key",
        description="not returned",
        current_version_id=current_version_id,
        created_at="2026-09-12T12:00:00Z",
        updated_at="2026-09-12T12:00:00Z",
    )


def _inspection_binding(*, secret_id: str = "inspection-secret") -> SecretBinding:
    return SecretBinding(
        binding_id="inspection-binding",
        secret_id=secret_id,
        integration=PROVIDER_DELIVERY_INSPECTION_INTEGRATION,
        binding_key="private_key",
        context="launchplane",
        instance="",
        created_at="2026-09-12T12:00:00Z",
        updated_at="2026-09-12T12:00:00Z",
    )


def _app(
    *,
    store: object,
    runtime_policy: LaunchplaneAuthzPolicy,
    persisted_policy: LaunchplaneAuthzPolicy | None = None,
    identity: GitHubHumanIdentity | TerminalAgentIdentity | LocalAdminIdentity | None = None,
    policy_record_reader: Callable[[], object] | None = None,
) -> FastAPI:
    app = FastAPI()
    trace_ids = iter(range(1, 100))

    def http_error(
        *,
        status_code: int,
        trace_id: str,
        code: str,
        message: str,
        authz: dict[str, object] | None = None,
    ) -> HTTPException:
        _ = trace_id, authz
        return HTTPException(status_code=status_code, detail={"code": code, "message": message})

    resolved_identity = identity or _human()
    persisted = persisted_policy or runtime_policy
    register_privileged_operation_routes(
        cast(ApiRouteRegistrar, app),
        dependencies=PrivilegedOperationRouteDependencies(
            common=ReadRouteDependencies(
                read_identity=lambda: resolved_identity,
                get_record_store=lambda: store,
                next_trace_id=lambda: f"trace-{next(trace_ids)}",
                authorization_allows=lambda **_: False,
                http_error=http_error,
                error_response_model=_ErrorResponse,
            ),
            read_bearer_identity=lambda: resolved_identity,
            read_github_human_identity=lambda: cast(GitHubHumanIdentity, resolved_identity),
            read_github_human_mutation_identity=lambda: cast(
                GitHubHumanIdentity, resolved_identity
            ),
            policy_reader=lambda: runtime_policy,
            policy_record_reader=policy_record_reader or (lambda: _policy_record(persisted)),
        ),
    )
    return app


class OrdinaryAgentDeliveryAuthorizationInputServiceTests(unittest.TestCase):
    def test_terminal_enrollment_readiness_is_redacted_and_nonmutating(self) -> None:
        store = _ReadStore()
        identity = TerminalAgentIdentity(subject="trusted-terminal", token_label="owner-terminal")
        response = read_ordinary_agent_delivery_authorization_candidate_inputs(
            record_store=store,
            policy_record=_policy_record(_policy()),
            trace_id="terminal-readiness",
            observed_at="2026-09-14T15:00:00Z",
            configured_terminal_identity=identity,
        )

        self.assertEqual(response.terminal_enrollment.state, "missing")
        serialized = response.model_dump_json()
        self.assertNotIn(identity.subject, serialized)
        self.assertNotIn(identity.token_label, serialized)
        self.assertEqual(store.inventory_reads, 1)
        self.assertEqual(store.merge_policy_reads, 1)

    def _read(self, store: object):  # type: ignore[no-untyped-def]
        return read_ordinary_agent_delivery_authorization_candidate_inputs(
            record_store=store,
            policy_record=_policy_record(_policy()),
            trace_id="trace-test",
            observed_at="2026-09-12T12:30:00Z",
        )

    def test_inspection_setup_defaults_to_not_evaluated_for_legacy_store(self) -> None:
        response = self._read(_ReadStore())

        self.assertEqual(response.inspection_setup.state, "not_evaluated")
        self.assertEqual(response.inspection_setup.runtime.state, "not_evaluated")
        self.assertEqual(response.inspection_setup.managed_secret.state, "not_evaluated")

    @patch("control_plane.secrets._decrypt_secret_value")
    @patch("control_plane.github_app_identity.GitHubAppIdentity")
    @patch(
        "control_plane.provider_delivery_inspection_profile."
        "resolve_provider_delivery_inspection_profile"
    )
    def test_inspection_setup_returns_only_coherent_recorded_metadata(
        self,
        resolve_profile: object,
        github_app_identity: object,
        decrypt_secret: object,
    ) -> None:
        store = _InspectionReadStore(
            runtime_records=(_inspection_runtime(app_id=2**63 - 1),),
            secret_records=(_inspection_secret(),),
            secret_bindings=(_inspection_binding(),),
        )

        response = self._read(store)

        self.assertEqual(response.inspection_setup.state, "metadata_recorded")
        self.assertEqual(response.inspection_setup.runtime.app_id, str(2**63 - 1))
        self.assertEqual(response.inspection_setup.runtime.recorded_at, "2026-09-12T12:00:00Z")
        self.assertEqual(response.inspection_setup.managed_secret.secret_id, "inspection-secret")
        self.assertEqual(
            response.inspection_setup.managed_secret.current_version_id,
            "inspection-version-v1",
        )
        self.assertEqual(store.runtime_metadata_reads, 1)
        self.assertEqual(store.secret_metadata_reads, 1)
        self.assertEqual(store.binding_metadata_reads, 1)
        for forbidden_call in (resolve_profile, github_app_identity, decrypt_secret):
            cast(Mock, forbidden_call).assert_not_called()
        rendered = response.model_dump_json()
        for private_value in (
            PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY,
            "UNRELATED_RUNTIME_VALUE",
            "private provider inspection key",
            "not returned",
        ):
            self.assertNotIn(private_value, rendered)

    def test_inspection_setup_distinguishes_missing_invalid_and_partial_metadata(self) -> None:
        cases = (
            (
                "runtime_missing",
                _InspectionReadStore(
                    secret_records=(_inspection_secret(),),
                    secret_bindings=(_inspection_binding(),),
                ),
                "record_missing",
                "metadata_recorded",
            ),
            (
                "app_id_missing",
                _InspectionReadStore(
                    runtime_records=(_inspection_runtime(include_app_id=False),),
                    secret_records=(_inspection_secret(),),
                    secret_bindings=(_inspection_binding(),),
                ),
                "app_id_missing",
                "metadata_recorded",
            ),
            (
                "app_id_invalid",
                _InspectionReadStore(
                    runtime_records=(_inspection_runtime(app_id=True),),
                    secret_records=(_inspection_secret(),),
                    secret_bindings=(_inspection_binding(),),
                ),
                "app_id_invalid",
                "metadata_recorded",
            ),
            (
                "binding_missing",
                _InspectionReadStore(
                    runtime_records=(_inspection_runtime(),),
                    secret_records=(_inspection_secret(),),
                ),
                "metadata_recorded",
                "binding_missing",
            ),
            (
                "binding_mismatch",
                _InspectionReadStore(
                    runtime_records=(_inspection_runtime(),),
                    secret_records=(_inspection_secret(),),
                    secret_bindings=(_inspection_binding(secret_id="other-secret"),),
                ),
                "metadata_recorded",
                "binding_mismatch",
            ),
        )
        for label, store, runtime_state, secret_state in cases:
            with self.subTest(label=label):
                response = self._read(store)
                self.assertEqual(response.inspection_setup.state, "incomplete")
                self.assertEqual(response.inspection_setup.runtime.state, runtime_state)
                self.assertEqual(response.inspection_setup.managed_secret.state, secret_state)

    def test_inspection_setup_distinguishes_ambiguity_and_unavailable_payloads(self) -> None:
        ambiguous = self._read(
            _InspectionReadStore(
                runtime_records=(_inspection_runtime(), _inspection_runtime(app_id=43)),
                secret_records=(_inspection_secret(), _inspection_secret(secret_id="other")),
                secret_bindings=(_inspection_binding(),),
            )
        )
        unavailable = self._read(
            _InspectionReadStore(
                runtime_error=RuntimeError("private runtime database failure"),
                secret_error=ValueError("private malformed secret payload"),
            )
        )

        self.assertEqual(ambiguous.inspection_setup.state, "incomplete")
        self.assertEqual(ambiguous.inspection_setup.runtime.state, "record_ambiguous")
        self.assertEqual(ambiguous.inspection_setup.managed_secret.state, "secret_ambiguous")
        self.assertEqual(unavailable.inspection_setup.state, "unavailable")
        self.assertEqual(unavailable.inspection_setup.runtime.state, "unavailable")
        self.assertEqual(unavailable.inspection_setup.managed_secret.state, "secret_unreadable")
        self.assertNotIn("private runtime database failure", unavailable.model_dump_json())
        self.assertNotIn("private malformed secret payload", unavailable.model_dump_json())

    def test_inspection_setup_missing_version_pointer_does_not_expose_partial_ids(self) -> None:
        secret_without_pointer = _inspection_secret().model_copy(update={"current_version_id": ""})
        response = self._read(
            _InspectionReadStore(
                runtime_records=(_inspection_runtime(),),
                secret_records=(secret_without_pointer,),
                secret_bindings=(_inspection_binding(),),
            )
        )

        self.assertEqual(response.inspection_setup.state, "incomplete")
        metadata = response.inspection_setup.managed_secret
        self.assertEqual(metadata.state, "version_pointer_missing")
        self.assertIsNone(metadata.secret_id)
        self.assertIsNone(metadata.binding_id)
        self.assertIsNone(metadata.current_version_id)

    def test_selects_unique_latest_tracked_inventory_and_configured_branches(self) -> None:
        older = _inventory(repository_id=101, repository="Example/Alpha")
        current = _inventory(
            repository_id=101, repository="Example/Alpha", revision=2, reason="current"
        )
        retired = _inventory(repository_id=202, repository="example/retired")
        retired_current = _inventory(
            repository_id=202,
            repository="example/retired",
            revision=2,
            state="retired",
            reason="retired",
        )
        store = _ReadStore(
            inventories=(
                retired_current,
                older,
                retired,
                current,
                _inventory(repository_id=303, repository="example/without-branches"),
            ),
            merge_policies=(
                _merge_policy_with_branches(
                    repository="Example/Alpha", branches=("release", "main")
                ),
            ),
        )

        response = self._read(store)

        self.assertEqual(response.inventory_state, "complete")
        self.assertEqual(response.merge_policy_state, "available")
        self.assertEqual(len(response.repositories), 2)
        self.assertEqual(response.repositories[0].record_id, current.record_id)
        self.assertEqual(response.repositories[0].configured_branches, ("main", "release"))
        self.assertEqual(response.repositories[1].configured_branches, ())
        self.assertIn("configured_branches_missing", {item.code for item in response.diagnostics})
        self.assertNotIn("example/retired", {item.repository for item in response.repositories})
        self.assertEqual(store.inventory_reads, 1)
        self.assertEqual(store.merge_policy_reads, 1)

    def test_valid_offset_timestamp_preserves_available_merge_policy(self) -> None:
        payload = _merge_policy_with_branches(
            repository="example/alpha", branches=("main",)
        ).model_dump(mode="json")
        payload["updated_at"] = "2026-09-12T14:00:00+02:00"
        policy = MergeTrainPolicyRecord.model_validate(payload)

        response = self._read(
            _ReadStore(
                inventories=(_inventory(repository_id=101, repository="example/alpha"),),
                merge_policies=(policy,),
            )
        )

        self.assertEqual(response.merge_policy_state, "available")
        assert response.merge_policy is not None
        self.assertEqual(
            datetime.fromisoformat(response.merge_policy.updated_at),
            datetime.fromisoformat(policy.updated_at),
        )

    def test_inventory_tie_is_ambiguous_and_retired_latest_never_falls_back(self) -> None:
        tied_a = _inventory(repository_id=101, repository="example/alpha", revision=2)
        tied_b = _inventory(
            repository_id=101,
            repository="example/alpha",
            revision=2,
            reason="conflicting current",
        )
        retired_older = _inventory(repository_id=202, repository="example/retired")
        retired_current = _inventory(
            repository_id=202, repository="example/retired", revision=2, state="retired"
        )

        response = self._read(
            _ReadStore(inventories=(tied_a, tied_b, retired_older, retired_current))
        )

        self.assertEqual(response.inventory_state, "ambiguous")
        self.assertEqual(response.repositories, ())
        self.assertIn("inventory_ambiguous", {item.code for item in response.diagnostics})

    def test_unavailable_and_bounded_sources_are_reported_without_partial_results(self) -> None:
        unavailable = self._read(
            _ReadStore(
                inventory_error=RuntimeError("private database error"),
                merge_policy_error=RuntimeError("private merge error"),
            )
        )
        inventories = tuple(
            _inventory(repository_id=index + 1, repository=f"example/repo-{index}")
            for index in range(MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS + 1)
        )
        truncated_inventory = self._read(_ReadStore(inventories=inventories))
        template = build_test_merge_train_policy().policies[0]
        oversized_policy = MergeTrainPolicy(
            policies=tuple(
                template.model_copy(update={"base_branch": f"branch-{index}"})
                for index in range(MAX_AUTHORIZATION_INPUT_MERGE_TARGETS + 1)
            )
        )
        truncated_merge = self._read(
            _ReadStore(
                merge_policies=(
                    MergeTrainPolicyRecord(
                        record_id="oversized-policy",
                        source="test",
                        updated_at="2026-09-12T12:00:00Z",
                        policy=oversized_policy,
                    ),
                )
            )
        )

        self.assertEqual(unavailable.inventory_state, "unavailable")
        self.assertEqual(unavailable.merge_policy_state, "unavailable")
        self.assertNotIn("private database error", unavailable.model_dump_json())
        self.assertNotIn("private merge error", unavailable.model_dump_json())
        self.assertEqual(truncated_inventory.inventory_state, "truncated")
        self.assertEqual(truncated_inventory.repositories, ())
        self.assertEqual(truncated_merge.merge_policy_state, "truncated")
        self.assertIsNone(truncated_merge.merge_policy)

    def test_missing_and_ambiguous_merge_policy_never_invent_branches(self) -> None:
        inventory = _inventory(repository_id=101, repository="example/alpha")
        missing = self._read(_ReadStore(inventories=(inventory,)))
        ambiguous = self._read(
            _ReadStore(
                inventories=(inventory,),
                merge_policies=(
                    build_test_merge_train_policy_record(record_id="policy-one"),
                    build_test_merge_train_policy_record(record_id="policy-two"),
                ),
            )
        )

        self.assertEqual(missing.merge_policy_state, "missing")
        self.assertEqual(missing.repositories[0].configured_branches, ())
        self.assertEqual(ambiguous.merge_policy_state, "ambiguous")
        self.assertIsNone(ambiguous.merge_policy)
        self.assertEqual(ambiguous.repositories[0].configured_branches, ())

    def test_filtered_non_active_merge_policy_record_is_unavailable(self) -> None:
        inconsistent_store = _ReadStore(
            merge_policies=(
                build_test_merge_train_policy_record().model_copy(update={"status": "superseded"}),
            )
        )

        def ignore_status(*, status: str = "", limit: int | None = None) -> tuple[object, ...]:
            _ = status
            records = inconsistent_store.merge_policies
            return records if limit is None else records[:limit]

        inconsistent_store.list_merge_train_policy_records = ignore_status  # type: ignore[method-assign]
        response = self._read(inconsistent_store)

        self.assertEqual(response.merge_policy_state, "unavailable")
        self.assertIsNone(response.merge_policy)

    def test_duplicate_repository_branch_pair_is_ambiguous(self) -> None:
        repository_policy = build_test_merge_train_policy().policies[0]
        invalid_policy = MergeTrainPolicy.model_construct(
            schema_version=1,
            policies=(
                repository_policy,
                repository_policy.model_copy(
                    update={
                        "repository": repository_policy.repository.upper(),
                        "blocked_label": "other-blocked",
                    }
                ),
            ),
        )
        invalid_record = MergeTrainPolicyRecord.model_construct(
            schema_version=1,
            record_id="duplicate-policy",
            status="active",
            source="test",
            updated_at="2026-09-12T12:00:00Z",
            policy_sha256="0" * 64,
            policy=invalid_policy,
        )

        response = self._read(_ReadStore(merge_policies=(invalid_record,)))

        self.assertEqual(response.merge_policy_state, "ambiguous")
        self.assertIsNone(response.merge_policy)


class OrdinaryAgentDeliveryAuthorizationInputHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_get_returns_minimal_read_only_projection(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = _policy()
            policy_record = store.seed_authz_policy_if_absent(_policy_record(policy))
            inventory = _inventory(repository_id=101, repository="example/alpha")
            store.write_repository_inventory_record(inventory)
            store.write_merge_train_policy_record(
                _merge_policy_with_branches(
                    repository="example/alpha", branches=("release", "main")
                )
            )
            app = _app(
                store=store,
                runtime_policy=policy,
                policy_record_reader=lambda: policy_record,
            )

            async with lifespan_client(app) as client:
                response = await client.get(_ROUTE)

            operation_records = store.list_privileged_operation_records(limit=None)
            persisted_inventories = store.list_repository_inventory_records(limit=None)
            persisted_policies = store.list_authz_policy_records(status="active", limit=None)
            store.close()

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["authorization_policy"]["record_id"], policy_record.record_id)
        self.assertEqual(payload["authorization_policy"]["schema_version"], 2)
        self.assertEqual(payload["repositories"][0]["configured_branches"], ["main", "release"])
        self.assertEqual(operation_records, ())
        self.assertEqual(persisted_inventories, (inventory,))
        self.assertEqual(persisted_policies, (policy_record,))
        rendered = json.dumps(payload, sort_keys=True)
        for sensitive_field in (
            "repository_owner_id",
            "source",
            "reason",
            "enqueue_label",
            "github_token",
            "principal_id",
        ):
            self.assertNotIn(sensitive_field, rendered)

    async def test_terminal_and_lifecycle_only_callers_are_denied_before_source_reads(self) -> None:
        lifecycle_policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_humans": [
                    {
                        "managed_set_id": ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
                        "managed_rule_id": ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID,
                        "github_ids": [123],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": list(ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS),
                    }
                ],
            }
        )
        cases = (
            (
                "terminal",
                _policy(),
                TerminalAgentIdentity(subject="agent:planner", token_label="planner"),
            ),
            (
                "local_admin",
                _policy(),
                LocalAdminIdentity(subject="admin:operator", token_label="operator"),
            ),
            ("lifecycle_only", lifecycle_policy, _human()),
        )
        with (
            patch(
                "control_plane.http_routes.privileged_operations."
                "read_ordinary_agent_delivery_authorization_candidate_inputs"
            ) as read_inputs,
            patch("control_plane.secrets._decrypt_secret_value") as decrypt_secret,
            patch("control_plane.github_app_identity.GitHubAppIdentity") as github_app_identity,
        ):
            for label, policy, identity in cases:
                with self.subTest(label=label):
                    store = _InspectionReadStore()
                    app = _app(
                        store=store,
                        runtime_policy=policy,
                        persisted_policy=_policy(),
                        identity=identity,
                    )
                    async with lifespan_client(app) as client:
                        response = await client.get(_ROUTE)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["detail"]["code"], "authorization_denied")
                    self.assertEqual(store.inventory_reads, 0)
                    self.assertEqual(store.merge_policy_reads, 0)
                    self.assertEqual(store.runtime_metadata_reads, 0)
                    self.assertEqual(store.secret_metadata_reads, 0)
                    self.assertEqual(store.binding_metadata_reads, 0)
            read_inputs.assert_not_called()
            decrypt_secret.assert_not_called()
            github_app_identity.assert_not_called()

    async def test_fresh_database_policy_must_retain_strict_administration(self) -> None:
        persisted_payload = _policy().model_dump(mode="json")
        persisted_payload["github_humans"][1]["actions"].remove("authz_policy_grant.write")
        persisted_policy = LaunchplaneAuthzPolicy.model_validate(persisted_payload)
        store = _InspectionReadStore()
        app = _app(
            store=store,
            runtime_policy=_policy(),
            persisted_policy=persisted_policy,
        )

        async with lifespan_client(app) as client:
            response = await client.get(_ROUTE)

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"]["code"], "authorization_denied")
        self.assertEqual(store.inventory_reads, 0)
        self.assertEqual(store.merge_policy_reads, 0)
        self.assertEqual(store.runtime_metadata_reads, 0)
        self.assertEqual(store.secret_metadata_reads, 0)
        self.assertEqual(store.binding_metadata_reads, 0)

    async def test_non_active_database_policy_is_unavailable_before_source_reads(self) -> None:
        store = _InspectionReadStore()
        superseded_record = _policy_record(_policy()).model_copy(update={"status": "superseded"})
        app = _app(
            store=store,
            runtime_policy=_policy(),
            policy_record_reader=lambda: superseded_record,
        )

        async with lifespan_client(app) as client:
            response = await client.get(_ROUTE)

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"]["code"], "authz_policy_unavailable")
        self.assertEqual(store.inventory_reads, 0)
        self.assertEqual(store.merge_policy_reads, 0)
        self.assertEqual(store.runtime_metadata_reads, 0)
        self.assertEqual(store.secret_metadata_reads, 0)
        self.assertEqual(store.binding_metadata_reads, 0)

    async def test_openapi_exposes_parameterless_human_get(self) -> None:
        app = _app(store=_ReadStore(), runtime_policy=_policy())

        operation = app.openapi()["paths"][_ROUTE]["get"]

        self.assertEqual(
            operation["operationId"],
            "read_ordinary_agent_delivery_authorization_candidate_inputs",
        )
        self.assertNotIn("requestBody", operation)
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse",
        )


class MergeTrainPolicyBoundedReadTests(unittest.TestCase):
    def test_postgres_limit_bounds_payload_decoding(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            for index in range(3):
                store.write_merge_train_policy_record(
                    build_test_merge_train_policy_record(
                        record_id=f"superseded-policy-{index}",
                        updated_at=f"2026-09-12T12:0{index}:00Z",
                    ).model_copy(update={"status": "superseded"})
                )
            with patch.object(store, "_read_payload", wraps=store._read_payload) as read_payload:
                records = store.list_merge_train_policy_records(status="superseded", limit=2)
            store.close()

        self.assertEqual(len(records), 2)
        self.assertEqual(read_payload.call_count, 2)


class ProviderDeliveryInspectionSetupQueryTests(unittest.TestCase):
    def test_postgres_queries_apply_exact_filters_and_two_row_bounds(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_runtime_environment_record(_inspection_runtime())
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="global",
                    env={PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY: 99},
                    updated_at="2026-09-12T12:01:00Z",
                )
            )
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context="launchplane",
                    instance="other",
                    env={PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY: 100},
                    updated_at="2026-09-12T12:02:00Z",
                )
            )
            for index in range(3):
                store.write_secret_record(
                    _inspection_secret(secret_id=f"inspection-secret-{index}").model_copy(
                        update={"name": f"inspection key {index}"}
                    )
                )
                store.write_secret_binding(
                    _inspection_binding(secret_id=f"inspection-secret-{index}").model_copy(
                        update={"binding_id": f"inspection-binding-{index}"}
                    )
                )
            store.write_secret_record(
                _inspection_secret(secret_id="other-integration-secret").model_copy(
                    update={"integration": "other_integration", "name": "other integration"}
                )
            )
            store.write_secret_binding(
                _inspection_binding().model_copy(
                    update={
                        "binding_id": "other-key-binding",
                        "binding_key": "other_key",
                    }
                )
            )

            runtimes = store.list_provider_delivery_inspection_setup_runtime_records()
            secrets = store.list_provider_delivery_inspection_setup_secret_records()
            bindings = store.list_provider_delivery_inspection_setup_secret_bindings()
            store.close()

        self.assertEqual(runtimes, (_inspection_runtime(),))
        self.assertEqual(len(secrets), 2)
        self.assertEqual(len(bindings), 2)
        self.assertTrue(
            all(
                record.scope == "context"
                and record.context == "launchplane"
                and not record.instance
                and record.integration == PROVIDER_DELIVERY_INSPECTION_INTEGRATION
                and record.status == "configured"
                for record in secrets
            )
        )
        self.assertTrue(
            all(
                binding.context == "launchplane"
                and not binding.instance
                and binding.integration == PROVIDER_DELIVERY_INSPECTION_INTEGRATION
                and binding.binding_key == "private_key"
                and binding.status == "configured"
                for binding in bindings
            )
        )
