import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord, PreviewPullRequestSummary
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence, PromotionRecord
from control_plane.service import create_harbor_service_app
from control_plane.service_auth import GitHubActionsIdentity, GitHubOidcVerifier, HarborAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.verireel_testing_deploy import VeriReelTestingDeployResult


class _StubVerifier:
    def __init__(self, identity: GitHubActionsIdentity):
        self.identity = identity

    def verify(self, token: str) -> GitHubActionsIdentity:
        if token != "valid-token":
            raise ValueError("OIDC bearer token is required.")
        return self.identity


def _identity(
    *,
    repository: str = "every/verireel",
    workflow_ref: str = "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
    event_name: str = "pull_request",
    ref: str = "refs/heads/main",
    environment: str = "",
) -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository=repository,
        repository_owner="every",
        workflow_ref=workflow_ref,
        job_workflow_ref="",
        ref=ref,
        ref_type="branch",
        event_name=event_name,
        environment=environment,
        subject="repo:every/verireel:pull_request",
        sha="6b3c9d7e8f901234567890abcdef1234567890ab",
        raw_claims={"repository": repository, "workflow_ref": workflow_ref},
    )


def _invoke_app(
    app,
    *,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    authorization: str = "Bearer valid-token",
):
    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else b""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body_bytes)),
        "wsgi.input": io.BytesIO(body_bytes),
        "HTTP_AUTHORIZATION": authorization,
    }
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    response_body = b"".join(app(environ, start_response))
    return int(str(captured["status"]).split(" ", 1)[0]), json.loads(response_body.decode("utf-8"))


class GitHubOidcVerifierTests(unittest.TestCase):
    def test_verify_decodes_expected_github_claims(self) -> None:
        mock_jwk_client = Mock()
        mock_jwk_client.get_signing_key_from_jwt.return_value = SimpleNamespace(key="signing-key")
        claims = {
            "repository": "every/verireel",
            "repository_owner": "every",
            "workflow_ref": "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
            "job_workflow_ref": "",
            "ref": "refs/heads/main",
            "ref_type": "branch",
            "event_name": "pull_request",
            "environment": "",
            "sub": "repo:every/verireel:pull_request",
            "sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
        }
        with patch("control_plane.service_auth.jwt.decode", return_value=claims) as decode_mock:
            verifier = GitHubOidcVerifier(
                audience="harbor.shinycomputers.com",
                jwk_client=mock_jwk_client,
            )
            identity = verifier.verify("header.payload.signature")

        mock_jwk_client.get_signing_key_from_jwt.assert_called_once_with("header.payload.signature")
        decode_mock.assert_called_once_with(
            "header.payload.signature",
            "signing-key",
            algorithms=["RS256"],
            audience="harbor.shinycomputers.com",
            issuer="https://token.actions.githubusercontent.com",
        )
        self.assertEqual(identity.repository, "every/verireel")
        self.assertEqual(identity.workflow_ref, claims["workflow_ref"])


