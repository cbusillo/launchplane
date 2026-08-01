from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from typing import Any, Literal, cast

import click

from control_plane.contracts.tenant_merge_eligibility import (
    TenantRepositoryClassificationRecord,
)
from control_plane.contracts.trusted_maintenance import (
    TrustedMaintenanceActorRule,
    TrustedMaintenanceAllowedEvent,
    TrustedMaintenancePolicyRecord,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.trusted_maintenance_github_webhook import (
    TRUSTED_MAINTENANCE_GITHUB_WEBHOOK_SOURCE,
    TrustedMaintenanceGitHubWebhookDependencies,
    TrustedMaintenanceGitHubWebhookResult,
    handle_trusted_maintenance_github_webhook as _handle_trusted_maintenance_github_webhook,
)


PRODUCT = "example-product"
CONTEXT = "example-context"
REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/example-product"
PULL_REQUEST_NUMBER = 17
HEAD_SHA = "a" * 40
SIGNED_PAYLOAD_SHA256 = "d" * 64


def handle_trusted_maintenance_github_webhook(
    *,
    event_name: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
    control_plane_root: Path,
    dependencies: TrustedMaintenanceGitHubWebhookDependencies,
) -> TrustedMaintenanceGitHubWebhookResult:
    return _handle_trusted_maintenance_github_webhook(
        event_name=event_name,
        delivery_id=delivery_id,
        signed_payload_sha256=SIGNED_PAYLOAD_SHA256,
        payload=payload,
        record_store=record_store,
        control_plane_root=control_plane_root,
        dependencies=dependencies,
    )


class _TestPostgresRecordStore(PostgresRecordStore):
    @property
    def database_dialect_name(self) -> str:
        return "postgresql"


class _GitHubPullRequestApi:
    def __init__(self, pull_request: dict[str, object] | None = None) -> None:
        self.pull_request = pull_request or _current_pull_request_payload()
        self.fail_next = False
        self.before_return: Any = None
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, **kwargs: object) -> object:
        path = str(kwargs.get("path") or "")
        token = str(kwargs.get("token") or "")
        with self._lock:
            self.calls.append((path, token))
            if self.fail_next:
                self.fail_next = False
                raise click.ClickException("GitHub is temporarily unavailable.")
            before_return = self.before_return
            self.before_return = None
        if before_return is not None:
            before_return()
        if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST_NUMBER}":
            return self.pull_request
        raise AssertionError(f"unexpected GitHub API request: {path}")


class _GitHubTokenResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, *, control_plane_root: Path, context_name: str) -> str:
        self.calls.append((control_plane_root, context_name))
        return "managed-token"


