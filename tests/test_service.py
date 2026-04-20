import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane.service import create_harbor_service_app
from control_plane.service_auth import GitHubActionsIdentity, GitHubOidcVerifier, HarborAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore


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
