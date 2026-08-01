import unittest

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.repository_human_admission import (
    TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,
    RepositoryHumanManagerDelegation,
    RepositoryHumanRolePolicyRecord,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeCandidate,
    TenantRepositoryClassificationRecord,
)
from control_plane.repository_human_admission import (
    TenantTechnicalHumanWaiverAuthorizationError,
    capture_tenant_technical_human_waiver_event,
    manager_role_policy_provenance,
    technical_human_waiver_path_result,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
)


PRODUCT = "launchplane"
CONTEXT = "production"
REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/tenant-site"
PULL_REQUEST_NUMBER = 17
HEAD_SHA = "a" * 40
OLDER_HEAD_SHA = "b" * 40
OCCURRED_AT = "2026-07-31T12:00:00Z"
EVALUATED_AT = "2026-07-31T12:05:00Z"


class RepositoryHumanAdmissionTests(unittest.TestCase):
    def test_owner_waiver_captures_exact_managed_authz_and_satisfies_path(self) -> None:
        role_policy = _role_policy(repository_owner_ids=(301,))
        authz_policy = _authz_policy_record(github_ids=(301,))

        result = capture_tenant_technical_human_waiver_event(
            identity=_human(github_id=301),
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=authz_policy,
            action="created",
            occurred_at=OCCURRED_AT,
            source_event_kind="github_issue_comment",
            source_event_id="comment-1001",
            reason="Narrow technical change reviewed by repo owner.",
            expires_at="2026-07-31T13:00:00Z",
        )

        self.assertEqual(result.path_result.state, "satisfied")
        self.assertEqual(result.record.binding.head_sha, HEAD_SHA)
        self.assertEqual(result.record.authorization.managed_set_id, "tenant-human.example")
        self.assertEqual(result.record.authorization.managed_rule_id, "technical-waiver")
        self.assertEqual(
            result.record.authorization.action,
            TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,
        )
        self.assertEqual(
            result.record.authorization.role_policy_provenance.authority_kind,
            "repository_owner",
        )

    def test_waiver_requires_human_owner_and_active_authz(self) -> None:
        role_policy = _role_policy(repository_owner_ids=(301,))
        authz_policy = _authz_policy_record(github_ids=(301,))

        with self.assertRaisesRegex(
            TenantTechnicalHumanWaiverAuthorizationError,
            "GitHub human identity",
        ):
            capture_tenant_technical_human_waiver_event(
                identity=TerminalAgentIdentity(subject="agent", token_label="local"),
                candidate=_candidate(),
                classification=_classification(),
                role_policy_record=role_policy,
                authz_policy_record=authz_policy,
                action="created",
                occurred_at=OCCURRED_AT,
                source_event_kind="terminal",
                source_event_id="event-1",
                reason="Agent cannot authorize this.",
            )

        with self.assertRaisesRegex(
            TenantTechnicalHumanWaiverAuthorizationError,
            "repository owner",
        ):
            capture_tenant_technical_human_waiver_event(
                identity=_human(github_id=302),
                candidate=_candidate(),
                classification=_classification(),
                role_policy_record=role_policy,
                authz_policy_record=_authz_policy_record(github_ids=(302,)),
                action="created",
                occurred_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-1002",
                reason="Not an owner.",
            )

        with self.assertRaisesRegex(
            TenantTechnicalHumanWaiverAuthorizationError,
            "exactly one managed authz policy rule",
        ):
            capture_tenant_technical_human_waiver_event(
                identity=_human(github_id=301),
                candidate=_candidate(),
                classification=_classification(),
                role_policy_record=role_policy,
                authz_policy_record=_authz_policy_record(github_ids=(999,)),
                action="created",
                occurred_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-1003",
                reason="No authz rule.",
            )

    def test_waiver_path_is_exact_head_policy_bound_expiring_and_revocable(self) -> None:
        role_policy = _role_policy(repository_owner_ids=(301,))
        authz_policy = _authz_policy_record(github_ids=(301,))
        created = capture_tenant_technical_human_waiver_event(
            identity=_human(github_id=301),
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=authz_policy,
            action="created",
            occurred_at=OCCURRED_AT,
            source_event_kind="github_issue_comment",
            source_event_id="comment-1001",
            reason="Narrow technical change reviewed by repo owner.",
            expires_at="2026-07-31T13:00:00Z",
        ).record

        head_drift = technical_human_waiver_path_result(
            candidate=_candidate(head_sha=OLDER_HEAD_SHA),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=authz_policy,
            events=(created,),
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(head_drift.state, "stale")

        policy_drift = technical_human_waiver_path_result(
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=_role_policy(repository_owner_ids=(301,), revision=2),
            authz_policy_record=authz_policy,
            events=(created,),
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(policy_drift.state, "stale")

        expired = technical_human_waiver_path_result(
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=authz_policy,
            events=(created,),
            evaluated_at="2026-07-31T13:00:00Z",
        )
        self.assertEqual(expired.state, "stale")

        revoked = capture_tenant_technical_human_waiver_event(
            identity=_human(github_id=301),
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=authz_policy,
            action="revoked",
            occurred_at="2026-07-31T12:10:00Z",
            source_event_kind="github_issue_comment",
            source_event_id="comment-1004",
            reason="Owner revoked the waiver.",
        ).record
        revoked_path = technical_human_waiver_path_result(
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=authz_policy,
            events=(created, revoked),
            evaluated_at="2026-07-31T12:11:00Z",
        )
        self.assertEqual(revoked_path.state, "denied")

    def test_future_dated_waiver_is_ignored(self) -> None:
        role_policy = _role_policy(repository_owner_ids=(301,))
        authz_policy = _authz_policy_record(github_ids=(301,))
        future = capture_tenant_technical_human_waiver_event(
            identity=_human(github_id=301),
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=authz_policy,
            action="created",
            occurred_at="2026-07-31T12:10:00Z",
            source_event_kind="github_issue_comment",
            source_event_id="comment-1001",
            reason="Future event should not count yet.",
        ).record

        path = technical_human_waiver_path_result(
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=authz_policy,
            events=(future,),
            evaluated_at=OCCURRED_AT,
        )

        self.assertEqual(path.state, "pending")

    def test_manager_authority_distinguishes_primary_backup_delegated_and_revoked(self) -> None:
        active_delegation = RepositoryHumanManagerDelegation(
            delegated_manager_github_id=503,
            delegated_by_github_id=501,
            starts_at="2026-07-31T11:00:00Z",
            expires_at="2026-07-31T13:00:00Z",
            source_event_kind="github_issue_comment",
            source_event_id="delegation-1",
            reason="Cover manager approval window.",
        )
        revoked_delegation = RepositoryHumanManagerDelegation(
            delegated_manager_github_id=504,
            delegated_by_github_id=501,
            starts_at="2026-07-31T11:00:00Z",
            expires_at="2026-07-31T13:00:00Z",
            revoked_at="2026-07-31T11:30:00Z",
            source_event_kind="github_issue_comment",
            source_event_id="delegation-2",
            reason="Cover manager approval window.",
        )
        role_policy = _role_policy(
            repository_owner_ids=(301,),
            manager_primary_ids=(501,),
            manager_backup_ids=(502,),
            manager_delegations=(active_delegation, revoked_delegation),
        )

        self.assertEqual(
            manager_role_policy_provenance(
                role_policy_record=role_policy,
                repository_id=REPOSITORY_ID,
                repository_owner_id=REPOSITORY_OWNER_ID,
                repository=REPOSITORY,
                github_id=501,
                evaluated_at=EVALUATED_AT,
            ).authority_kind,
            "manager_primary",
        )
        self.assertEqual(
            manager_role_policy_provenance(
                role_policy_record=role_policy,
                repository_id=REPOSITORY_ID,
                repository_owner_id=REPOSITORY_OWNER_ID,
                repository=REPOSITORY,
                github_id=502,
                evaluated_at=EVALUATED_AT,
            ).authority_kind,
            "manager_backup",
        )
        delegated = manager_role_policy_provenance(
            role_policy_record=role_policy,
            repository_id=REPOSITORY_ID,
            repository_owner_id=REPOSITORY_OWNER_ID,
            repository=REPOSITORY,
            github_id=503,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(delegated.authority_kind, "manager_delegated")
        self.assertEqual(delegated.delegation_id, active_delegation.delegation_id)
        with self.assertRaisesRegex(ValueError, "active manager authority"):
            manager_role_policy_provenance(
                role_policy_record=role_policy,
                repository_id=REPOSITORY_ID,
                repository_owner_id=REPOSITORY_OWNER_ID,
                repository=REPOSITORY,
                github_id=504,
                evaluated_at=EVALUATED_AT,
            )


def _candidate(**overrides: object) -> TenantMergeCandidate:
    payload = {
        "product": PRODUCT,
        "context": CONTEXT,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "pull_request_number": PULL_REQUEST_NUMBER,
        "head_sha": HEAD_SHA,
    }
    payload.update(overrides)
    return TenantMergeCandidate.model_validate(payload)


def _classification(**overrides: object) -> TenantRepositoryClassificationRecord:
    payload = {
        "product": PRODUCT,
        "context": CONTEXT,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "classification_kind": "tenant_ui",
        "classification_revision": 1,
        "classified_at": "2026-07-31T11:00:00Z",
        "source": "test",
        "reason": "tenant UI repository",
    }
    payload.update(overrides)
    return TenantRepositoryClassificationRecord.model_validate(payload)


def _role_policy(
    *,
    repository_owner_ids: tuple[int, ...],
    manager_primary_ids: tuple[int, ...] = (501,),
    manager_backup_ids: tuple[int, ...] = (),
    manager_delegations: tuple[RepositoryHumanManagerDelegation, ...] = (),
    revision: int = 1,
) -> RepositoryHumanRolePolicyRecord:
    return RepositoryHumanRolePolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        role_policy_revision=revision,
        repository_owner_github_ids=repository_owner_ids,
        manager_primary_github_ids=manager_primary_ids,
        manager_backup_github_ids=manager_backup_ids,
        manager_delegations=manager_delegations,
        effective_at="2026-07-31T11:00:00Z",
        source="test:role-policy",
        reason="test role policy",
    )


def _authz_policy_record(*, github_ids: tuple[int, ...]) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="tenant-human.example",
                managed_rule_id="technical-waiver",
                github_ids=github_ids,
                roles=("read_only",),
                products=(PRODUCT,),
                contexts=(CONTEXT,),
                actions=(TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,),
            ),
        ),
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        status="active",
        source="test:authz-policy",
        updated_at="2026-07-31T11:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


def _human(*, github_id: int) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=f"human-{github_id}",
        github_id=github_id,
        name="Human Example",
        email="human@example.com",
        organizations=frozenset(),
        teams=frozenset(),
        role="read_only",
    )


if __name__ == "__main__":
    unittest.main()