class TrustedMaintenanceGitHubWebhookTests(unittest.TestCase):
    def test_no_authority_or_no_signed_rule_candidate_skips_before_api(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = _store(root / "store")
            api = _GitHubPullRequestApi()

            no_authority = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-no-authority",
                payload=_signed_pull_request_payload(),
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )

            store.write_tenant_repository_classification_record(_classification())
            store.write_trusted_maintenance_policy_record(_policy(actor_id=999))
            no_rule = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-no-rule",
                payload=_signed_pull_request_payload(),
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )
            wrong_event_action = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-wrong-action",
                payload=_signed_pull_request_payload(action="reopened"),
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )

        self.assertEqual(no_authority.status, "skipped")
        self.assertEqual(no_authority.reason, "authority_not_available")
        self.assertEqual(no_rule.status, "skipped")
        self.assertEqual(no_rule.reason, "rule_not_matched")
        self.assertEqual(wrong_event_action.status, "skipped")
        self.assertEqual(wrong_event_action.reason, "rule_not_matched")
        self.assertEqual(api.calls, [])

    def test_capture_uses_refetched_facts_and_replays_changed_delivery_header(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = _seeded_store(root / "store", policy=_policy(evidence_ttl_seconds=60))
            api = _GitHubPullRequestApi(
                _current_pull_request_payload(author_login="current-automation")
            )
            token_resolver = _GitHubTokenResolver()
            payload = _signed_pull_request_payload(author_login="signed-automation")

            first = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-capture",
                payload=payload,
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api, token_resolver=token_resolver),
            )
            replay = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="changed-unsigned-delivery-header",
                payload=payload,
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api, token_resolver=token_resolver),
            )
            evidence = store.list_trusted_maintenance_evidence_records(
                repository_id=REPOSITORY_ID,
                delivery_id="delivery-capture",
            )
            store.close()

        self.assertEqual(first.status, "captured")
        self.assertEqual(first.evidence_status, "written")
        self.assertEqual(replay.status, "replayed")
        self.assertEqual(replay.evidence_status, "replayed")
        self.assertEqual(len(evidence), 1)
        binding = evidence[0].binding
        self.assertEqual(binding.source, TRUSTED_MAINTENANCE_GITHUB_WEBHOOK_SOURCE)
        self.assertEqual(binding.delivery_id, "delivery-capture")
        self.assertEqual(binding.signed_payload_sha256, SIGNED_PAYLOAD_SHA256)
        self.assertEqual(binding.pr_author_login, "current-automation")
        self.assertEqual(binding.sender_login, "signed-sender")
        self.assertEqual(binding.pr_author_github_id, 301)
        self.assertEqual(binding.sender_github_id, 301)
        self.assertEqual(binding.event_name, "pull_request")
        self.assertEqual(binding.event_action, "synchronize")
        self.assertEqual(evidence[0].occurred_at, evidence[0].recorded_at)
        self.assertTrue(evidence[0].expires_at)
        self.assertEqual(token_resolver.calls, [(root, CONTEXT), (root, CONTEXT)])
        self.assertEqual(
            api.calls, [(f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST_NUMBER}", "managed-token")] * 2
        )

    def test_signed_and_refetched_fact_mismatches_skip_without_evidence(self) -> None:
        cases: tuple[tuple[str, dict[str, object]], ...] = (
            (
                "closed",
                _current_pull_request_payload(state="closed"),
            ),
            (
                "base_repo_mismatch",
                _current_pull_request_payload(repository_id="1002"),
            ),
            (
                "stale_head",
                _current_pull_request_payload(head_sha="b" * 40),
            ),
            (
                "fork_head",
                _current_pull_request_payload(
                    head_repository_id="1002",
                    head_repository_owner_id="2002",
                    head_repository="example/fork",
                ),
            ),
            (
                "author_mismatch",
                _current_pull_request_payload(author_id=302),
            ),
            (
                "missing_current_author_login",
                _current_pull_request_payload(author_login=""),
            ),
        )
        for name, pull_request in cases:
            with self.subTest(name=name), TemporaryDirectory() as temporary_directory_name:
                root = Path(temporary_directory_name)
                store = _seeded_store(root / "store")
                result = handle_trusted_maintenance_github_webhook(
                    event_name="pull_request",
                    delivery_id=f"delivery-{name}",
                    payload=_signed_pull_request_payload(),
                    record_store=store,
                    control_plane_root=root,
                    dependencies=_dependencies(api=_GitHubPullRequestApi(pull_request)),
                )
                evidence = store.list_trusted_maintenance_evidence_records(
                    repository_id=REPOSITORY_ID
                )
                store.close()

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason, "current_pull_request_not_matched")
            self.assertEqual(evidence, ())

    def test_non_bot_or_login_only_signed_payloads_skip_before_api(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = _seeded_store(root / "store")
            api = _GitHubPullRequestApi()
            non_bot = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-non-bot",
                payload=_signed_pull_request_payload(sender_type="User"),
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )
            login_only_payload = _signed_pull_request_payload()
            cast(dict[str, object], login_only_payload["sender"]).pop("id")
            login_only = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-login-only",
                payload=login_only_payload,
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )
            store.close()

        self.assertEqual(non_bot.status, "skipped")
        self.assertEqual(non_bot.reason, "non_bot_actor")
        self.assertEqual(login_only.status, "skipped")
        self.assertEqual(login_only.reason, "malformed_payload")
        self.assertEqual(api.calls, [])

    def test_transient_github_failure_then_exact_redelivery_succeeds(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = _seeded_store(root / "store")
            api = _GitHubPullRequestApi()
            api.fail_next = True

            first = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-redelivery",
                payload=_signed_pull_request_payload(),
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )
            after_failure = store.list_trusted_maintenance_evidence_records(
                repository_id=REPOSITORY_ID
            )
            redelivery = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-redelivery",
                payload=_signed_pull_request_payload(),
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )
            evidence = store.list_trusted_maintenance_evidence_records(repository_id=REPOSITORY_ID)
            store.close()

        self.assertEqual(first.status, "retryable_error")
        self.assertEqual(first.reason, "github_api_unavailable")
        self.assertEqual(after_failure, ())
        self.assertEqual(redelivery.status, "captured")
        self.assertEqual(len(evidence), 1)

    def test_authority_drift_during_fetch_is_retryable_and_writes_no_evidence(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            classification = _classification()
            store = _seeded_store(root / "store", classification=classification)
            api = _GitHubPullRequestApi()

            def drift_classification() -> None:
                store.write_tenant_repository_classification_record(
                    _classification(
                        kind="engineering",
                        revision=2,
                        supersedes_record_id=classification.record_id,
                    )
                )

            api.before_return = drift_classification
            result = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-drift",
                payload=_signed_pull_request_payload(),
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )
            evidence = store.list_trusted_maintenance_evidence_records(repository_id=REPOSITORY_ID)
            store.close()

        self.assertEqual(result.status, "retryable_error")
        self.assertEqual(result.reason, "authority_drift")
        self.assertEqual(evidence, ())

    def test_same_delivery_changed_binding_returns_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = _seeded_store(
                root / "store",
                policy=_policy(event_actions=("synchronize", "reopened")),
            )
            api = _GitHubPullRequestApi()
            first = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-conflict",
                payload=_signed_pull_request_payload(action="synchronize"),
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )
            conflict = handle_trusted_maintenance_github_webhook(
                event_name="pull_request",
                delivery_id="delivery-conflict",
                payload=_signed_pull_request_payload(action="reopened"),
                record_store=store,
                control_plane_root=root,
                dependencies=_dependencies(api=api),
            )
            evidence = store.list_trusted_maintenance_evidence_records(
                repository_id=REPOSITORY_ID,
                delivery_id="delivery-conflict",
            )
            store.close()

        self.assertEqual(first.status, "captured")
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(conflict.reason, "evidence_conflict")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].binding.event_action, "synchronize")

    def test_concurrent_exact_delivery_capture_replays_single_evidence(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = _seeded_store(root / "store")
            payload = _signed_pull_request_payload()

            def capture() -> str:
                result = handle_trusted_maintenance_github_webhook(
                    event_name="pull_request",
                    delivery_id="delivery-concurrent",
                    payload=payload,
                    record_store=store,
                    control_plane_root=root,
                    dependencies=_dependencies(api=_GitHubPullRequestApi()),
                )
                return result.status

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = tuple(executor.map(lambda _: capture(), range(2)))
            evidence = store.list_trusted_maintenance_evidence_records(
                repository_id=REPOSITORY_ID,
                delivery_id="delivery-concurrent",
            )
            store.close()

        self.assertEqual(set(statuses), {"captured", "replayed"})
        self.assertEqual(len(evidence), 1)


def _dependencies(
    *,
    api: _GitHubPullRequestApi,
    token_resolver: _GitHubTokenResolver | None = None,
) -> TrustedMaintenanceGitHubWebhookDependencies:
    return TrustedMaintenanceGitHubWebhookDependencies(
        github_token=token_resolver or _GitHubTokenResolver(),
        github_api=api,
    )


def _store(root: Path) -> _TestPostgresRecordStore:
    root.mkdir(parents=True, exist_ok=True)
    store = _TestPostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'db.sqlite3'}")
    store.ensure_schema()
    return store