class HarborServiceTests(unittest.TestCase):
    def test_health_endpoint_reports_storage_backend(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_harbor_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=HarborAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )

            status_code, payload = _invoke_app(app, method="GET", path="/v1/health", authorization="")

            self.assertEqual(status_code, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["storage_backend"], "filesystem")

    def test_preview_generation_endpoint_writes_records_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/previews/generations",
                payload={
                    "product": "verireel",
                    "preview": {
                        "context": "verireel-testing",
                        "anchor_repo": "verireel",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                        "canonical_url": "https://pr-123.ver-preview.shinycomputers.com",
                        "state": "active",
                        "updated_at": "2026-04-16T08:10:00Z",
                        "eligible_at": "2026-04-16T08:10:00Z",
                    },
                    "generation": {
                        "context": "verireel-testing",
                        "anchor_repo": "verireel",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                        "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                        "state": "ready",
                        "requested_reason": "external_preview_refresh",
                        "requested_at": "2026-04-16T08:02:00Z",
                        "ready_at": "2026-04-16T08:10:00Z",
                        "finished_at": "2026-04-16T08:10:00Z",
                        "resolved_manifest_fingerprint": "verireel-preview-manifest-pr-123-6b3c9d7",
                        "artifact_id": "ghcr.io/every/verireel-app:pr-123-6b3c9d7",
                        "deploy_status": "pass",
                        "verify_status": "pass",
                        "overall_health_status": "pass",
                    },
                },
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            store = FilesystemRecordStore(state_dir=state_dir)
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            generation = store.read_preview_generation_record(
                "preview-verireel-testing-verireel-pr-123-generation-0001"
            )
            self.assertEqual(preview.canonical_url, "https://pr-123.ver-preview.shinycomputers.com")
            self.assertEqual(preview.state, "active")
            self.assertEqual(generation.state, "ready")
            self.assertEqual(generation.artifact_id, "ghcr.io/every/verireel-app:pr-123-6b3c9d7")

    def test_deployment_endpoint_writes_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/deploy-testing.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/deploy-testing.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/deployments",
                payload={
                    "product": "odoo",
                    "deployment": {
                        "record_id": "deployment-20260420T153000Z-opw-testing",
                        "artifact_identity": {"artifact_id": "artifact-20260420-a1b2c3d4"},
                        "context": "opw",
                        "instance": "testing",
                        "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
                        "resolved_target": {
                            "target_type": "compose",
                            "target_id": "compose-123",
                            "target_name": "opw-testing",
                        },
                        "deploy": {
                            "target_name": "opw-testing",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "delegated-compose-ship",
                            "status": "pass",
                            "started_at": "2026-04-20T15:30:00Z",
                            "finished_at": "2026-04-20T15:32:10Z",
                        },
                        "post_deploy_update": {
                            "attempted": True,
                            "status": "pass",
                            "detail": "Update completed.",
                        },
                        "destination_health": {
                            "verified": True,
                            "urls": ["https://testing.example.com/web/health"],
                            "timeout_seconds": 45,
                            "status": "pass",
                        },
                    },
                },
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "deployment_record_id": "deployment-20260420T153000Z-opw-testing",
                    "inventory_record_id": "opw-testing",
                },
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            deployment = store.read_deployment_record("deployment-20260420T153000Z-opw-testing")
            inventory = store.read_environment_inventory(
                context_name="opw",
                instance_name="testing",
            )
            self.assertEqual(deployment.context, "opw")
            self.assertEqual(deployment.instance, "testing")
            self.assertEqual(deployment.deploy.status, "pass")
            self.assertEqual(deployment.resolved_target.target_id, "compose-123")
            self.assertEqual(inventory.deployment_record_id, deployment.record_id)
            self.assertEqual(inventory.promotion_record_id, "")

    def test_promotion_endpoint_writes_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T160500Z-opw-prod",
                    artifact_identity={"artifact_id": "artifact-20260420-a1b2c3d4"},
                    context="opw",
                    instance="prod",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    resolved_target=ResolvedTargetEvidence(
                        target_type="compose",
                        target_id="compose-456",
                        target_name="opw-prod",
                    ),
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-20T16:05:00Z",
                        finished_at="2026-04-20T16:08:30Z",
                    ),
                )
            )
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["promotion.write"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/promote-prod.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/promotions",
                payload={
                    "product": "odoo",
                    "promotion": {
                        "record_id": "promotion-20260420T160500Z-opw-testing-to-prod",
                        "artifact_identity": {"artifact_id": "artifact-20260420-a1b2c3d4"},
                        "deployment_record_id": "deployment-20260420T160500Z-opw-prod",
                        "backup_record_id": "backup-opw-prod-20260420T155000Z",
                        "context": "opw",
                        "from_instance": "testing",
                        "to_instance": "prod",
                        "backup_gate": {
                            "required": True,
                            "status": "pass",
                            "evidence": {"recorded_by": "harbor-service"},
                        },
                        "deploy": {
                            "target_name": "opw-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "status": "pass",
                            "started_at": "2026-04-20T16:05:00Z",
                            "finished_at": "2026-04-20T16:08:30Z",
                        },
                        "destination_health": {
                            "verified": True,
                            "urls": ["https://prod.example.com/web/health"],
                            "timeout_seconds": 45,
                            "status": "pass",
                        },
                    },
                },
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "promotion_record_id": "promotion-20260420T160500Z-opw-testing-to-prod",
                    "inventory_record_id": "opw-prod",
                },
            )
            promotion = store.read_promotion_record(
                "promotion-20260420T160500Z-opw-testing-to-prod"
            )
            inventory = store.read_environment_inventory(
                context_name="opw",
                instance_name="prod",
            )
            self.assertEqual(promotion.context, "opw")
            self.assertEqual(promotion.from_instance, "testing")
            self.assertEqual(promotion.to_instance, "prod")
            self.assertEqual(promotion.deploy.status, "pass")
            self.assertEqual(promotion.backup_gate.status, "pass")
            self.assertEqual(inventory.deployment_record_id, "deployment-20260420T160500Z-opw-prod")
            self.assertEqual(inventory.promotion_record_id, promotion.record_id)
            self.assertEqual(inventory.promoted_from_instance, "testing")

    def test_preview_destroyed_endpoint_writes_records_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    context="verireel-testing",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    preview_label="verireel-testing/verireel/pr-123",
                    state="active",
                    canonical_url="https://pr-123.ver-preview.shinycomputers.com",
                    created_at="2026-04-16T08:02:00Z",
                    updated_at="2026-04-16T08:10:00Z",
                    eligible_at="2026-04-16T08:10:00Z",
                    active_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    serving_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    latest_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                )
            )
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_destroyed.write"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main"
                        )
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/previews/destroyed",
                payload={
                    "product": "verireel",
                    "destroy": {
                        "context": "verireel-testing",
                        "anchor_repo": "verireel",
                        "anchor_pr_number": 123,
                        "destroyed_at": "2026-04-16T09:04:00Z",
                        "destroy_reason": "external_preview_cleanup_completed",
                    },
                },
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "preview_id": "preview-verireel-testing-verireel-pr-123",
                    "transition": "destroyed",
                },
            )
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroyed_at, "2026-04-16T09:04:00Z")
            self.assertEqual(preview.destroy_reason, "external_preview_cleanup_completed")
            self.assertEqual(preview.active_generation_id, "")
            self.assertEqual(preview.serving_generation_id, "")
            self.assertEqual(
                preview.latest_generation_id,
                "preview-verireel-testing-verireel-pr-123-generation-0001",
            )

    def test_verireel_testing_deploy_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_testing_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_testing_deploy",
                return_value=VeriReelTestingDeployResult(
                    deployment_record_id="deployment-verireel-testing-run-12345-attempt-1",
                    deploy_status="pass",
                    deploy_started_at="2026-04-20T18:20:00Z",
                    deploy_finished_at="2026-04-20T18:21:15Z",
                    target_name="ver-testing-app",
                    target_type="application",
                    target_id="testing-app-123",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/testing-deploy",
                    payload={
                        "product": "verireel",
                        "deploy": {
                            "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                            "source_git_ref": "abcdef1234567890",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {"deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1"},
            )
            self.assertEqual(payload["result"]["deploy_status"], "pass")
            self.assertEqual(payload["result"]["target_id"], "testing-app-123")
            execute_mock.assert_called_once()

    def test_verireel_testing_deploy_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/testing-deploy",
                payload={
                    "product": "verireel",
                    "deploy": {
                        "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                        "source_git_ref": "abcdef1234567890",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_deployment_read_endpoint_returns_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
                    context="opw",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    resolved_target=ResolvedTargetEvidence(
                        target_type="compose",
                        target_id="compose-123",
                        target_name="opw-testing",
                    ),
                    deploy=DeploymentEvidence(
                        target_name="opw-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                        started_at="2026-04-20T15:30:00Z",
                        finished_at="2026-04-20T15:32:10Z",
                    ),
                )
            )
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "contexts": ["opw"],
                            "actions": ["deployment.read"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/deployments/deployment-20260420T153000Z-opw-testing",
            )

            self.assertEqual(status_code, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["record"]["record_id"], "deployment-20260420T153000Z-opw-testing")
            self.assertEqual(payload["record"]["resolved_target"]["target_id"], "compose-123")

    def test_preview_history_and_recent_operations_endpoints_return_operator_read_models(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    context="verireel-testing",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    preview_label="verireel/pr-123",
                    canonical_url="https://pr-123.ver-preview.shinycomputers.com",
                    state="active",
                    created_at="2026-04-20T10:00:00Z",
                    updated_at="2026-04-20T10:05:00Z",
                    eligible_at="2026-04-20T10:05:00Z",
                )
            )
            store.write_preview_generation_record(
                PreviewGenerationRecord(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    sequence=1,
                    state="ready",
                    requested_reason="external_preview_refresh",
                    requested_at="2026-04-20T10:01:00Z",
                    ready_at="2026-04-20T10:05:00Z",
                    finished_at="2026-04-20T10:05:00Z",
                    resolved_manifest_fingerprint="preview-manifest-123",
                    artifact_id="ghcr.io/every/verireel-app:pr-123",
                    anchor_summary=PreviewPullRequestSummary(
                        repo="verireel",
                        pr_number=123,
                        head_sha="6b3c9d7e8f901234567890abcdef1234567890ab",
                        pr_url="https://github.com/every/verireel/pull/123",
                    ),
                    deploy_status="pass",
                    verify_status="pass",
                    overall_health_status="pass",
                )
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-verireel-testing",
                    artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
                    context="verireel-testing",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    resolved_target=ResolvedTargetEvidence(
                        target_type="application",
                        target_id="app-123",
                        target_name="verireel-testing",
                    ),
                    deploy=DeploymentEvidence(
                        target_name="verireel-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="delegated-app-ship",
                        status="pass",
                        started_at="2026-04-20T15:30:00Z",
                        finished_at="2026-04-20T15:32:10Z",
                    ),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260420T160500Z-verireel-testing-to-prod",
                    artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
                    deployment_record_id="deployment-20260420T153000Z-verireel-testing",
                    backup_record_id="backup-verireel-prod-20260420T160000Z",
                    context="verireel-testing",
                    from_instance="testing",
                    to_instance="prod",
                    deploy=DeploymentEvidence(
                        target_name="verireel-prod",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="delegated-app-promote",
                        status="pass",
                        started_at="2026-04-20T16:05:00Z",
                        finished_at="2026-04-20T16:07:00Z",
                    ),
                )
            )
            store.write_environment_inventory(
                EnvironmentInventory(
                    context="verireel-testing",
                    instance="testing",
                    artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="verireel-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="delegated-app-ship",
                        status="pass",
                        started_at="2026-04-20T15:30:00Z",
                        finished_at="2026-04-20T15:32:10Z",
                    ),
                    updated_at="2026-04-20T15:33:00Z",
                    deployment_record_id="deployment-20260420T153000Z-verireel-testing",
                )
            )
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview.read", "operations.read"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            history_status_code, history_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/previews/preview-verireel-testing-verireel-pr-123/history",
            )
            operations_status_code, operations_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/verireel-testing/operations/recent",
            )

            self.assertEqual(history_status_code, 200)
            self.assertEqual(history_payload["preview"]["preview_id"], "preview-verireel-testing-verireel-pr-123")
            self.assertEqual(len(history_payload["generations"]), 1)
            self.assertEqual(history_payload["generations"][0]["state"], "ready")

            self.assertEqual(operations_status_code, 200)
            self.assertEqual(operations_payload["context"], "verireel-testing")
            self.assertEqual(operations_payload["storage_backend"], "filesystem")
            self.assertEqual(len(operations_payload["inventory"]), 1)
            self.assertEqual(len(operations_payload["recent_deployments"]), 1)
            self.assertEqual(len(operations_payload["recent_promotions"]), 1)
            self.assertEqual(len(operations_payload["recent_previews"]), 1)

    def test_preview_generation_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/previews/generations",
                payload={
                    "product": "verireel",
                    "preview": {
                        "context": "verireel-testing",
                        "anchor_repo": "verireel",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                        "canonical_url": "https://pr-123.ver-preview.shinycomputers.com",
                    },
                    "generation": {
                        "context": "verireel-testing",
                        "anchor_repo": "verireel",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                        "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                        "state": "ready",
                        "requested_reason": "external_preview_refresh",
                        "requested_at": "2026-04-16T08:02:00Z",
                        "resolved_manifest_fingerprint": "verireel-preview-manifest-pr-123-6b3c9d7",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_deployment_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/deploy-testing.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/deploy-testing.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/deployments",
                payload={
                    "product": "odoo",
                    "deployment": {
                        "record_id": "deployment-20260420T153000Z-opw-testing",
                        "context": "opw",
                        "instance": "testing",
                        "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
                        "deploy": {
                            "target_name": "opw-testing",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                        },
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_promotion_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/promote-prod.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/promotions",
                payload={
                    "product": "odoo",
                    "promotion": {
                        "record_id": "promotion-20260420T160500Z-opw-testing-to-prod",
                        "artifact_identity": {"artifact_id": "artifact-20260420-a1b2c3d4"},
                        "context": "opw",
                        "from_instance": "testing",
                        "to_instance": "prod",
                        "deploy": {
                            "target_name": "opw-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                        },
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_preview_destroyed_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = HarborAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_harbor_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/previews/destroyed",
                payload={
                    "product": "verireel",
                    "destroy": {
                        "context": "verireel-testing",
                        "anchor_repo": "verireel",
                        "anchor_pr_number": 123,
                        "destroyed_at": "2026-04-16T09:04:00Z",
                        "destroy_reason": "external_preview_cleanup_completed",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_preview_generation_endpoint_requires_bearer_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_harbor_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=HarborAuthzPolicy(github_actions=()),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/previews/generations",
                payload={"product": "verireel", "preview": {}, "generation": {}},
                authorization="",
            )

            self.assertEqual(status_code, 401)
            self.assertEqual(payload["error"]["code"], "authentication_required")
