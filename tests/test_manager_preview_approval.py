import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.manager_preview_approval import (
    MANAGER_PREVIEW_APPROVAL_READ_ACTION,
    MANAGER_PREVIEW_APPROVAL_WRITE_ACTION,
    ManagerPreviewApprovalDecision,
    ManagerPreviewApprovalEventRecord,
)
from control_plane.contracts.repository_human_admission import (
    RepositoryHumanManagerDelegation,
    RepositoryHumanRolePolicyRecord,
)
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.manager_preview_approval import (
    ManagerPreviewApprovalAuthorizationError,
    ManagerPreviewApprovalEventConflictError,
    ManagerPreviewApprovalEventStore,
    build_current_manager_preview_approval_binding,
    build_manager_preview_approval_system_event,
    capture_manager_preview_approval_authorization,
    evaluate_manager_preview_approval,
    record_manager_preview_approval_event,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


PRODUCT = "example-site"
CONTEXT = "example-site-testing"
REPOSITORY = "example/example-site"
PR_NUMBER = 17
HEAD_SHA = "1" * 40
IMAGE_DIGEST = f"sha256:{'a' * 64}"
OCCURRED_AT = "2026-07-30T12:00:00Z"


class ManagerPreviewApprovalTests(unittest.TestCase):
    def test_records_approval_only_after_exact_manager_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            result = record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(login="renamed-manager"),
                policy_record=_policy_record(),
                product=PRODUCT,
                preview=_preview(),
                generation=_generation(),
                action="approved",
                occurred_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-101",
            )

            self.assertEqual(result.status, "written")
            self.assertEqual(result.record.manager_github_id, 101)
            self.assertEqual(result.record.manager_login, "renamed-manager")
            self.assertEqual(result.record.binding.head_sha, HEAD_SHA)
            self.assertEqual(result.record.binding.artifact_image_digest, IMAGE_DIGEST)
            self.assertEqual(
                store.list_manager_preview_approval_event_records(
                    product=PRODUCT,
                    context=CONTEXT,
                    repository=REPOSITORY,
                    pr_number=PR_NUMBER,
                ),
                (result.record,),
            )

    def test_rejects_actor_not_named_by_stable_github_id(self) -> None:
        with self.assertRaisesRegex(
            ManagerPreviewApprovalAuthorizationError,
            "exactly one managed policy rule",
        ):
            capture_manager_preview_approval_authorization(
                identity=_manager_identity(github_id=202),
                product=PRODUCT,
                context=CONTEXT,
                policy_record=_policy_record(),
                authorized_at=OCCURRED_AT,
            )

    def test_rejects_actor_without_stable_github_numeric_identity(self) -> None:
        with self.assertRaisesRegex(
            ManagerPreviewApprovalAuthorizationError,
            "stable GitHub numeric identity",
        ):
            capture_manager_preview_approval_authorization(
                identity=_manager_identity(github_id=0),
                product=PRODUCT,
                context=CONTEXT,
                policy_record=_policy_record(),
                authorized_at=OCCURRED_AT,
            )

    def test_manager_write_path_rejects_system_actions(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            with self.assertRaisesRegex(ValueError, "manager action"):
                record_manager_preview_approval_event(
                    record_store=store,
                    identity=_manager_identity(),
                    policy_record=_policy_record(),
                    product=PRODUCT,
                    preview=_preview(),
                    generation=_generation(),
                    action="superseded",
                    occurred_at=OCCURRED_AT,
                    source_event_kind="preview_generation",
                    source_event_id="generation-18",
                    reason="A new serving generation replaced this preview.",
                )

    def test_requires_current_verified_runtime_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "runtime identity"):
            build_current_manager_preview_approval_binding(
                product=PRODUCT,
                preview=_preview(),
                generation=_generation(runtime_identity=None),
            )

    def test_rejects_runtime_identity_for_wrong_source_or_environment(self) -> None:
        mismatch_cases = {
            "source": _runtime_identity(source_git_ref="2" * 40),
            "environment": _runtime_identity(environment_kind="stable"),
            "preview_id": _runtime_identity(preview_id="preview-18"),
            "generation": _runtime_identity(preview_generation_id="generation-18"),
        }
        for label, runtime_identity in mismatch_cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "runtime identity"):
                    build_current_manager_preview_approval_binding(
                        product=PRODUCT,
                        preview=_preview(),
                        generation=_generation(runtime_identity=runtime_identity),
                    )

    def test_requires_runtime_identity_preview_id_to_match_binding_preview_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "runtime identity"):
            build_current_manager_preview_approval_binding(
                product=PRODUCT,
                preview=_preview(),
                generation=_generation(runtime_identity=_runtime_identity(preview_id="preview-18")),
            )

    def test_filesystem_replays_identical_event_and_rejects_conflicting_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            event = _approved_event(store)

            self.assertEqual(store.write_manager_preview_approval_event_record(event), "replayed")
            conflicting = ManagerPreviewApprovalEventRecord.model_validate(
                {**event.model_dump(mode="json"), "reason": "Conflicting replay."}
            )
            with self.assertRaises(ManagerPreviewApprovalEventConflictError):
                store.write_manager_preview_approval_event_record(conflicting)

    def test_sql_store_persists_and_replays_append_only_event(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            event = _approved_event(store)
            try:
                self.assertEqual(
                    store.write_manager_preview_approval_event_record(event),
                    "replayed",
                )
                self.assertEqual(
                    store.list_manager_preview_approval_event_records(preview_id="preview-17"),
                    (event,),
                )
                conflicting = ManagerPreviewApprovalEventRecord.model_validate(
                    {**event.model_dump(mode="json"), "reason": "Conflicting replay."}
                )
                with self.assertRaises(ManagerPreviewApprovalEventConflictError):
                    store.write_manager_preview_approval_event_record(conflicting)
            finally:
                store.close()

    def test_decision_tracks_changes_requested_revocation_and_invalidation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            approved = _approved_event(store)
            changes_requested = record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(),
                policy_record=_policy_record(),
                product=PRODUCT,
                preview=_preview(),
                generation=_generation(),
                action="changes_requested",
                occurred_at="2026-07-30T12:01:00Z",
                source_event_kind="github_issue_comment",
                source_event_id="comment-102",
                reason="Adjust the primary call to action.",
            ).record
            decision = _decision(events=(approved, changes_requested))
            self.assertEqual(decision.status, "changes_requested")

            revoked = record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(),
                policy_record=_policy_record(),
                product=PRODUCT,
                preview=_preview(),
                generation=_generation(),
                action="revoked",
                occurred_at="2026-07-30T12:02:00Z",
                source_event_kind="github_issue_comment",
                source_event_id="comment-103",
                reason="Approval was revoked.",
            ).record
            decision = _decision(events=(approved, changes_requested, revoked))
            self.assertEqual(decision.status, "revoked")

            invalidated = build_manager_preview_approval_system_event(
                binding=approved.binding,
                action="invalidated",
                occurred_at="2026-07-30T12:03:00Z",
                source_event_kind="preview_destroyed",
                source_event_id="preview-17-destroyed",
                reason="The preview was destroyed.",
            )
            decision = _decision(events=(approved, changes_requested, revoked, invalidated))
            self.assertEqual(decision.status, "stale")

            late_approval = record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(),
                policy_record=_policy_record(),
                product=PRODUCT,
                preview=_preview(),
                generation=_generation(),
                action="approved",
                occurred_at="2026-07-30T12:05:00Z",
                source_event_kind="github_issue_comment",
                source_event_id="comment-104",
            ).record
            decision = _decision(
                events=(approved, changes_requested, revoked, invalidated, late_approval)
            )
            self.assertEqual(decision.status, "stale")
            self.assertEqual(decision.event_id, invalidated.event_id)

            superseded = build_manager_preview_approval_system_event(
                binding=approved.binding,
                action="superseded",
                occurred_at="2026-07-30T12:04:00Z",
                source_event_kind="preview_generation",
                source_event_id="generation-18",
                reason="A new serving generation replaced this preview.",
            )
            decision = _decision(
                events=(approved, changes_requested, revoked, invalidated, superseded)
            )
            self.assertEqual(decision.status, "stale")

    def test_exact_approval_becomes_stale_when_preview_identity_changes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            approved = _approved_event(store)
            stale_cases = {
                "head": (
                    _preview(),
                    _generation(
                        head_sha="2" * 40,
                        runtime_identity=_runtime_identity(source_git_ref="2" * 40),
                    ),
                ),
                "generation": (
                    _preview(
                        active_generation_id="generation-18",
                        serving_generation_id="generation-18",
                        latest_generation_id="generation-18",
                    ),
                    _generation(
                        generation_id="generation-18",
                        runtime_identity=_runtime_identity(preview_generation_id="generation-18"),
                    ),
                ),
                "artifact": (
                    _preview(),
                    _generation(
                        artifact_id="artifact-18",
                        runtime_identity=_runtime_identity(
                            artifact_id="artifact-18",
                            image_reference=f"ghcr.io/example/site@sha256:{'b' * 64}",
                        ),
                    ),
                ),
                "manifest": (
                    _preview(latest_manifest_fingerprint="manifest-18"),
                    _generation(resolved_manifest_fingerprint="manifest-18"),
                ),
                "runtime": (
                    _preview(),
                    _generation(
                        runtime_identity=_runtime_identity(deployment_record_id="deployment-18")
                    ),
                ),
            }
            for label, (preview, generation) in stale_cases.items():
                with self.subTest(label=label):
                    decision = _decision(
                        preview=preview,
                        generation=generation,
                        events=(approved,),
                    )
                    self.assertEqual(decision.status, "stale")

    def test_terminal_prior_binding_yields_pending_for_replacement_preview(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            approved = _approved_event(store)
            replacement_preview = _preview(
                active_generation_id="generation-18",
                serving_generation_id="generation-18",
                latest_generation_id="generation-18",
                latest_manifest_fingerprint="manifest-18",
            )
            replacement_generation = _generation(
                generation_id="generation-18",
                sequence=2,
                resolved_manifest_fingerprint="manifest-18",
                artifact_id="artifact-18",
                runtime_identity=_runtime_identity(
                    deployment_record_id="deployment-18",
                    artifact_id="artifact-18",
                    image_reference=f"ghcr.io/example/site@sha256:{'b' * 64}",
                    preview_generation_id="generation-18",
                ),
            )

            for action in ("invalidated", "superseded"):
                with self.subTest(action=action):
                    terminal = build_manager_preview_approval_system_event(
                        binding=approved.binding,
                        action=action,
                        occurred_at="2026-07-30T11:59:00Z",
                        source_event_kind="preview_lifecycle",
                        source_event_id=f"generation-17:{action}",
                        reason="The prior serving generation is no longer current.",
                    )

                    decision = _decision(
                        preview=replacement_preview,
                        generation=replacement_generation,
                        events=(approved, terminal),
                    )

                    self.assertEqual(decision.status, "pending")
                    self.assertEqual(decision.reason_code, "approval_missing")
                    self.assertNotEqual(
                        decision.current_binding_sha256,
                        approved.binding.binding_sha256,
                    )

    def test_policy_change_makes_prior_approval_stale(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            approved = _approved_event(store)
            changed_policy = _policy_record(
                revision=2,
                extra_actions=("product_profile.read",),
            )

            decision = _decision(events=(approved,), policy_record=changed_policy)

            self.assertEqual(decision.status, "stale")
            self.assertEqual(decision.reason_code, "approval_stale")

            wrong_scope = _decision(
                events=(approved,),
                role_policy_record=_role_policy(
                    manager_primary_ids=(101,),
                    context="staging",
                ),
            )
            self.assertEqual(wrong_scope.status, "stale")

    def test_future_dated_approval_event_is_ignored(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            future_approved = record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(),
                policy_record=_policy_record(),
                product=PRODUCT,
                preview=_preview(),
                generation=_generation(),
                action="approved",
                occurred_at="2026-07-30T12:11:00Z",
                source_event_kind="github_issue_comment",
                source_event_id="comment-104",
            ).record

            decision = _decision(events=(future_approved,))

            self.assertEqual(decision.status, "pending")
            self.assertEqual(decision.reason_code, "approval_missing")

            with self.assertRaisesRegex(ValueError, "recorded before it occurred"):
                record_manager_preview_approval_event(
                    record_store=store,
                    identity=_manager_identity(),
                    policy_record=_policy_record(),
                    product=PRODUCT,
                    preview=_preview(),
                    generation=_generation(),
                    action="approved",
                    occurred_at="2026-07-30T12:11:00Z",
                    recorded_at="2026-07-30T12:10:00Z",
                    source_event_kind="github_issue_comment",
                    source_event_id="comment-future-rejected",
                )

    def test_role_aware_manager_approval_requires_current_delegation(self) -> None:
        active_delegation = RepositoryHumanManagerDelegation(
            delegated_manager_github_id=101,
            delegated_by_github_id=202,
            starts_at="2026-07-30T11:00:00Z",
            expires_at="2026-07-30T13:00:00Z",
            source_event_kind="github_issue_comment",
            source_event_id="delegation-101",
            reason="Manager coverage.",
        )
        role_policy = _role_policy(manager_delegations=(active_delegation,))
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            approved = record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(),
                policy_record=_policy_record(),
                product=PRODUCT,
                preview=_preview(),
                generation=_generation(),
                action="approved",
                occurred_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-101",
                role_policy_record=role_policy,
                repository_id="1001",
                repository_owner_id="2001",
            ).record

            active_decision = _decision(events=(approved,), role_policy_record=role_policy)
            self.assertEqual(active_decision.status, "approved")
            assert approved.authorization is not None
            assert approved.authorization.role_policy_provenance is not None
            self.assertEqual(
                approved.authorization.role_policy_provenance.authority_kind,
                "manager_delegated",
            )

            revoked_policy = _role_policy(
                manager_delegations=(
                    active_delegation.model_copy(update={"revoked_at": "2026-07-30T12:01:00Z"}),
                )
            )
            stale_decision = _decision(events=(approved,), role_policy_record=revoked_policy)
            self.assertEqual(stale_decision.status, "stale")
            self.assertEqual(stale_decision.reason_code, "approval_stale")

    def test_role_policy_rejects_legacy_approval_without_provenance(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            legacy_approval = _approved_event(store)

            decision = _decision(
                events=(legacy_approval,),
                role_policy_record=_role_policy(manager_primary_ids=(101,)),
            )

            self.assertEqual(decision.status, "stale")
            self.assertEqual(decision.reason_code, "approval_stale")

    def test_same_timestamp_revocation_wins_over_approval(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            approved = _approved_event(store)
            revoked = record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(),
                policy_record=_policy_record(),
                product=PRODUCT,
                preview=_preview(),
                generation=_generation(),
                action="revoked",
                occurred_at=OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-revoked-same-time",
                reason="Manager revoked approval.",
            ).record

            decision = _decision(events=(approved, revoked))

            self.assertEqual(decision.status, "revoked")
            self.assertEqual(decision.reason_code, "approval_revoked")

    def test_missing_policy_makes_approval_unavailable(self) -> None:
        decision = evaluate_manager_preview_approval(
            product=PRODUCT,
            preview=_preview(),
            generation=_generation(),
            policy_record=None,
            events=(),
            evaluated_at="2026-07-30T12:10:00Z",
        )

        self.assertEqual(decision.status, "unavailable")
        self.assertEqual(decision.reason_code, "policy_unavailable")

    def test_destroyed_or_failed_preview_is_unavailable_without_gating_cleanup(self) -> None:
        destroyed = _decision(
            preview=_preview(state="destroyed"),
            events=(),
        )
        failed = _decision(
            generation=_generation(verify_status="fail"),
            events=(),
        )

        self.assertEqual(destroyed.status, "unavailable")
        self.assertEqual(destroyed.reason_code, "preview_inactive")
        self.assertEqual(failed.status, "unavailable")
        self.assertEqual(failed.reason_code, "generation_verification_failed")

    def test_empty_github_ids_do_not_change_existing_policy_serialization(self) -> None:
        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_humans=(
                GitHubHumanPolicyRule(
                    logins=("example",),
                    roles=("read_only",),
                    actions=(MANAGER_PREVIEW_APPROVAL_READ_ACTION,),
                ),
            ),
        )

        payload = json.loads(policy.model_dump_json())

        self.assertNotIn("github_ids", payload["github_humans"][0])


def _approved_event(
    record_store: ManagerPreviewApprovalEventStore,
) -> ManagerPreviewApprovalEventRecord:
    return record_manager_preview_approval_event(
        record_store=record_store,
        identity=_manager_identity(),
        policy_record=_policy_record(),
        product=PRODUCT,
        preview=_preview(),
        generation=_generation(),
        action="approved",
        occurred_at=OCCURRED_AT,
        source_event_kind="github_issue_comment",
        source_event_id="comment-101",
    ).record


def _decision(
    *,
    preview: PreviewRecord | None = None,
    generation: PreviewGenerationRecord | None = None,
    policy_record: LaunchplaneAuthzPolicyRecord | None = None,
    role_policy_record: RepositoryHumanRolePolicyRecord | None = None,
    events: tuple[ManagerPreviewApprovalEventRecord, ...],
) -> ManagerPreviewApprovalDecision:
    return evaluate_manager_preview_approval(
        product=PRODUCT,
        preview=preview or _preview(),
        generation=generation or _generation(),
        policy_record=policy_record or _policy_record(),
        events=events,
        evaluated_at="2026-07-30T12:10:00Z",
        role_policy_record=role_policy_record,
    )


def _manager_identity(*, github_id: int = 101, login: str = "manager") -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name="Example Manager",
        email="manager@example.com",
        organizations=frozenset(),
        teams=frozenset(),
        role="read_only",
    )