def _seeded_store(
    root: Path,
    *,
    classification: TenantRepositoryClassificationRecord | None = None,
    policy: TrustedMaintenancePolicyRecord | None = None,
) -> _TestPostgresRecordStore:
    store = _store(root)
    store.write_tenant_repository_classification_record(classification or _classification())
    store.write_trusted_maintenance_policy_record(policy or _policy())
    return store


def _classification(
    *,
    kind: Literal["engineering", "tenant_ui"] = "tenant_ui",
    revision: int = 1,
    supersedes_record_id: str | None = None,
) -> TenantRepositoryClassificationRecord:
    return TenantRepositoryClassificationRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        product=PRODUCT,
        context=CONTEXT,
        classification_kind=kind,
        classification_revision=revision,
        classified_at="2026-07-31T09:00:00Z",
        source="test-classifier",
        reason="test classification",
        supersedes_record_id=supersedes_record_id,
    )


def _policy(
    *,
    actor_id: int = 301,
    sender_ids: tuple[int, ...] = (301,),
    event_actions: tuple[str, ...] = ("synchronize",),
    evidence_ttl_seconds: int | None = None,
) -> TrustedMaintenancePolicyRecord:
    return TrustedMaintenancePolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        product=PRODUCT,
        context=CONTEXT,
        policy_revision=1,
        actor_rules=(
            TrustedMaintenanceActorRule(
                actor_github_id=actor_id,
                actor_login="automation-301",
                sender_github_ids=sender_ids,
                sender_logins=("automation-sender",),
                allowed_events=(
                    TrustedMaintenanceAllowedEvent(
                        event_name="pull_request",
                        actions=event_actions,
                    ),
                ),
            ),
        ),
        evidence_ttl_seconds=evidence_ttl_seconds,
        effective_at="2026-07-31T10:00:00Z",
        source="test-source",
        reason="test trusted-maintenance policy",
    )


