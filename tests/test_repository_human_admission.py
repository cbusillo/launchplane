import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.repository_human_admission import (
    TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,
    RepositoryHumanManagerDelegation,
    RepositoryHumanRolePolicyStatus,
    RepositoryHumanRolePolicyRecord,
    TenantTechnicalHumanWaiverAction,
    repository_human_role_policy_digest,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeCandidate,
    TenantRepositoryClassificationRecord,
)
from control_plane.repository_human_admission import (
    RepositoryHumanRolePolicyConflictError,
    RepositoryHumanRolePolicySequenceError,
    TenantTechnicalHumanWaiverApplyEnvelope,
    TenantTechnicalHumanWaiverAuthorizationError,
    TenantTechnicalHumanWaiverExpectedAuthority,
    TenantTechnicalHumanWaiverStaleAuthorityError,
    apply_tenant_technical_human_waiver,
    apply_repository_human_role_policy,
    capture_tenant_technical_human_waiver_event,
    get_repository_human_role_policy_read_model,
    manager_role_policy_provenance,
    plan_repository_human_role_policy_apply,
    plan_repository_human_role_policy_append,
    technical_human_waiver_path_result,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
)
from control_plane.storage.filesystem import FilesystemRecordStore


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
    def test_role_policy_digest_ignores_lifecycle_status(self) -> None:
        active = _role_policy(repository_owner_ids=(301,))
        superseded = _role_policy(
            repository_owner_ids=(301,),
            status="superseded",
        )

        self.assertEqual(active.record_id, superseded.record_id)
        self.assertEqual(active.role_policy_digest, superseded.role_policy_digest)
        self.assertEqual(
            repository_human_role_policy_digest(superseded),
            active.role_policy_digest,
        )

    def test_role_policy_append_plan_supersedes_current_without_digest_churn(
        self,
    ) -> None:
        revision_1 = _role_policy(repository_owner_ids=(301,))
        revision_2 = _role_policy(
            repository_owner_ids=(302,),
            revision=2,
            supersedes_record_id=revision_1.record_id,
        )

        plan = plan_repository_human_role_policy_append(
            records=(revision_1,),
            record=revision_2,
        )

        self.assertEqual(plan.status, "written")
        self.assertEqual(plan.current_record, revision_1)
        self.assertIsNotNone(plan.superseded_current_record)
        superseded_current = plan.superseded_current_record
        assert superseded_current is not None
        self.assertEqual(superseded_current.status, "superseded")
        self.assertEqual(superseded_current.record_id, revision_1.record_id)
        self.assertEqual(
            superseded_current.role_policy_digest,
            revision_1.role_policy_digest,
        )

        replay = plan_repository_human_role_policy_append(
            records=(superseded_current, revision_2),
            record=revision_2,
        )
        self.assertEqual(replay.status, "replayed")

        historical_replay = plan_repository_human_role_policy_append(
            records=(superseded_current, revision_2),
            record=revision_1,
        )
        self.assertEqual(historical_replay.status, "replayed")

        with self.assertRaises(RepositoryHumanRolePolicyConflictError):
            plan_repository_human_role_policy_append(
                records=(superseded_current, revision_2),
                record=_role_policy(
                    repository_owner_ids=(301,),
                    reason="different historical payload",
                ),
            )

        with self.assertRaises(RepositoryHumanRolePolicyConflictError):
            plan_repository_human_role_policy_append(
                records=(revision_1,),
                record=revision_1.model_copy(update={"status": "superseded"}),
            )

    def test_role_policy_current_read_model_is_scope_bound_and_ambiguous_safe(
        self,
    ) -> None:
        class AmbiguousReadStore:
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
                del repository_id, repository_owner_id, repository, product, context, status, limit
                revision_1 = _role_policy(repository_owner_ids=(301,))
                revision_2 = _role_policy(
                    repository_owner_ids=(302,),
                    revision=2,
                    supersedes_record_id=revision_1.record_id,
                )
                return (revision_1, revision_2)

        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            missing = get_repository_human_role_policy_read_model(
                repository_id=REPOSITORY_ID,
                product=PRODUCT,
                context=CONTEXT,
                store=store,
            )
            revision_1 = _role_policy(repository_owner_ids=(301,))
            revision_2 = _role_policy(
                repository_owner_ids=(302,),
                revision=2,
                supersedes_record_id=revision_1.record_id,
            )
            store.write_repository_human_role_policy_record(revision_1)
            store.write_repository_human_role_policy_record(revision_2)
            available = get_repository_human_role_policy_read_model(
                repository_id=REPOSITORY_ID,
                product=PRODUCT,
                context=CONTEXT,
                store=store,
            )

        ambiguous = get_repository_human_role_policy_read_model(
            repository_id=REPOSITORY_ID,
            product=PRODUCT,
            context=CONTEXT,
            store=AmbiguousReadStore(),
        )

        self.assertEqual(missing.status, "missing")
        self.assertIsNone(missing.current_record)
        self.assertEqual(available.status, "available")
        self.assertEqual(available.current_record, revision_2)
        self.assertEqual(available.history_count, 2)
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertIsNone(ambiguous.current_record)

    def test_role_policy_apply_requires_explicit_expected_tip_id_and_digest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            revision_1 = _role_policy(repository_owner_ids=(301,))
            revision_2 = _role_policy(
                repository_owner_ids=(302,),
                revision=2,
                supersedes_record_id=revision_1.record_id,
            )

            applied_1 = apply_repository_human_role_policy(
                store=store,
                record=revision_1,
                mode="apply",
            )

            with self.assertRaisesRegex(ValueError, "expected current record ID and digest"):
                apply_repository_human_role_policy(
                    store=store,
                    record=revision_2,
                    expected_current_record_id=revision_1.record_id,
                    mode="apply",
                )
            with self.assertRaises(RepositoryHumanRolePolicyConflictError):
                apply_repository_human_role_policy(
                    store=store,
                    record=revision_2,
                    expected_current_record_id="wrong-record-id",
                    expected_current_role_policy_digest=revision_1.role_policy_digest,
                    mode="apply",
                )
            with self.assertRaises(RepositoryHumanRolePolicyConflictError):
                apply_repository_human_role_policy(
                    store=store,
                    record=revision_2,
                    expected_current_record_id=revision_1.record_id,
                    expected_current_role_policy_digest="f" * 64,
                    mode="apply",
                )

            applied_2 = apply_repository_human_role_policy(
                store=store,
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
                mode="apply",
            )
            replay_2 = apply_repository_human_role_policy(
                store=store,
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
                mode="apply",
            )

        self.assertEqual(applied_1.status, "applied")
        self.assertEqual(applied_2.status, "applied")
        self.assertEqual(replay_2.status, "replayed")

    def test_role_policy_apply_rejects_inactive_candidate_and_sequence_gaps(
        self,
    ) -> None:
        revision_1 = _role_policy(repository_owner_ids=(301,))
        revision_2 = _role_policy(
            repository_owner_ids=(302,),
            revision=2,
            supersedes_record_id=revision_1.record_id,
        )
        with self.assertRaisesRegex(ValueError, "active candidate"):
            plan_repository_human_role_policy_apply(
                records=(),
                record=revision_1.model_copy(update={"status": "superseded"}),
                expected_current_record_id="",
                expected_current_role_policy_digest="",
            )
        with self.assertRaises(RepositoryHumanRolePolicySequenceError):
            plan_repository_human_role_policy_apply(
                records=(revision_1,),
                record=revision_2.model_copy(update={"role_policy_revision": 3}),
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
            )

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
            recorded_at=OCCURRED_AT,
            expires_at="2026-07-31T13:00:00Z",
        )

        self.assertEqual(result.path_result.state, "satisfied")
        self.assertEqual(result.record.binding.head_sha, HEAD_SHA)
        self.assertEqual(result.record.binding.product, PRODUCT)
        self.assertEqual(result.record.binding.context, CONTEXT)
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
                recorded_at=OCCURRED_AT,
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
                recorded_at=OCCURRED_AT,
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
                recorded_at=OCCURRED_AT,
            )

    def test_waiver_requires_numeric_managed_rule_not_login_only(self) -> None:
        role_policy = _role_policy(repository_owner_ids=(301,))
        login_only_policy = _authz_policy_record(
            github_ids=(),
            extra_rule={
                "managed_set_id": "tenant-human.login-only",
                "managed_rule_id": "technical-waiver-login-only",
                "logins": ["human-301"],
                "roles": ["read_only"],
                "products": [PRODUCT],
                "contexts": [CONTEXT],
                "actions": [TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION],
            },
        )

        with self.assertRaisesRegex(
            TenantTechnicalHumanWaiverAuthorizationError,
            "caller GitHub ID",
        ):
            capture_tenant_technical_human_waiver_event(
                identity=_human(github_id=301),
                candidate=_candidate(),
                classification=_classification(),
                role_policy_record=role_policy,
                authz_policy_record=login_only_policy,
                action="created",
                occurred_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-login-only",
                reason="Login-only managed rule must fail closed.",
                recorded_at=OCCURRED_AT,
            )

    def test_waiver_numeric_rule_ignores_mutable_login_metadata(self) -> None:
        role_policy = _role_policy(repository_owner_ids=(301,))
        renamed_human = _human(
            github_id=301,
            login="new-human-301",
            organizations=frozenset({"new-org"}),
            teams=frozenset({"new-org/new-team"}),
            role="admin",
        )

        event = capture_tenant_technical_human_waiver_event(
            identity=renamed_human,
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=_authz_policy_record(
                github_ids=(),
                extra_rule={
                    "managed_set_id": "tenant-human.numeric-only",
                    "managed_rule_id": "technical-waiver-stale-login",
                    "github_ids": [301],
                    "logins": ["old-human-301"],
                    "organizations": ["new-org"],
                    "teams": ["new-org/new-team"],
                    "roles": ["admin"],
                    "products": [PRODUCT],
                    "contexts": [CONTEXT],
                    "actions": [TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION],
                },
            ),
            action="created",
            occurred_at=OCCURRED_AT,
            source_event_kind="github_issue_comment",
            source_event_id="comment-renamed-human",
            reason="Numeric GitHub ID remains the waiver authority.",
            recorded_at=OCCURRED_AT,
        ).record

        self.assertEqual(event.authorization.author_github_id, 301)
        self.assertEqual(event.authorization.author_login, "new-human-301")

    def test_waiver_requires_exactly_one_numeric_managed_rule(self) -> None:
        role_policy = _role_policy(repository_owner_ids=(301,))
        duplicate_policy = _authz_policy_record(
            github_ids=(301,),
            extra_rule={
                "managed_set_id": "tenant-human.example",
                "managed_rule_id": "technical-waiver-duplicate",
                "github_ids": [301],
                "roles": ["read_only"],
                "products": [PRODUCT],
                "contexts": [CONTEXT],
                "actions": [TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION],
            },
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
                authz_policy_record=duplicate_policy,
                action="created",
                occurred_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-duplicate-rule",
                reason="Duplicate managed rules must fail closed.",
                recorded_at=OCCURRED_AT,
            )

    def test_apply_envelope_does_not_accept_occurred_at(self) -> None:
        with self.assertRaises(ValueError):
            TenantTechnicalHumanWaiverApplyEnvelope.model_validate(
                _waiver_apply_payload(action="created") | {"occurred_at": "2001-01-01T00:00:00Z"}
            )

    def test_waiver_apply_plans_create_and_revoke_lifecycle(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            classification = _classification()
            role_policy = _role_policy(repository_owner_ids=(301,))
            authz_policy = _authz_policy_record(github_ids=(301,))
            store.write_tenant_repository_classification_record(classification)
            store.write_repository_human_role_policy_record(role_policy)
            _write_authz_policy_record(store, authz_policy)

            create_result = apply_tenant_technical_human_waiver(
                store=store,
                identity=_human(github_id=301),
                envelope=TenantTechnicalHumanWaiverApplyEnvelope.model_validate(
                    _waiver_apply_payload(action="created")
                ),
                observed_at=OCCURRED_AT,
            )
            created_event = capture_tenant_technical_human_waiver_event(
                identity=_human(github_id=301),
                candidate=_candidate(),
                classification=classification,
                role_policy_record=role_policy,
                authz_policy_record=authz_policy,
                action="created",
                occurred_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-created",
                reason="Owner reviewed exact technical waiver.",
                recorded_at=OCCURRED_AT,
            ).record
            store.write_tenant_technical_human_waiver_event_record(created_event)
            revoke_result = apply_tenant_technical_human_waiver(
                store=store,
                identity=_human(github_id=301),
                envelope=TenantTechnicalHumanWaiverApplyEnvelope.model_validate(
                    _waiver_apply_payload(
                        action="revoked",
                        source_event_id="comment-revoked",
                        expected_current={
                            "waiver_id": created_event.waiver_id,
                            "event_digest": created_event.event_digest,
                        },
                    )
                ),
                observed_at="2026-07-31T12:10:00Z",
            )

        self.assertEqual(create_result.status, "would_apply")
        self.assertEqual(create_result.path_result.state, "satisfied")
        self.assertTrue(create_result.dry_run)
        self.assertEqual(revoke_result.status, "would_apply")
        self.assertEqual(revoke_result.path_result.state, "denied")
        self.assertTrue(revoke_result.dry_run)

    def test_waiver_apply_rejects_superseded_repository_classification(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            classification = _classification()
            replacement_classification = _classification(
                product="replacement-product",
                context="replacement-context",
                classification_kind="engineering",
                classification_revision=2,
                classified_at="2026-07-31T11:05:00Z",
                supersedes_record_id=classification.record_id,
            )
            role_policy = _role_policy(repository_owner_ids=(301,))
            authz_policy = _authz_policy_record(github_ids=(301,))
            store.write_tenant_repository_classification_record(classification)
            store.write_tenant_repository_classification_record(replacement_classification)
            store.write_repository_human_role_policy_record(role_policy)
            _write_authz_policy_record(store, authz_policy)

            with self.assertRaisesRegex(
                TenantTechnicalHumanWaiverStaleAuthorityError,
                "classification does not match candidate",
            ):
                apply_tenant_technical_human_waiver(
                    store=store,
                    identity=_human(github_id=301),
                    envelope=TenantTechnicalHumanWaiverApplyEnvelope.model_validate(
                        _waiver_apply_payload(action="created")
                    ),
                    observed_at="2026-07-31T12:00:00Z",
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
            recorded_at=OCCURRED_AT,
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
            recorded_at="2026-07-31T12:10:00Z",
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
            recorded_at="2026-07-31T12:10:00Z",
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

    def test_waiver_rejects_future_occurrence_and_classification_scope_drift(self) -> None:
        role_policy = _role_policy(repository_owner_ids=(301,))
        authz_policy = _authz_policy_record(github_ids=(301,))

        with self.assertRaisesRegex(ValueError, "recorded before it occurred"):
            capture_tenant_technical_human_waiver_event(
                identity=_human(github_id=301),
                candidate=_candidate(),
                classification=_classification(),
                role_policy_record=role_policy,
                authz_policy_record=authz_policy,
                action="created",
                occurred_at="2026-07-31T12:10:00Z",
                recorded_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-future",
                reason="Future evidence must fail closed.",
            )

        with self.assertRaisesRegex(ValueError, "classification does not match"):
            capture_tenant_technical_human_waiver_event(
                identity=_human(github_id=301),
                candidate=_candidate(),
                classification=_classification(context="staging"),
                role_policy_record=role_policy,
                authz_policy_record=authz_policy,
                action="created",
                occurred_at=OCCURRED_AT,
                recorded_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-scope",
                reason="Wrong classification scope must fail closed.",
            )

    def test_same_timestamp_revocation_wins(self) -> None:
        role_policy = _role_policy(repository_owner_ids=(301,))
        authz_policy = _authz_policy_record(github_ids=(301,))
        events = tuple(
            capture_tenant_technical_human_waiver_event(
                identity=_human(github_id=301),
                candidate=_candidate(),
                classification=_classification(),
                role_policy_record=role_policy,
                authz_policy_record=authz_policy,
                action=cast(TenantTechnicalHumanWaiverAction, action),
                occurred_at=OCCURRED_AT,
                recorded_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id=source_event_id,
                reason=reason,
            ).record
            for action, source_event_id, reason in (
                ("created", "comment-create", "Owner approved technical handling."),
                ("revoked", "comment-revoke", "Owner revoked technical handling."),
            )
        )

        result = technical_human_waiver_path_result(
            candidate=_candidate(),
            classification=_classification(),
            role_policy_record=role_policy,
            authz_policy_record=authz_policy,
            events=events,
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(result.state, "denied")

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
                product=PRODUCT,
                context=CONTEXT,
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
                product=PRODUCT,
                context=CONTEXT,
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
            product=PRODUCT,
            context=CONTEXT,
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
                product=PRODUCT,
                context=CONTEXT,
                github_id=504,
                evaluated_at=EVALUATED_AT,
            )

    def test_role_policy_effective_time_and_delegator_fail_closed(self) -> None:
        future_policy = _role_policy(
            repository_owner_ids=(301,),
            effective_at="2026-07-31T13:00:00Z",
        )
        with self.assertRaisesRegex(ValueError, "not effective yet"):
            manager_role_policy_provenance(
                role_policy_record=future_policy,
                repository_id=REPOSITORY_ID,
                repository_owner_id=REPOSITORY_OWNER_ID,
                repository=REPOSITORY,
                product=PRODUCT,
                context=CONTEXT,
                github_id=501,
                evaluated_at=EVALUATED_AT,
            )

        unauthorized_delegation = RepositoryHumanManagerDelegation(
            delegated_manager_github_id=503,
            delegated_by_github_id=999,
            starts_at="2026-07-31T11:00:00Z",
            expires_at="2026-07-31T13:00:00Z",
            source_event_kind="github_issue_comment",
            source_event_id="delegation-unauthorized",
            reason="Unauthorized delegation must fail.",
        )
        with self.assertRaisesRegex(ValueError, "granted by a primary or backup manager"):
            _role_policy(
                repository_owner_ids=(301,),
                manager_delegations=(unauthorized_delegation,),
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
    status: RepositoryHumanRolePolicyStatus = "active",
    revision: int = 1,
    effective_at: str = "2026-07-31T11:00:00Z",
    supersedes_record_id: str | None = None,
    reason: str = "test role policy",
) -> RepositoryHumanRolePolicyRecord:
    return RepositoryHumanRolePolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        product=PRODUCT,
        context=CONTEXT,
        status=status,
        role_policy_revision=revision,
        repository_owner_github_ids=repository_owner_ids,
        manager_primary_github_ids=manager_primary_ids,
        manager_backup_github_ids=manager_backup_ids,
        manager_delegations=manager_delegations,
        effective_at=effective_at,
        source="test:role-policy",
        reason=reason,
        supersedes_record_id=supersedes_record_id,
    )


def _authz_policy_record(
    *,
    github_ids: tuple[int, ...],
    extra_rule: dict[str, object] | None = None,
) -> LaunchplaneAuthzPolicyRecord:
    rules: list[GitHubHumanPolicyRule] = []
    if github_ids:
        rules.append(
            GitHubHumanPolicyRule(
                managed_set_id="tenant-human.example",
                managed_rule_id="technical-waiver",
                github_ids=github_ids,
                roles=("read_only",),
                products=(PRODUCT,),
                contexts=(CONTEXT,),
                actions=(TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,),
            )
        )
    if extra_rule is not None:
        rules.append(GitHubHumanPolicyRule.model_validate(extra_rule))
    policy = LaunchplaneAuthzPolicy(schema_version=2, github_humans=tuple(rules))
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


def _write_authz_policy_record(
    store: FilesystemRecordStore,
    record: LaunchplaneAuthzPolicyRecord,
) -> None:
    record_path = store.state_dir / "launchplane_authz_policies" / f"{record.record_id}.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")


def _waiver_apply_payload(
    *,
    action: str,
    source_event_id: str | None = None,
    expected_current: dict[str, object] | None = None,
) -> dict[str, object]:
    classification = _classification()
    role_policy = _role_policy(repository_owner_ids=(301,))
    authz_policy = _authz_policy_record(github_ids=(301,))
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": "dry_run",
        "action": action,
        "candidate": _candidate().model_dump(mode="json"),
        "expected_authority": TenantTechnicalHumanWaiverExpectedAuthority(
            classification_record_id=classification.record_id,
            classification_digest=classification.classification_digest,
            role_policy_record_id=role_policy.record_id,
            role_policy_digest=role_policy.role_policy_digest,
            authz_policy_record_id=authz_policy.record_id,
            authz_policy_digest=authz_policy.policy_sha256,
        ).model_dump(mode="json"),
        "source_event_kind": "github_issue_comment",
        "source_event_id": source_event_id or f"comment-{action}",
        "reason": "Owner reviewed exact technical waiver.",
    }
    if expected_current is not None:
        payload["expected_current"] = expected_current
    return payload


def _human(
    *,
    github_id: int,
    login: str = "",
    organizations: frozenset[str] = frozenset(),
    teams: frozenset[str] = frozenset(),
    role: Literal["read_only", "admin"] = "read_only",
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login or f"human-{github_id}",
        github_id=github_id,
        name="Human Example",
        email="human@example.com",
        organizations=organizations,
        teams=teams,
        role=role,
    )


if __name__ == "__main__":
    unittest.main()