def _policy_record(
    *,
    revision: int = 1,
    extra_actions: tuple[str, ...] = (),
) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="manager.example-site",
                managed_rule_id="preview-approval",
                github_ids=(101,),
                roles=("read_only",),
                products=(PRODUCT,),
                contexts=(CONTEXT,),
                actions=(
                    MANAGER_PREVIEW_APPROVAL_READ_ACTION,
                    MANAGER_PREVIEW_APPROVAL_WRITE_ACTION,
                    *extra_actions,
                ),
            ),
        ),
    )
    policy_sha256 = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=revision,
            policy_sha256=policy_sha256,
        ),
        revision=revision,
        status="active",
        source="test:manager-preview-approval",
        updated_at=OCCURRED_AT,
        policy_sha256=policy_sha256,
        policy=policy,
    )


def _role_policy(
    *,
    manager_primary_ids: tuple[int, ...] = (202,),
    manager_backup_ids: tuple[int, ...] = (),
    manager_delegations: tuple[RepositoryHumanManagerDelegation, ...] = (),
    context: str = CONTEXT,
) -> RepositoryHumanRolePolicyRecord:
    return RepositoryHumanRolePolicyRecord(
        repository_id="1001",
        repository_owner_id="2001",
        repository=REPOSITORY,
        product=PRODUCT,
        context=context,
        role_policy_revision=1,
        repository_owner_github_ids=(301,),
        manager_primary_github_ids=manager_primary_ids,
        manager_backup_github_ids=manager_backup_ids,
        manager_delegations=manager_delegations,
        effective_at="2026-07-30T11:00:00Z",
        source="test:manager-preview-role-policy",
        reason="test manager role policy",
    )