def _signed_pull_request_payload(
    *,
    action: str = "synchronize",
    repository_id: str = REPOSITORY_ID,
    repository_owner_id: str = REPOSITORY_OWNER_ID,
    repository: str = REPOSITORY,
    pr_number: int = PULL_REQUEST_NUMBER,
    head_sha: str = HEAD_SHA,
    author_id: int = 301,
    author_type: str = "Bot",
    author_login: str = "signed-automation",
    sender_id: int = 301,
    sender_type: str = "Bot",
    sender_login: str = "signed-sender",
) -> dict[str, object]:
    return {
        "action": action,
        "repository": {
            "id": int(repository_id),
            "full_name": repository,
            "owner": {"id": int(repository_owner_id), "login": repository.split("/", 1)[0]},
        },
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "head": {"sha": head_sha},
            "user": {"id": author_id, "type": author_type, "login": author_login},
        },
        "sender": {"id": sender_id, "type": sender_type, "login": sender_login},
    }


def _current_pull_request_payload(
    *,
    state: str = "open",
    repository_id: str = REPOSITORY_ID,
    repository_owner_id: str = REPOSITORY_OWNER_ID,
    repository: str = REPOSITORY,
    head_sha: str = HEAD_SHA,
    head_repository_id: str = REPOSITORY_ID,
    head_repository_owner_id: str = REPOSITORY_OWNER_ID,
    head_repository: str = REPOSITORY,
    author_id: int = 301,
    author_type: str = "Bot",
    author_login: str = "current-automation",
) -> dict[str, object]:
    return {
        "number": PULL_REQUEST_NUMBER,
        "state": state,
        "base": {
            "repo": {
                "id": int(repository_id),
                "full_name": repository,
                "owner": {"id": int(repository_owner_id)},
            }
        },
        "head": {
            "sha": head_sha,
            "repo": {
                "id": int(head_repository_id),
                "full_name": head_repository,
                "owner": {"id": int(head_repository_owner_id)},
            },
        },
        "user": {"id": author_id, "type": author_type, "login": author_login},
    }


if __name__ == "__main__":
    unittest.main()