def _preview(**updates: object) -> PreviewRecord:
    payload = {
        "preview_id": "preview-17",
        "context": CONTEXT,
        "anchor_repo": REPOSITORY,
        "anchor_pr_number": PR_NUMBER,
        "anchor_pr_url": f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
        "preview_label": "launchplane-preview",
        "canonical_url": "https://preview-17.example.com/",
        "state": "active",
        "created_at": "2026-07-30T11:00:00Z",
        "updated_at": "2026-07-30T11:30:00Z",
        "eligible_at": "2026-07-30T11:00:00Z",
        "active_generation_id": "generation-17",
        "serving_generation_id": "generation-17",
        "latest_generation_id": "generation-17",
        "latest_manifest_fingerprint": "manifest-17",
    }
    payload.update(updates)
    return PreviewRecord.model_validate(payload)


def _generation(**updates: object) -> PreviewGenerationRecord:
    payload = {
        "generation_id": "generation-17",
        "preview_id": "preview-17",
        "sequence": 1,
        "state": "ready",
        "requested_reason": "Preview requested.",
        "requested_at": "2026-07-30T11:00:00Z",
        "started_at": "2026-07-30T11:01:00Z",
        "ready_at": "2026-07-30T11:30:00Z",
        "finished_at": "2026-07-30T11:30:00Z",
        "resolved_manifest_fingerprint": "manifest-17",
        "artifact_id": "artifact-17",
        "anchor_summary": PreviewPullRequestSummary(
            repo=REPOSITORY,
            pr_number=PR_NUMBER,
            head_sha=HEAD_SHA,
            pr_url=f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
        ),
        "deploy_status": "pass",
        "verify_status": "pass",
        "overall_health_status": "pass",
        "runtime_identity": _runtime_identity(),
    }
    head_sha = updates.pop("head_sha", None)
    if head_sha is not None:
        payload["anchor_summary"] = PreviewPullRequestSummary(
            repo=REPOSITORY,
            pr_number=PR_NUMBER,
            head_sha=str(head_sha),
            pr_url=f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
        )
    payload.update(updates)
    return PreviewGenerationRecord.model_validate(payload)


def _runtime_identity(**updates: object) -> RuntimeIdentity:
    payload: dict[str, object] = {
        "product": PRODUCT,
        "context": CONTEXT,
        "instance": "preview-17",
        "environment_kind": "preview",
        "deployment_record_id": "deployment-17",
        "artifact_id": "artifact-17",
        "source_git_ref": HEAD_SHA,
        "image_reference": f"ghcr.io/example/site@{IMAGE_DIGEST}",
        "preview_id": "preview-17",
        "preview_generation_id": "generation-17",
        "deployed_at": "2026-07-30T11:20:00Z",
    }
    payload.update(updates)
    return RuntimeIdentity.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
