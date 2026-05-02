import base64
import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Literal
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
)
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PromotionRecord,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.service import create_launchplane_service_app
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    GitHubOidcVerifier,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    GITHUB_EMAILS_URL,
    GITHUB_ORGS_URL,
    GITHUB_TEAMS_URL,
    GITHUB_USER_URL,
    GitHubOAuthClient,
    GitHubOAuthConfig,
    load_github_oauth_config_from_env,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.verireel_preview_driver import (
    VeriReelPreviewDestroyResult,
    VeriReelPreviewInventoryItem,
    VeriReelPreviewInventoryResult,
    VeriReelPreviewRefreshResult,
)
from control_plane.workflows.verireel_app_maintenance import VeriReelAppMaintenanceResult
from control_plane.workflows.verireel_prod_backup_gate import VeriReelProdBackupGateResult
from control_plane.workflows.verireel_prod_promotion import VeriReelProdPromotionResult
from control_plane.workflows.verireel_prod_rollback import VeriReelProdRollbackResult
from control_plane.workflows.verireel_stable_deploy import VeriReelStableDeployResult
from control_plane.workflows.verireel_environment import VeriReelStableEnvironmentResult
from control_plane.workflows.verireel_rollout import VeriReelRolloutVerificationResult
from control_plane.workflows.odoo_artifact_publish import OdooArtifactPublishResult
from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult
from control_plane.workflows.odoo_prod_backup_gate import OdooProdBackupGateResult
from control_plane.workflows.odoo_prod_promotion import OdooProdPromotionResult
from control_plane.workflows.odoo_prod_rollback import OdooProdRollbackResult
from control_plane.workflows.generic_web_promotion import GenericWebProdPromotionResult
from control_plane.workflows.generic_web_promotion_workflow import GenericWebPromotionWorkflowResult


class _StubVerifier:
    def __init__(self, identity: GitHubActionsIdentity):
        self.identity = identity

    def verify(self, token: str) -> GitHubActionsIdentity:
        if token != "valid-token":
            raise ValueError("OIDC bearer token is required.")
        return self.identity


class _StubGitHubOAuthClient:
    def __init__(self, identity: GitHubHumanIdentity):
        self.identity = identity
        self.code_verifier = ""

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return f"https://github.example/authorize?state={state}&challenge={code_challenge}"

    def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        authz_policy: LaunchplaneAuthzPolicy,
    ) -> GitHubHumanIdentity:
        self.code_verifier = code_verifier
        if code != "github-code":
            raise ValueError("unexpected code")
        return self.identity


class _FakeGitHubResponse:
    def __init__(self, payload: object):
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeOAuth2Session:
    def __init__(self, payloads: dict[str, object]):
        self.payloads = payloads
        self.requested_urls: list[str] = []
        self.token_request: dict[str, str] = {}

    def fetch_token(self, url: str, *, code: str, code_verifier: str) -> None:
        self.token_request = {
            "url": url,
            "code": code,
            "code_verifier": code_verifier,
        }

    def get(self, url: str) -> _FakeGitHubResponse:
        self.requested_urls.append(url)
        return _FakeGitHubResponse(self.payloads[url])


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


def _human_identity(*, role: Literal["read_only", "admin"] = "read_only") -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="alice",
        github_id=123,
        name="Alice Operator",
        email="alice@example.com",
        organizations=frozenset({"shinycomputers"}),
        teams=frozenset({"launchplane-readers", "shinycomputers/launchplane-readers"}),
        role=role,
    )


def _github_oauth_config() -> GitHubOAuthConfig:
    return GitHubOAuthConfig(
        client_id="client-id",
        client_secret="client-secret",
        public_url="https://launchplane.example",
        session_secret="test-session-secret",
        cookie_secure=False,
    )


def _signed_in_cookie(app) -> str:
    _, login_headers, _ = _invoke_raw_app(app, method="GET", path="/auth/github/login")
    state = parse_qs(urlparse(login_headers["Location"]).query)["state"][0]
    _, callback_headers, _ = _invoke_raw_app(
        app,
        method="GET",
        path="/auth/github/callback",
        query_string=f"code=github-code&state={state}",
    )
    return callback_headers["Set-Cookie"]


def _product_profile_payload(product: str = "sellyouroutboard") -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "display_name": "Sell Your Outboard",
        "repository": f"cbusillo/{product}",
        "driver_id": "generic-web",
        "image": {"repository": f"ghcr.io/cbusillo/{product}"},
        "runtime_port": 3000,
        "health_path": "/api/health",
        "lanes": (
            {
                "instance": "testing",
                "context": f"{product}-testing",
                "base_url": "https://testing.sellyouroutboard.com",
                "health_url": "https://testing.sellyouroutboard.com/api/health",
            },
        ),
        "preview": {
            "enabled": True,
            "context": f"{product}-testing",
            "slug_template": "pr-{number}",
        },
        "updated_at": "2026-04-30T21:30:00Z",
        "source": "test",
    }


def _product_profile_payload_with_prod(product: str = "sellyouroutboard") -> dict[str, object]:
    payload = _product_profile_payload(product)
    lanes = list(payload["lanes"])
    lanes.append(
        {
            "instance": "prod",
            "context": f"{product}-testing",
            "base_url": "https://www.sellyouroutboard.com",
            "health_url": "https://www.sellyouroutboard.com/api/health",
        }
    )
    payload["lanes"] = tuple(lanes)
    return payload


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _seed_tracked_target_records(
    *,
    database_url: str,
    context: str,
    instance: str,
    target_id: str,
    target_type: Literal["compose", "application"],
    target_name: str,
) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_dokploy_target_record(
            DokployTargetRecord(
                context=context,
                instance=instance,
                target_type=target_type,
                target_name=target_name,
                updated_at="2026-05-01T00:00:00Z",
                source_label="test",
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=context,
                instance=instance,
                target_id=target_id,
                updated_at="2026-05-01T00:00:00Z",
                source_label="test",
            )
        )
    finally:
        store.close()


def _product_config_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "dry-run",
        "product": "sellyouroutboard",
        "context": "sellyouroutboard-prod",
        "instance": "prod",
        "source_label": "product-config-api-test",
        "runtime_env": {
            "scope": "instance",
            "env": {
                "CONTACT_EMAIL_MODE": "smtp",
                "SELLYOUROUTBOARD_SITE_URL": "https://www.sellyouroutboard.com",
            },
        },
        "secrets": [
            {
                "name": "SMTP_PASSWORD",
                "binding_key": "SMTP_PASSWORD",
                "value": "smtp-secret-value",
                "scope": "context_instance",
                "description": "SMTP password",
            }
        ],
    }


def _invoke_app(
    app,
    *,
    method: str,
    path: str,
    query_string: str = "",
    payload: dict[str, object] | None = None,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
):
    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else b""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "CONTENT_LENGTH": str(len(body_bytes)),
        "wsgi.input": io.BytesIO(body_bytes),
        "HTTP_AUTHORIZATION": authorization,
    }
    for header_name, header_value in (headers or {}).items():
        environ[f"HTTP_{header_name.upper().replace('-', '_')}"] = header_value
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    response_body = b"".join(app(environ, start_response))
    return int(str(captured["status"]).split(" ", 1)[0]), json.loads(response_body.decode("utf-8"))


def _invoke_raw_app(
    app,
    *,
    method: str,
    path: str,
    authorization: str = "",
    query_string: str = "",
    headers: dict[str, str] | None = None,
):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "CONTENT_LENGTH": "0",
        "wsgi.input": io.BytesIO(b""),
        "HTTP_AUTHORIZATION": authorization,
    }
    for header_name, header_value in (headers or {}).items():
        environ[f"HTTP_{header_name.upper().replace('-', '_')}"] = header_value
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    response_body = b"".join(app(environ, start_response))
    return (
        int(str(captured["status"]).split(" ", 1)[0]),
        dict(captured["headers"]),
        response_body,
    )


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
                audience="launchplane.shinycomputers.com",
                jwk_client=mock_jwk_client,
            )
            identity = verifier.verify("header.payload.signature")

        mock_jwk_client.get_signing_key_from_jwt.assert_called_once_with("header.payload.signature")
        decode_mock.assert_called_once_with(
            "header.payload.signature",
            "signing-key",
            algorithms=["RS256"],
            audience="launchplane.shinycomputers.com",
            issuer="https://token.actions.githubusercontent.com",
        )
        self.assertEqual(identity.repository, "every/verireel")
        self.assertEqual(identity.workflow_ref, claims["workflow_ref"])

    def test_policy_wildcard_matches_branch_specific_workflow_ref(self) -> None:
        identity = _identity(
            repository="cbusillo/verireel",
            workflow_ref=(
                "cbusillo/verireel/.github/workflows/preview-control-plane.yml"
                "@refs/heads/code/2026-04-21-preview-validation-pr"
            ),
        )
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/verireel",
                        "workflow_refs": [
                            "cbusillo/verireel/.github/workflows/preview-control-plane.yml@*"
                        ],
                        "event_names": ["pull_request"],
                        "products": ["verireel"],
                        "contexts": ["verireel-testing"],
                        "actions": ["verireel_preview_refresh.execute"],
                    }
                ]
            }
        )

        self.assertTrue(
            policy.allows(
                identity=identity,
                action="verireel_preview_refresh.execute",
                product="verireel",
                context="verireel-testing",
            )
        )


class GitHubHumanAuthTests(unittest.TestCase):
    def test_github_oauth_config_loads_bootstrap_admin_emails(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LAUNCHPLANE_GITHUB_CLIENT_ID": "client-id",
                "LAUNCHPLANE_GITHUB_CLIENT_SECRET": "client-secret",
                "LAUNCHPLANE_PUBLIC_URL": "https://launchplane.example/",
                "LAUNCHPLANE_SESSION_SECRET": "session-secret",
                "LAUNCHPLANE_COOKIE_SECURE": "false",
                "LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS": (
                    " Info@ShinyComputers.com, ops@example.com "
                ),
            },
            clear=True,
        ):
            config = load_github_oauth_config_from_env()

        self.assertIsNotNone(config)
        assert config is not None
        self.assertFalse(config.cookie_secure)
        self.assertEqual(config.public_url, "https://launchplane.example")
        self.assertIn("user:email", config.scopes)
        self.assertEqual(
            config.bootstrap_admin_emails,
            frozenset({"info@shinycomputers.com", "ops@example.com"}),
        )

    def test_github_oauth_bootstrap_admin_can_use_verified_private_email(self) -> None:
        config = GitHubOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            public_url="https://launchplane.example",
            session_secret="session-secret",
            bootstrap_admin_emails=frozenset({"info@shinycomputers.com"}),
        )
        oauth_session = _FakeOAuth2Session(
            {
                GITHUB_USER_URL: {
                    "login": "bootstrapper",
                    "id": 987,
                    "name": "Bootstrap Operator",
                    "email": None,
                },
                GITHUB_ORGS_URL: [],
                GITHUB_TEAMS_URL: [],
                GITHUB_EMAILS_URL: [
                    {
                        "email": "info@shinycomputers.com",
                        "primary": True,
                        "verified": True,
                    },
                    {
                        "email": "unverified@example.com",
                        "primary": False,
                        "verified": False,
                    },
                ],
            }
        )
        client = GitHubOAuthClient(config)

        with patch.object(GitHubOAuthClient, "_new_session", return_value=oauth_session):
            identity = client.fetch_identity(
                code="github-code",
                code_verifier="verifier",
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_humans": []}),
            )

        self.assertEqual(identity.login, "bootstrapper")
        self.assertEqual(identity.email, "info@shinycomputers.com")
        self.assertEqual(identity.role, "admin")
        self.assertIn(GITHUB_EMAILS_URL, oauth_session.requested_urls)

    def _signed_in_cookie(
        self,
        app,
    ) -> str:
        _, login_headers, _ = _invoke_raw_app(app, method="GET", path="/auth/github/login")
        state = parse_qs(urlparse(login_headers["Location"]).query)["state"][0]
        _, callback_headers, _ = _invoke_raw_app(
            app,
            method="GET",
            path="/auth/github/callback",
            query_string=f"code=github-code&state={state}",
        )
        return callback_headers["Set-Cookie"]

    def test_github_oauth_callback_issues_session_cookie(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"logins": ["alice"], "roles": ["read_only"]}]}
        )
        oauth_client = _StubGitHubOAuthClient(_human_identity())
        with TemporaryDirectory() as tmpdir:
            app = create_launchplane_service_app(
                state_dir=Path(tmpdir),
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=oauth_client,  # type: ignore[arg-type]
            )
            status_code, headers, _ = _invoke_raw_app(
                app,
                method="GET",
                path="/auth/github/login",
                query_string="return_to=/ui",
            )
            self.assertEqual(status_code, 302)
            state = parse_qs(urlparse(headers["Location"]).query)["state"][0]

            status_code, headers, _ = _invoke_raw_app(
                app,
                method="GET",
                path="/auth/github/callback",
                query_string=f"code=github-code&state={state}",
            )

        self.assertEqual(status_code, 302)
        self.assertEqual(headers["Location"], "/ui")
        self.assertIn("launchplane_session=", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertTrue(oauth_client.code_verifier)

    def test_session_endpoint_reads_github_human_session(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"logins": ["alice"], "roles": ["read_only"]}]}
        )
        oauth_client = _StubGitHubOAuthClient(_human_identity())
        with TemporaryDirectory() as tmpdir:
            app = create_launchplane_service_app(
                state_dir=Path(tmpdir),
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=oauth_client,  # type: ignore[arg-type]
            )
            cookie = self._signed_in_cookie(app)
            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/auth/session",
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["identity"]["login"], "alice")
        self.assertEqual(payload["identity"]["role"], "read_only")

    def test_database_backed_human_session_survives_app_recreation(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"logins": ["alice"], "roles": ["read_only"]}]}
        )
        oauth_client = _StubGitHubOAuthClient(_human_identity())
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite+pysqlite:///{Path(tmpdir) / 'launchplane.sqlite3'}"
            app = create_launchplane_service_app(
                state_dir=Path(tmpdir) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                database_url=database_url,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=oauth_client,  # type: ignore[arg-type]
            )
            cookie = self._signed_in_cookie(app)
            recreated_app = create_launchplane_service_app(
                state_dir=Path(tmpdir) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                database_url=database_url,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=oauth_client,  # type: ignore[arg-type]
            )

            status_code, payload = _invoke_app(
                recreated_app,
                method="GET",
                path="/v1/auth/session",
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["identity"]["login"], "alice")

    def test_human_session_can_read_allowed_driver_metadata(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_humans": [
                    {
                        "logins": ["alice"],
                        "roles": ["read_only"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["driver.read"],
                    }
                ]
            }
        )
        oauth_client = _StubGitHubOAuthClient(_human_identity())
        with TemporaryDirectory() as tmpdir:
            app = create_launchplane_service_app(
                state_dir=Path(tmpdir),
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=oauth_client,  # type: ignore[arg-type]
            )
            cookie = self._signed_in_cookie(app)
            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/drivers",
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("drivers", payload)

    def test_read_only_human_session_rejects_runtime_read(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_humans": [
                    {
                        "logins": ["alice"],
                        "roles": ["read_only"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["driver.read"],
                    }
                ]
            }
        )
        oauth_client = _StubGitHubOAuthClient(_human_identity())
        with TemporaryDirectory() as tmpdir:
            app = create_launchplane_service_app(
                state_dir=Path(tmpdir),
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=oauth_client,  # type: ignore[arg-type]
            )
            cookie = self._signed_in_cookie(app)
            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/service/runtime",
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_human_session_does_not_authorize_post_mutations(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"logins": ["alice"], "roles": ["admin"]}]}
        )
        oauth_client = _StubGitHubOAuthClient(_human_identity(role="admin"))
        with TemporaryDirectory() as tmpdir:
            app = create_launchplane_service_app(
                state_dir=Path(tmpdir),
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=oauth_client,  # type: ignore[arg-type]
            )
            cookie = self._signed_in_cookie(app)
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-inventory",
                payload={"schema_version": 1, "context": "verireel-testing"},
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 401)
        self.assertEqual(payload["error"]["code"], "authentication_required")


class LaunchplaneServiceTests(unittest.TestCase):
    def test_health_endpoint_reports_storage_backend(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )

            status_code, payload = _invoke_app(
                app, method="GET", path="/v1/health", authorization=""
            )

            self.assertEqual(status_code, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["storage_backend"], "filesystem")

    def test_service_runtime_endpoint_reports_current_image_reference(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service.read"],
                        }
                    ]
                }
            )
            policy_text = "schema_version = 1\n"
            with patch.dict(
                os.environ,
                {
                    "DOCKER_IMAGE_REFERENCE": "ghcr.io/cbusillo/launchplane@sha256:test",
                    "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane.shinycomputers.com",
                    "LAUNCHPLANE_POLICY_B64": base64.b64encode(policy_text.encode("utf-8")).decode(
                        "ascii"
                    ),
                },
                clear=True,
            ):
                app = create_launchplane_service_app(
                    state_dir=Path(temporary_directory_name) / "state",
                    verifier=_StubVerifier(
                        _identity(
                            repository="cbusillo/launchplane",
                            workflow_ref=(
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ),
                            event_name="workflow_dispatch",
                        )
                    ),
                    authz_policy=policy,
                    control_plane_root_path=Path(temporary_directory_name),
                )

                status_code, payload = _invoke_app(app, method="GET", path="/v1/service/runtime")

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["runtime"]["docker_image_reference"],
            "ghcr.io/cbusillo/launchplane@sha256:test",
        )
        self.assertEqual(
            payload["runtime"]["authz_policy_sha256"],
            authz_policy_sha256(policy),
        )
        self.assertEqual(payload["runtime"]["authz_policy_source"], "bootstrap_seeded_store")
        self.assertEqual(
            payload["runtime"]["bootstrap_authz_policy_sha256"],
            hashlib.sha256(policy_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(payload["runtime"]["service_audience"], "launchplane.shinycomputers.com")

    def test_service_uses_db_backed_authz_policy_when_present(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = (
                f"sqlite+pysqlite:///{Path(temporary_directory_name) / 'launchplane.sqlite3'}"
            )
            db_policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service.read"],
                        }
                    ]
                }
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_authz_policy_record(
                LaunchplaneAuthzPolicyRecord(
                    record_id="launchplane-authz-policy-test",
                    status="active",
                    source="test",
                    updated_at="2026-04-20T10:05:00Z",
                    policy=db_policy,
                )
            )
            store.close()

            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                database_url=database_url,
                control_plane_root_path=Path(temporary_directory_name),
            )

            status_code, payload = _invoke_app(app, method="GET", path="/v1/service/runtime")

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["runtime"]["authz_policy_sha256"], authz_policy_sha256(db_policy))
        self.assertEqual(payload["runtime"]["authz_policy_source"], "db")

    def test_ui_route_serves_static_shell_without_authentication(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            asset_root = ui_root / "assets"
            asset_root.mkdir(parents=True)
            (ui_root / "index.html").write_text(
                '<html><head><script type="module" src="/ui/assets/app.js"></script></head></html>',
                encoding="utf-8",
            )
            (asset_root / "app.js").write_text("console.log('launchplane ui');\n", encoding="utf-8")
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            shell_status, shell_headers, shell_body = _invoke_raw_app(app, method="GET", path="/ui")
            asset_status, asset_headers, asset_body = _invoke_raw_app(
                app, method="GET", path="/ui/assets/app.js"
            )

        self.assertEqual(shell_status, 200)
        self.assertEqual(shell_headers["Content-Type"], "text/html")
        self.assertIn(b"/ui/assets/app.js", shell_body)
        self.assertEqual(shell_headers["Cache-Control"], "no-store")
        self.assertEqual(asset_status, 200)
        self.assertIn(asset_headers["Content-Type"], {"text/javascript", "application/javascript"})
        self.assertIn(b"launchplane ui", asset_body)
        self.assertEqual(asset_headers["Cache-Control"], "public, max-age=31536000, immutable")

    def test_root_route_serves_ui_shell_without_authentication(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text(
                '<html><head><script type="module" src="/ui/assets/app.js"></script></head></html>',
                encoding="utf-8",
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            status_code, headers, body = _invoke_raw_app(app, method="GET", path="/")

        self.assertEqual(status_code, 200)
        self.assertEqual(headers["Content-Type"], "text/html")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"/ui/assets/app.js", body)

    def test_ui_route_falls_back_to_shell_for_nested_paths(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text("<html>Launchplane UI</html>", encoding="utf-8")
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            status_code, headers, body = _invoke_raw_app(
                app, method="GET", path="/ui/contexts/verireel"
            )

        self.assertEqual(status_code, 200)
        self.assertEqual(headers["Content-Type"], "text/html")
        self.assertIn(b"Launchplane UI", body)

    def test_ui_asset_route_rejects_parent_directory_segments(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text("<html>Launchplane UI</html>", encoding="utf-8")
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            status_code, headers, body = _invoke_raw_app(
                app, method="GET", path="/ui/assets/%2e%2e/index.html"
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn(b"Launchplane UI", body)

    def test_driver_descriptor_endpoints_return_provider_neutral_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["driver.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )

            list_status_code, list_payload = _invoke_app(app, method="GET", path="/v1/drivers")
            show_status_code, show_payload = _invoke_app(app, method="GET", path="/v1/drivers/odoo")

        self.assertEqual(list_status_code, 200)
        self.assertEqual(
            [driver["driver_id"] for driver in list_payload["drivers"]],
            ["generic-web", "odoo", "verireel"],
        )
        self.assertNotIn("Dokploy", json.dumps(list_payload["drivers"]))
        self.assertEqual(show_status_code, 200)
        self.assertEqual(show_payload["driver"]["driver_id"], "odoo")
        rollback_actions = [
            action
            for action in show_payload["driver"]["actions"]
            if action["action_id"] == "prod_rollback"
        ]
        self.assertEqual(rollback_actions[0]["safety"], "destructive")

    def test_driver_descriptor_endpoint_returns_not_found_for_unknown_driver(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["driver.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/drivers/missing",
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_tracked_target_logs_endpoint_returns_redacted_application_logs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["target_logs.read"],
                        }
                    ]
                }
            )
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("ok", "RESEND_API_KEY=[redacted]"),
                ) as logs_mock,
            ):
                app = create_launchplane_service_app(
                    state_dir=root / "state",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    control_plane_root_path=root,
                    database_url=database_url,
                )
                status_code, payload = _invoke_app(
                    app,
                    method="GET",
                    path="/v1/contexts/sellyouroutboard-testing/instances/testing/logs",
                    query_string="lines=2&since=5m&search=contact",
                )

        self.assertEqual(status_code, 200)
        logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            application_id="app-123",
            line_count=2,
            since="5m",
            search="contact",
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["target"]["target_name"], "syo-testing-app")
        self.assertEqual(payload["target"]["app_name"], "syo-testing-gfbiqh")
        self.assertEqual(payload["request"], {"line_count": 2, "since": "5m", "search": "contact"})
        self.assertEqual(payload["logs"]["lines"], ["ok", "RESEND_API_KEY=[redacted]"])
        self.assertNotIn("secret-token", json.dumps(payload))

    def test_tracked_target_logs_endpoint_requires_authz_action(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["driver.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/sellyouroutboard-testing/instances/testing/logs",
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_tracked_target_logs_endpoint_requires_db_backed_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["target_logs.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/sellyouroutboard-testing/instances/testing/logs",
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "database_required")

    def test_product_profile_endpoints_round_trip_authorized_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane", "sellyouroutboard"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.write", "product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            write_status_code, write_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-profiles",
                payload=_product_profile_payload(),
                headers={"Idempotency-Key": "profile-sellyouroutboard"},
            )
            show_status_code, show_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles/sellyouroutboard",
            )
            list_status_code, _, list_body = _invoke_raw_app(
                app,
                method="GET",
                path="/v1/product-profiles",
                authorization="Bearer valid-token",
                query_string="driver_id=generic-web",
            )
            list_payload = json.loads(list_body.decode("utf-8"))

        self.assertEqual(write_status_code, 202)
        self.assertEqual(write_payload["records"], {"product_profile": "sellyouroutboard"})
        self.assertEqual(show_status_code, 200)
        self.assertEqual(show_payload["profile"]["driver_id"], "generic-web")
        self.assertEqual(show_payload["profile"]["preview"]["slug_template"], "pr-{number}")
        self.assertEqual(list_status_code, 200)
        self.assertEqual(list_payload["driver_id"], "generic-web")
        self.assertEqual(
            [profile["product"] for profile in list_payload["profiles"]],
            ["sellyouroutboard"],
        )

    def test_product_context_cutover_endpoint_updates_profile_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.write", "product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
            finally:
                store.close()

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-profiles/context-cutover/apply",
                payload={
                    "product": "sellyouroutboard",
                    "source_context": "sellyouroutboard-testing",
                    "target_context": "sellyouroutboard",
                    "mode": "apply",
                    "display_name": "SellYourOutboard",
                    "source_label": "test:context-cutover",
                },
                headers={"Idempotency-Key": "profile-context-cutover"},
            )
            show_status_code, show_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles/sellyouroutboard",
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"], {"product_profile": "sellyouroutboard"})
        self.assertEqual(payload["result"]["profile"]["display_name"], "SellYourOutboard")
        self.assertEqual(show_status_code, 200)
        self.assertEqual(show_payload["profile"]["display_name"], "SellYourOutboard")
        self.assertEqual(
            {lane["context"] for lane in show_payload["profile"]["lanes"]},
            {"sellyouroutboard"},
        )
        self.assertEqual(show_payload["profile"]["preview"]["context"], "sellyouroutboard")

    def test_product_profile_context_cutover_audit_returns_redacted_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="sellyouroutboard-testing",
                        instance="prod",
                        env={"TAWK_PROPERTY_ID": "property-legacy"},
                        updated_at="2026-05-01T00:03:00Z",
                        source_label="legacy",
                    )
                )
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="sellyouroutboard",
                        instance="prod",
                        env={"TAWK_WIDGET_ID": "widget-canonical"},
                        updated_at="2026-05-01T00:04:00Z",
                        source_label="operator:mistake",
                    )
                )
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles/sellyouroutboard/context-cutover-audit",
                query_string=(
                    "source_context=sellyouroutboard-testing&target_context=sellyouroutboard"
                ),
            )

        payload_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(status_code, 200)
        self.assertEqual(payload["audit"]["status"], "ok")
        self.assertNotIn("property-legacy", payload_text)
        self.assertNotIn("widget-canonical", payload_text)
        self.assertEqual(
            payload["audit"]["contexts"]["source"]["runtime_environment_records"][0]["env_keys"],
            ["TAWK_PROPERTY_ID"],
        )
        self.assertEqual(
            payload["audit"]["contexts"]["target"]["runtime_environment_records"][0]["env_keys"],
            ["TAWK_WIDGET_ID"],
        )

    def test_product_profile_context_cutover_audit_rejects_unowned_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles/sellyouroutboard/context-cutover-audit",
                query_string="source_context=opw&target_context=sellyouroutboard",
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "context_not_in_product_boundary")

    def test_product_profile_context_cutover_audit_invalid_request_is_generic(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles/sellyouroutboard/context-cutover-audit",
                query_string=("source_context=sellyouroutboard&target_context=sellyouroutboard"),
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_context_cutover_audit_request")
        self.assertEqual(
            payload["error"]["message"],
            "Context cutover audit request is invalid.",
        )

    def test_product_profile_context_cutover_audit_rejects_unauthorized_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles/sellyouroutboard/context-cutover-audit",
                query_string=(
                    "source_context=sellyouroutboard-testing&target_context=sellyouroutboard"
                ),
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(payload["authz"]["identity"]["repository"], "every/verireel")
        self.assertEqual(payload["authz"]["policy_source"], "bootstrap_seeded_store")

    def test_product_profile_list_denial_includes_authz_diagnostics(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles",
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(payload["authz"]["identity"]["repository"], "every/verireel")
        self.assertEqual(payload["authz"]["policy_source"], "bootstrap_seeded_store")

    def test_product_profile_write_rejects_unauthorized_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-profiles",
                payload=_product_profile_payload(),
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_product_config_api_dry_run_returns_redacted_plan_without_writes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-prod"],
                            "actions": ["product_config.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/product-config/apply",
                    payload=_product_config_payload(),
                    headers={"Idempotency-Key": "product-config-dry-run"},
                )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                runtime_records = store.list_runtime_environment_records()
                secret_records = store.list_secret_records()
            finally:
                store.close()

        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertEqual(payload["result"]["runtime_environment"]["action"], "created")
        self.assertEqual(payload["result"]["secrets"][0]["action"], "created")
        self.assertNotIn("smtp-secret-value", response_text)
        self.assertNotIn("https://www.sellyouroutboard.com", response_text)
        self.assertEqual(runtime_records, ())
        self.assertEqual(secret_records, ())

    def test_product_config_api_apply_writes_runtime_and_managed_secret(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-prod"],
                            "actions": ["product_config.apply"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            request_payload = {**_product_config_payload(), "mode": "apply"}

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/product-config/apply",
                    payload=request_payload,
                    headers={"Idempotency-Key": "product-config-apply"},
                )
                store = PostgresRecordStore(database_url=database_url)
                store.ensure_schema()
                try:
                    runtime_records = store.list_runtime_environment_records()
                    secret_records = store.list_secret_records()
                    secret_binding = store.list_secret_bindings(limit=None)[0]
                finally:
                    store.close()

        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(payload["result"]["runtime_environment"]["action"], "created")
        self.assertEqual(payload["result"]["secrets"][0]["action"], "created")
        self.assertNotIn("smtp-secret-value", response_text)
        self.assertNotIn("https://www.sellyouroutboard.com", response_text)
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(
            runtime_records[0],
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard-prod",
                instance="prod",
                env={
                    "CONTACT_EMAIL_MODE": "smtp",
                    "SELLYOUROUTBOARD_SITE_URL": "https://www.sellyouroutboard.com",
                },
                updated_at=runtime_records[0].updated_at,
                source_label="product-config-api-test",
            ),
        )
        self.assertEqual(len(secret_records), 1)
        self.assertEqual(secret_records[0].name, "SMTP_PASSWORD")
        self.assertEqual(secret_binding.binding_key, "SMTP_PASSWORD")

    def test_product_config_api_rejects_missing_master_key_for_secret_bundle(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-prod"],
                            "actions": ["product_config.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with patch.dict(os.environ, {}, clear=True):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/product-config/apply",
                    payload=_product_config_payload(),
                )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "secret_configuration_required")
        self.assertEqual(
            payload["error"]["message"],
            "Launchplane service is missing required secret write configuration.",
        )
        self.assertNotIn("LAUNCHPLANE_MASTER_ENCRYPTION_KEY", json.dumps(payload))

    def test_product_config_api_rejects_secret_shaped_runtime_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-prod"],
                            "actions": ["product_config.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            request_payload = _product_config_payload()
            request_payload["secrets"] = []
            request_payload["runtime_env"] = {"scope": "instance", "env": {"API_TOKEN": "nope"}}

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/product-config/apply",
                    payload=request_payload,
                )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")
        self.assertNotIn("API_TOKEN", json.dumps(payload))

    def test_product_config_api_apply_requires_apply_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-prod"],
                            "actions": ["product_config.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            request_payload = {**_product_config_payload(), "mode": "apply"}

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-config/apply",
                payload=request_payload,
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_product_config_api_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["product_config.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-config/apply",
                payload=_product_config_payload(),
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_product_config_api_rejects_runtime_env_target_override(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-prod"],
                            "actions": ["product_config.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            request_payload = _product_config_payload()
            runtime_env = dict(request_payload["runtime_env"])
            runtime_env["context"] = "sellyouroutboard-testing"
            request_payload["runtime_env"] = runtime_env

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-config/apply",
                payload=request_payload,
            )

        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")
        self.assertNotIn("sellyouroutboard-testing", response_text)

    def test_product_config_api_rejects_secret_scope_override(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-prod"],
                            "actions": ["product_config.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            request_payload = _product_config_payload()
            request_payload["secrets"] = [{**request_payload["secrets"][0], "scope": "global"}]

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-config/apply",
                payload=request_payload,
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")

    def test_product_config_api_rejects_secret_target_override(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-prod"],
                            "actions": ["product_config.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            request_payload = _product_config_payload()
            request_payload["secrets"] = [
                {
                    **request_payload["secrets"][0],
                    "context": "sellyouroutboard-testing",
                }
            ]

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-config/apply",
                payload=request_payload,
            )

        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")
        self.assertNotIn("sellyouroutboard-testing", response_text)

    def test_generic_web_deploy_route_uses_profile_lane_for_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = SimpleNamespace(deployment_record_id="deployment-syo-testing")

            with patch(
                "control_plane.service.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-syo-testing"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-testing")
        deploy.assert_called_once()
        _, kwargs = deploy.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")
        self.assertEqual(kwargs["lane"].context, "sellyouroutboard-testing")

    def test_generic_web_deploy_route_rejects_wrong_product_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["different-context"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/deploy",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "deploy": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_prod_promotion_route_executes_for_authorized_product_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="deployment-syo-prod",
                    inventory_record_id="sellyouroutboard-testing-prod",
                    promotion_status="pass",
                    deployment_status="pass",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    target_name="syo-prod-app",
                    target_type="application",
                    target_id="app-123",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-prod-promotion-syo"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["promotion_record_id"], "promotion-syo-testing-to-prod")
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-prod")
        self.assertEqual(payload["records"]["inventory_record_id"], "sellyouroutboard-testing-prod")
        self.assertEqual(payload["result"]["source_health_status"], "pass")
        self.assertEqual(payload["result"]["destination_health_status"], "pass")
        execute_mock.assert_called_once()

    def test_generic_web_prod_promotion_route_rejects_wrong_product_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["different-context"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
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
                path="/v1/drivers/generic-web/prod-promotion",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "promotion": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_human_session_can_dry_run_generic_web_prod_promotion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=_StubGitHubOAuthClient(_human_identity(role="admin")),  # type: ignore[arg-type]
            )
            cookie = _signed_in_cookie(app)

            with patch(
                "control_plane.service.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="",
                    inventory_record_id="",
                    promotion_status="pending",
                    deployment_status="skipped",
                    backup_status="skipped",
                    source_health_status="pending",
                    destination_health_status="pending",
                    dry_run=True,
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                            "dry_run": True,
                        },
                    },
                    authorization="",
                    headers={"Cookie": cookie},
                )

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["dry_run"])
        self.assertEqual(payload["result"]["deployment_status"], "skipped")
        self.assertEqual(payload["records"]["deployment_record_id"], "")
        self.assertEqual(payload["records"]["inventory_record_id"], "")
        execute_mock.assert_called_once()

    def test_human_session_cannot_live_execute_generic_web_prod_promotion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=_StubGitHubOAuthClient(_human_identity(role="admin")),  # type: ignore[arg-type]
            )
            cookie = _signed_in_cookie(app)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "promotion": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                        "dry_run": False,
                    },
                },
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_human_session_can_dispatch_generic_web_promotion_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=_StubGitHubOAuthClient(_human_identity(role="admin")),  # type: ignore[arg-type]
            )
            cookie = _signed_in_cookie(app)

            with patch(
                "control_plane.service.dispatch_generic_web_promotion_workflow",
                return_value=GenericWebPromotionWorkflowResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    repository="cbusillo/sellyouroutboard",
                    workflow_id="promote-prod.yml",
                    ref="main",
                    dry_run=False,
                    bump="patch",
                    run_id=25237186636,
                    run_url="https://github.com/cbusillo/sellyouroutboard/actions/runs/25237186636",
                    run_status="queued",
                ),
            ) as dispatch_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion-workflow",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "workflow": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "context": "sellyouroutboard-testing",
                            "dry_run": False,
                            "bump": "patch",
                            "observe_timeout_seconds": 0,
                        },
                    },
                    authorization="",
                    headers={"Cookie": cookie},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(payload["result"]["workflow_id"], "promote-prod.yml")
        self.assertFalse(payload["result"]["dry_run"])
        self.assertEqual(payload["result"]["run_id"], 25237186636)
        self.assertEqual(payload["records"], {})
        dispatch_mock.assert_called_once()

    def test_generic_web_promotion_workflow_rejects_unauthorized_human(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_humans": []}),
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                github_oauth_client=_StubGitHubOAuthClient(_human_identity(role="admin")),  # type: ignore[arg-type]
            )
            cookie = _signed_in_cookie(app)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "workflow": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "dry_run": False,
                    },
                },
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_product_profile_appears_in_driver_view(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["driver.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/sellyouroutboard-testing/instances/prod/driver-view",
            )

        self.assertEqual(status_code, 200)
        driver = payload["view"]["drivers"][0]
        self.assertEqual(driver["driver_id"], "sellyouroutboard")
        self.assertEqual(driver["descriptor"]["base_driver_id"], "generic-web")
        self.assertEqual(driver["descriptor"]["product"], "sellyouroutboard")
        self.assertEqual(
            driver["available_actions"][1]["route_path"], "/v1/drivers/generic-web/prod-promotion"
        )

    def test_generic_web_preview_desired_state_route_uses_profile_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_desired_state.discover"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            record = PreviewDesiredStateRecord(
                desired_state_id="preview-desired-state-syo-testing-1",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                source="generic-web-preview",
                discovered_at="2026-04-30T21:00:00Z",
                repository="cbusillo/sellyouroutboard",
                label="preview",
                anchor_repo="sellyouroutboard",
                status="pass",
                desired_count=0,
            )

            with patch(
                "control_plane.service.discover_generic_web_preview_desired_state",
                return_value=record,
            ) as discover:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-desired-state",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "desired_state": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-desired-state:syo"},
                )
            records = FilesystemRecordStore(state_dir=state_dir).list_preview_desired_state_records(
                context_name="sellyouroutboard-testing"
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["preview_desired_state_id"],
            "preview-desired-state-syo-testing-1",
        )
        discover.assert_called_once()
        _, kwargs = discover.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")
        self.assertEqual(kwargs["profile"].preview.context, "sellyouroutboard-testing")
        self.assertEqual(records[0].desired_state_id, "preview-desired-state-syo-testing-1")

    def test_generic_web_preview_desired_state_route_rejects_wrong_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["different-context"],
                            "actions": ["preview_desired_state.discover"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/preview-desired-state",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "desired_state": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_preview_inventory_route_writes_scan_from_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = SimpleNamespace(
                context="sellyouroutboard-testing",
                source="generic-web-preview-inventory",
                previews=(SimpleNamespace(previewSlug="pr-42"),),
            )

            with patch(
                "control_plane.service.execute_generic_web_preview_inventory",
                return_value=driver_result,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-inventory",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "inventory": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                        },
                    },
                )
            records = FilesystemRecordStore(
                state_dir=state_dir
            ).list_preview_inventory_scan_records(context_name="sellyouroutboard-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["preview_inventory_scan_id"],
            records[0].scan_id,
        )
        self.assertEqual(records[0].source, "generic-web-preview-inventory")
        self.assertEqual(records[0].preview_slugs, ("pr-42",))

    def test_generic_web_preview_refresh_route_returns_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_generic_web_preview_refresh",
                return_value={"refresh_status": "pass", "application_id": "app-preview"},
            ) as refresh:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "refresh": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "pr-42",
                            "preview_url": "https://pr-42.example.test",
                            "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:pr-42"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["refresh_status"], "pass")
        self.assertEqual(payload["result"]["application_id"], "app-preview")
        refresh.assert_called_once()
        _, kwargs = refresh.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")

    def test_generic_web_preview_readiness_route_returns_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_readiness.evaluate"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.evaluate_generic_web_preview_readiness",
                return_value={
                    "readiness_status": "blocked",
                    "missing_template_env_keys": ["SMTP_HOST"],
                },
            ) as readiness:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-readiness",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "readiness": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                        },
                    },
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["readiness_status"], "blocked")
        self.assertEqual(payload["result"]["missing_template_env_keys"], ["SMTP_HOST"])
        readiness.assert_called_once()
        _, kwargs = readiness.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")

    def test_driver_context_view_endpoint_returns_lane_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="opw",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    resolved_target=ResolvedTargetEvidence(
                        target_type="compose",
                        target_id="target-123",
                        target_name="opw-testing",
                    ),
                    deploy=DeploymentEvidence(
                        target_name="opw-testing",
                        target_type="compose",
                        deploy_mode="runtime-provider-api",
                        deployment_id="deployment-provider-1",
                        status="pass",
                        started_at="2026-04-20T15:30:00Z",
                        finished_at="2026-04-20T15:32:00Z",
                    ),
                )
            )
            store.write_environment_inventory(
                EnvironmentInventory(
                    context="opw",
                    instance="testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="opw-testing",
                        target_type="compose",
                        deploy_mode="runtime-provider-api",
                        deployment_id="deployment-provider-1",
                        status="pass",
                        started_at="2026-04-20T15:30:00Z",
                        finished_at="2026-04-20T15:32:00Z",
                    ),
                    updated_at="2026-04-20T15:33:00Z",
                    deployment_record_id="deployment-20260420T153000Z-opw-testing",
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane"],
                            "contexts": ["opw"],
                            "actions": ["driver.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/opw/instances/testing/driver-view",
            )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["view"]["context"], "opw")
        self.assertEqual(payload["view"]["instance"], "testing")
        self.assertEqual(len(payload["view"]["drivers"]), 1)
        driver = payload["view"]["drivers"][0]
        self.assertEqual(driver["driver_id"], "odoo")
        self.assertEqual(
            driver["lane_summary"]["latest_deployment"]["record_id"],
            "deployment-20260420T153000Z-opw-testing",
        )
        self.assertEqual(
            driver["lane_summary"]["inventory"]["artifact_identity"]["artifact_id"],
            "artifact-20260420-a1b2c3d4",
        )
        self.assertEqual(driver["lane_summary"]["provenance"]["source_kind"], "record")
        self.assertEqual(
            driver["lane_summary"]["provenance"]["source_record_id"],
            "deployment-20260420T153000Z-opw-testing",
        )
        self.assertIn(
            driver["lane_summary"]["provenance"]["freshness_status"],
            {"verified", "recorded", "stale"},
        )

    def test_driver_context_view_endpoint_returns_preview_summaries(self) -> None:
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
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane"],
                            "contexts": ["verireel-testing"],
                            "actions": ["driver.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/verireel-testing/driver-view",
            )

        self.assertEqual(status_code, 200)
        driver = payload["view"]["drivers"][0]
        self.assertEqual(driver["driver_id"], "verireel")
        self.assertEqual(driver["preview_summaries"][0]["latest_generation"]["state"], "ready")
        self.assertEqual(
            driver["preview_summaries"][0]["provenance"]["source_record_id"],
            "preview-verireel-testing-verireel-pr-123-generation-0001",
        )
        self.assertIn(
            driver["preview_summaries"][0]["provenance"]["freshness_status"],
            {"verified", "recorded", "stale"},
        )
        destructive_actions = [
            action for action in driver["available_actions"] if action["safety"] == "destructive"
        ]
        self.assertEqual(
            {action["action_id"] for action in destructive_actions},
            {"prod_rollback", "preview_destroy"},
        )

    def test_data_freshness_report_covers_visible_surfaces(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            for instance_name in ("prod", "testing"):
                store.write_environment_inventory(
                    EnvironmentInventory(
                        context="verireel",
                        instance=instance_name,
                        artifact_identity=ArtifactIdentityReference(
                            artifact_id=f"artifact-verireel-{instance_name}"
                        ),
                        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                        deploy=DeploymentEvidence(
                            target_name=f"verireel-{instance_name}",
                            target_type="application",
                            deploy_mode="runtime-provider-api",
                            deployment_id=f"provider-{instance_name}",
                            status="pass",
                            started_at="2026-04-20T15:30:00Z",
                            finished_at="2026-04-20T15:32:00Z",
                        ),
                        updated_at="2026-04-20T15:33:00Z",
                        deployment_record_id=f"deployment-verireel-{instance_name}",
                    )
                )
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

            result = runner.invoke(
                main,
                [
                    "service",
                    "inspect-data-freshness",
                    "--state-dir",
                    str(state_dir),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["surface_count"], 3)
        self.assertEqual(payload["missing_provenance_count"], 0)
        self.assertEqual(
            {surface["name"] for surface in payload["surfaces"]},
            {
                "verireel/prod/lane",
                "verireel/testing/lane",
                "verireel-testing/preview-verireel-testing-verireel-pr-123",
            },
        )

    def test_data_freshness_report_uses_empty_preview_inventory_scan(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:05:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=0,
                    preview_slugs=(),
                )
            )

            result = runner.invoke(
                main,
                [
                    "service",
                    "inspect-data-freshness",
                    "--state-dir",
                    str(state_dir),
                ],
            )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        payload = json.loads(result.output.split("\nError:", maxsplit=1)[0])
        self.assertEqual(payload["status"], "rejected")
        preview_surface = next(
            surface
            for surface in payload["surfaces"]
            if surface["name"] == "verireel-testing/preview-inventory"
        )
        self.assertTrue(preview_surface["has_provenance"])
        self.assertEqual(
            preview_surface["source_record_id"],
            "preview-inventory-scan-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(payload["missing_provenance_count"], 2)

    def test_driver_context_view_endpoint_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane"],
                            "contexts": ["opw"],
                            "actions": ["driver.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/cm/instances/prod/driver-view",
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_self_deploy_endpoint_updates_target_env_and_triggers_dokploy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy_text = "schema_version = 1\n"
            policy_b64 = base64.b64encode(policy_text.encode("utf-8")).decode("ascii")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )

            with (
                patch(
                    "control_plane.service.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.service.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "env": (
                            "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/launchplane@sha256:old\n"
                            "LAUNCHPLANE_POLICY_TOML=schema_version = 1\n"
                            "LAUNCHPLANE_POLICY_FILE=/etc/launchplane/policy.toml\n"
                        )
                    },
                ),
                patch(
                    "control_plane.service.control_plane_dokploy.update_dokploy_target_env"
                ) as update_env_mock,
                patch(
                    "control_plane.service.control_plane_dokploy.trigger_deployment"
                ) as trigger_mock,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/launchplane/self-deploy",
                    payload={
                        "product": "launchplane",
                        "deploy": {
                            "target_type": "compose",
                            "target_id": "compose-123",
                            "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                            "policy_b64": policy_b64,
                            "oauth_env": {
                                "LAUNCHPLANE_GITHUB_CLIENT_ID": "client-id",
                                "LAUNCHPLANE_PUBLIC_URL": "https://launchplane.example",
                                "LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS": ("info@shinycomputers.com"),
                            },
                        },
                    },
                    headers={"Idempotency-Key": "launchplane-self-deploy:test"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["target_id"], "compose-123")
        self.assertEqual(payload["records"]["target_type"], "compose")
        self.assertEqual(
            payload["records"]["image_reference"],
            "ghcr.io/cbusillo/launchplane@sha256:new",
        )
        update_env_mock.assert_called_once()
        updated_env_text = update_env_mock.call_args.kwargs["env_text"]
        self.assertIn(
            "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/launchplane@sha256:new", updated_env_text
        )
        self.assertIn(f"LAUNCHPLANE_POLICY_B64={policy_b64}", updated_env_text)
        self.assertIn("LAUNCHPLANE_GITHUB_CLIENT_ID=client-id", updated_env_text)
        self.assertIn("LAUNCHPLANE_PUBLIC_URL=https://launchplane.example", updated_env_text)
        self.assertIn(
            "LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS=info@shinycomputers.com",
            updated_env_text,
        )
        self.assertNotIn("LAUNCHPLANE_POLICY_TOML=", updated_env_text)
        self.assertNotIn("LAUNCHPLANE_POLICY_FILE=", updated_env_text)
        trigger_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="token-123",
            target_type="compose",
            target_id="compose-123",
            no_cache=False,
        )

    def test_self_deploy_endpoint_rejects_unknown_oauth_env_keys(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/launchplane/self-deploy",
                payload={
                    "product": "launchplane",
                    "deploy": {
                        "target_type": "compose",
                        "target_id": "compose-123",
                        "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                        "oauth_env": {"DOKPLOY_TOKEN": "nope"},
                    },
                },
                headers={"Idempotency-Key": "launchplane-self-deploy:bad-oauth-env"},
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_authz_policy_grant_endpoint_writes_db_record_and_updates_runtime(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["sellyouroutboard"],
                        "contexts": ["launchplane"],
                        "actions": ["product_profile.read"],
                        "source_label": "test:audit-grant",
                    },
                },
                headers={"Idempotency-Key": "authz-grant:audit"},
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_policy = store.list_authz_policy_records(status="active", limit=1)[0]
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
            finally:
                store.close()
            profile_status_code, profile_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles/sellyouroutboard",
            )
            repeat_status_code, repeat_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["sellyouroutboard"],
                        "contexts": ["launchplane"],
                        "actions": ["product_profile.read"],
                        "source_label": "test:audit-grant",
                    },
                },
                headers={"Idempotency-Key": "authz-grant:audit-repeat"},
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["authz_policy_record_id"], active_policy.record_id)
        self.assertEqual(payload["result"]["changed"], True)
        self.assertNotIn("workflow_refs", json.dumps(payload, sort_keys=True))
        self.assertEqual(profile_status_code, 200)
        self.assertEqual(profile_payload["profile"]["product"], "sellyouroutboard")
        self.assertEqual(repeat_status_code, 202)
        self.assertEqual(
            repeat_payload["records"]["authz_policy_record_id"], active_policy.record_id
        )
        self.assertEqual(repeat_payload["result"]["changed"], False)

    def test_authz_policy_grant_endpoint_rejects_without_self_deploy_permission(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "actions": ["product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "actions": ["product_profile.read"],
                    },
                },
                headers={"Idempotency-Key": "authz-grant:unauthorized"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_preview_generation_endpoint_writes_records_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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

    def test_deployment_endpoint_replays_idempotent_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
            request_payload = {
                "product": "odoo",
                "deployment": {
                    "record_id": "deployment-20260420T153000Z-opw-testing",
                    "artifact_identity": {"artifact_id": "artifact-20260420-a1b2c3d4"},
                    "context": "opw",
                    "instance": "testing",
                    "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
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
            }

            first_status_code, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/deployments",
                payload=request_payload,
                headers={"Idempotency-Key": "deployment-opw-testing-123"},
            )
            second_status_code, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/deployments",
                payload=request_payload,
                headers={"Idempotency-Key": "deployment-opw-testing-123"},
            )

            self.assertEqual(first_status_code, 202)
            self.assertEqual(second_status_code, 202)
            self.assertEqual(first_payload["records"], second_payload["records"])
            self.assertTrue(second_payload["replayed"])
            self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])

    def test_deployment_endpoint_rejects_idempotency_key_reuse_for_different_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
            first_request_payload = {
                "product": "odoo",
                "deployment": {
                    "record_id": "deployment-20260420T153000Z-opw-testing",
                    "artifact_identity": {"artifact_id": "artifact-20260420-a1b2c3d4"},
                    "context": "opw",
                    "instance": "testing",
                    "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
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
            }
            second_request_payload = json.loads(json.dumps(first_request_payload))
            second_request_payload["deployment"]["record_id"] = (
                "deployment-20260420T153100Z-opw-testing"
            )
            second_request_payload["deployment"]["artifact_identity"]["artifact_id"] = (
                "artifact-20260420-e5f6g7h8"
            )

            first_status_code, _ = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/deployments",
                payload=first_request_payload,
                headers={"Idempotency-Key": "deployment-opw-testing-123"},
            )
            second_status_code, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/evidence/deployments",
                payload=second_request_payload,
                headers={"Idempotency-Key": "deployment-opw-testing-123"},
            )

            self.assertEqual(first_status_code, 202)
            self.assertEqual(second_status_code, 409)
            self.assertEqual(second_payload["error"]["code"], "idempotency_key_reused")

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
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
                            "evidence": {"recorded_by": "launchplane-service"},
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

    def test_backup_gate_endpoint_writes_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["backup_gate.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
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
                path="/v1/evidence/backup-gates",
                payload={
                    "product": "verireel",
                    "backup_gate": {
                        "record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                        "context": "verireel",
                        "instance": "prod",
                        "created_at": "2026-04-21T18:05:00Z",
                        "source": "verireel-prod-gate",
                        "status": "pass",
                        "evidence": {
                            "snapshot_name": "ver-predeploy-20260421T180500Z",
                            "manifest_path": "scratch/prod-gates/ver-predeploy-20260421T180500Z.json",
                        },
                    },
                },
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "backup_gate_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                },
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            backup_gate = store.read_backup_gate_record(
                "backup-gate-verireel-prod-run-12345-attempt-1"
            )
            self.assertEqual(
                backup_gate,
                BackupGateRecord(
                    record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    context="verireel",
                    instance="prod",
                    created_at="2026-04-21T18:05:00Z",
                    source="verireel-prod-gate",
                    status="pass",
                    evidence={
                        "snapshot_name": "ver-predeploy-20260421T180500Z",
                        "manifest_path": "scratch/prod-gates/ver-predeploy-20260421T180500Z.json",
                    },
                ),
            )

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
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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

    def test_preview_destroyed_endpoint_writes_records_for_authorized_janitor_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-verireel-testing-verireel-pr-72",
                    context="verireel-testing",
                    anchor_repo="verireel",
                    anchor_pr_number=72,
                    anchor_pr_url="https://github.com/every/verireel/pull/72",
                    preview_label="verireel-testing/verireel/pr-72",
                    state="active",
                    canonical_url="https://pr-72.ver-preview.shinycomputers.com",
                    created_at="2026-04-24T12:59:00Z",
                    updated_at="2026-04-24T12:59:00Z",
                    eligible_at="2026-04-24T12:59:00Z",
                    active_generation_id="preview-verireel-testing-verireel-pr-72-generation-0001",
                    serving_generation_id="preview-verireel-testing-verireel-pr-72-generation-0001",
                    latest_generation_id="preview-verireel-testing-verireel-pr-72-generation-0001",
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["schedule", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_destroyed.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="schedule",
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
                        "anchor_pr_number": 72,
                        "destroyed_at": "2026-04-24T13:01:00Z",
                        "destroy_reason": "external_preview_janitor_cleanup_completed",
                    },
                },
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "preview_id": "preview-verireel-testing-verireel-pr-72",
                    "transition": "destroyed",
                },
            )
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-72")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroyed_at, "2026-04-24T13:01:00Z")
            self.assertEqual(preview.destroy_reason, "external_preview_janitor_cleanup_completed")

    def test_verireel_testing_deploy_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
                "control_plane.service.execute_verireel_stable_deploy",
                return_value=VeriReelStableDeployResult(
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

    def test_verireel_testing_verification_driver_updates_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-verireel-testing-run-12345-attempt-1",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890"
                    ),
                    context="verireel",
                    instance="testing",
                    source_git_ref="abcdef1234567890",
                    resolved_target=ResolvedTargetEvidence(
                        target_type="application",
                        target_id="testing-app-123",
                        target_name="ver-testing-app",
                    ),
                    deploy=DeploymentEvidence(
                        target_name="ver-testing-app",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="testing-app-123",
                        status="pass",
                        started_at="2026-04-20T18:20:00Z",
                        finished_at="2026-04-20T18:21:15Z",
                    ),
                    destination_health={
                        "verified": True,
                        "urls": ["https://ver-testing.shinycomputers.com/api/health"],
                        "timeout_seconds": 45,
                        "status": "pass",
                    },
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/testing-verification",
                payload={
                    "product": "verireel",
                    "verification": {
                        "deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1",
                        "migration_status": "success",
                        "verification_status": "success",
                        "owner_routes_status": "success",
                    },
                },
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1",
                    "inventory_record_id": "verireel-testing",
                },
            )
            deployment = store.read_deployment_record(
                "deployment-verireel-testing-run-12345-attempt-1"
            )
            inventory = store.read_environment_inventory(
                context_name="verireel",
                instance_name="testing",
            )
            self.assertEqual(deployment.post_deploy_update.status, "pass")
            self.assertEqual(deployment.destination_health.status, "pass")
            self.assertEqual(inventory.deployment_record_id, deployment.record_id)

    def test_verireel_testing_verification_driver_marks_product_check_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-verireel-testing-run-12345-attempt-1",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890"
                    ),
                    context="verireel",
                    instance="testing",
                    source_git_ref="abcdef1234567890",
                    deploy=DeploymentEvidence(
                        target_name="ver-testing-app",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="testing-app-123",
                        status="pass",
                    ),
                    destination_health={
                        "verified": True,
                        "urls": ["https://ver-testing.shinycomputers.com/api/health"],
                        "timeout_seconds": 45,
                        "status": "pass",
                    },
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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

            status_code, _payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/testing-verification",
                payload={
                    "product": "verireel",
                    "verification": {
                        "deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1",
                        "migration_status": "success",
                        "verification_status": "failure",
                        "owner_routes_status": "success",
                    },
                },
            )

            self.assertEqual(status_code, 202)
            deployment = store.read_deployment_record(
                "deployment-verireel-testing-run-12345-attempt-1"
            )
            self.assertEqual(deployment.post_deploy_update.status, "pass")
            self.assertEqual(deployment.destination_health.status, "fail")

    def test_verireel_testing_deploy_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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

    def test_verireel_stable_environment_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
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
                            "actions": ["verireel_stable_environment.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
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

            with patch(
                "control_plane.service.resolve_verireel_stable_environment",
                return_value=VeriReelStableEnvironmentResult(
                    context="verireel",
                    instance="testing",
                    target_name="ver-testing-app",
                    target_type="application",
                    target_id="testing-app-123",
                    base_urls=("https://ver-testing.shinycomputers.com",),
                    primary_base_url="https://ver-testing.shinycomputers.com",
                    healthcheck_path="/api/health",
                    health_urls=("https://ver-testing.shinycomputers.com/api/health",),
                ),
            ) as resolve_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/stable-environment",
                    payload={
                        "product": "verireel",
                        "environment": {"context": "verireel", "instance": "testing"},
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["target_name"], "ver-testing-app")
            self.assertEqual(
                payload["result"]["primary_base_url"], "https://ver-testing.shinycomputers.com"
            )
            resolve_mock.assert_called_once()

    def test_verireel_runtime_verification_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
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
                            "actions": ["verireel_stable_environment.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
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

            with patch(
                "control_plane.service.execute_verireel_rollout_verification",
                return_value=VeriReelRolloutVerificationResult(
                    status="pass",
                    base_url="https://ver-testing.shinycomputers.com",
                    health_urls=("https://ver-testing.shinycomputers.com/api/health",),
                    started_at="2026-05-01T12:00:00Z",
                    finished_at="2026-05-01T12:00:05Z",
                ),
            ) as verify_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/runtime-verification",
                    payload={
                        "product": "verireel",
                        "verification": {"context": "verireel", "instance": "testing"},
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["status"], "pass")
            self.assertEqual(
                payload["result"]["base_url"], "https://ver-testing.shinycomputers.com"
            )
            verify_mock.assert_called_once()

    def test_verireel_app_maintenance_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
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
                            "actions": ["verireel_app_maintenance.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
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

            with patch(
                "control_plane.service.execute_verireel_app_maintenance",
                return_value=VeriReelAppMaintenanceResult(
                    maintenance_status="pass",
                    action="migrate",
                    context="verireel",
                    instance="testing",
                    application_name="ver-testing-app",
                    application_id="testing-app-123",
                    schedule_name="ver-apply-prisma-migrations",
                    started_at="2026-04-25T19:00:00Z",
                    finished_at="2026-04-25T19:01:00Z",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/app-maintenance",
                    payload={
                        "product": "verireel",
                        "maintenance": {
                            "context": "verireel",
                            "instance": "testing",
                            "action": "migrate",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["maintenance_status"], "pass")
            self.assertEqual(payload["result"]["application_id"], "testing-app-123")
            execute_mock.assert_called_once()

    def test_verireel_app_maintenance_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
                path="/v1/drivers/verireel/app-maintenance",
                payload={
                    "product": "verireel",
                    "maintenance": {
                        "context": "verireel",
                        "instance": "testing",
                        "action": "migrate",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_preview_inventory_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_inventory",
                return_value=VeriReelPreviewInventoryResult(
                    context="verireel-testing",
                    previews=(
                        VeriReelPreviewInventoryItem(
                            applicationId="app-42",
                            applicationName="ver-preview-pr-42-app",
                            previewSlug="pr-42",
                        ),
                    ),
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-inventory",
                    payload={
                        "product": "verireel",
                        "inventory": {"context": "verireel-testing"},
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["previews"][0]["previewSlug"], "pr-42")
            execute_mock.assert_called_once()
            scan_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_inventory_scan_records(context_name="verireel-testing")

            self.assertEqual(len(scan_records), 1)
            self.assertEqual(scan_records[0].preview_count, 1)
            self.assertEqual(scan_records[0].preview_slugs, ("pr-42",))

    def test_verireel_preview_inventory_driver_does_not_replay_cached_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "product": "verireel",
                "inventory": {"context": "verireel-testing"},
            }

            with patch(
                "control_plane.service.execute_verireel_preview_inventory",
                side_effect=[
                    VeriReelPreviewInventoryResult(
                        context="verireel-testing",
                        previews=(
                            VeriReelPreviewInventoryItem(
                                applicationId="app-93",
                                applicationName="ver-preview-pr-93-app",
                                previewSlug="pr-93",
                            ),
                        ),
                    ),
                    VeriReelPreviewInventoryResult(context="verireel-testing", previews=()),
                ],
            ) as execute_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-inventory",
                    payload=request_payload,
                    headers={"Idempotency-Key": "verireel-preview-inventory:verireel-testing"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-inventory",
                    payload=request_payload,
                    headers={"Idempotency-Key": "verireel-preview-inventory:verireel-testing"},
                )

            self.assertEqual(first_status_code, 202)
            self.assertEqual(second_status_code, 202)
            self.assertEqual(first_payload["result"]["previews"][0]["previewSlug"], "pr-93")
            self.assertEqual(second_payload["result"]["previews"], [])
            self.assertEqual(execute_mock.call_count, 2)

    def test_preview_lifecycle_plan_endpoint_records_report_only_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260429T192315Z",
                    context="verireel-testing",
                    scanned_at="2026-04-29T19:23:15Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=2,
                    preview_slugs=("pr-41", "pr-42"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
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
                path="/v1/previews/lifecycle-plan",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "source": "preview-janitor",
                    "desired_state_id": "preview-desired-state-verireel-testing-20260429T192314Z",
                    "desired_previews": [
                        {
                            "preview_slug": "pr-42",
                            "anchor_repo": "verireel",
                            "anchor_pr_number": 42,
                        },
                        {
                            "preview_slug": "pr-43",
                            "anchor_repo": "verireel",
                            "anchor_pr_number": 43,
                        },
                    ],
                },
            )

            plan_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_lifecycle_plan_records(context_name="verireel-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["preview_lifecycle_plan_id"], plan_records[0].plan_id)
        self.assertEqual(
            payload["result"]["inventory_scan_id"],
            "preview-inventory-scan-verireel-testing-20260429T192315Z",
        )
        self.assertEqual(payload["result"]["keep_slugs"], ["pr-42"])
        self.assertEqual(
            payload["result"]["desired_state_id"],
            "preview-desired-state-verireel-testing-20260429T192314Z",
        )
        self.assertEqual(payload["result"]["orphaned_slugs"], ["pr-41"])
        self.assertEqual(payload["result"]["missing_slugs"], ["pr-43"])
        self.assertEqual(len(plan_records), 1)
        self.assertEqual(plan_records[0].source, "preview-janitor")

    def test_preview_lifecycle_plan_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
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
                path="/v1/previews/lifecycle-plan",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "desired_previews": [{"preview_slug": "pr-42"}],
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_preview_lifecycle_plan_endpoint_records_missing_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
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
                path="/v1/previews/lifecycle-plan",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "desired_previews": [{"preview_slug": "pr-42"}],
                },
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["status"], "missing_inventory")
        self.assertEqual(payload["result"]["orphaned_slugs"], [])
        self.assertIn("has not recorded", payload["result"]["error_message"])

    def test_preview_desired_state_endpoint_discovers_and_records_labeled_prs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/launchplane",
                            "workflow_refs": [
                                "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_desired_state.discover"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/launchplane",
                        workflow_ref=(
                            "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.discover_github_preview_desired_state",
                return_value=PreviewDesiredStateRecord(
                    desired_state_id="preview-desired-state-verireel-testing-20260429T213000Z",
                    product="verireel",
                    context="verireel-testing",
                    source="launchplane-preview-lifecycle",
                    discovered_at="2026-04-29T21:30:00Z",
                    repository="every/verireel",
                    label="preview",
                    anchor_repo="verireel",
                    status="pass",
                    desired_count=1,
                    desired_previews=(
                        PreviewLifecycleDesiredPreview(
                            preview_slug="pr-42",
                            anchor_repo="verireel",
                            anchor_pr_number=42,
                            anchor_pr_url="https://github.com/every/verireel/pull/42",
                            head_sha="abc1234",
                        ),
                    ),
                ),
            ) as discover_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/previews/desired-state",
                    payload={
                        "product": "verireel",
                        "context": "verireel-testing",
                        "source": "launchplane-preview-lifecycle",
                        "repository": "every/verireel",
                        "label": "preview",
                        "anchor_repo": "verireel",
                    },
                )

            records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_desired_state_records(context_name="verireel-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["preview_desired_state_id"],
            "preview-desired-state-verireel-testing-20260429T213000Z",
        )
        self.assertEqual(payload["result"]["desired_previews"][0]["preview_slug"], "pr-42")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].desired_count, 1)
        discover_mock.assert_called_once()

    def test_preview_desired_state_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/launchplane",
                            "workflow_refs": [
                                "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/launchplane",
                        workflow_ref=(
                            "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
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
                path="/v1/previews/desired-state",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "repository": "every/verireel",
                    "anchor_repo": "verireel",
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_preview_pr_feedback_endpoint_records_skipped_delivery_without_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/pull/42/merge"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_pr_feedback.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-control-plane.yml@refs/pull/42/merge"
                        ),
                        event_name="pull_request",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "source": "preview-control-plane",
                    "repository": "every/verireel",
                    "anchor_repo": "verireel",
                    "anchor_pr_number": 42,
                    "anchor_pr_url": "https://github.com/every/verireel/pull/42",
                    "status": "ready",
                    "preview_url": "https://pr-42.preview.example",
                    "immutable_image_reference": "ghcr.io/every/verireel:pr-42-a1b2c3d4",
                    "refresh_image_reference": "ghcr.io/every/verireel:preview-pr-42",
                    "revision": "a1b2c3d4",
                    "run_url": "https://github.com/every/verireel/actions/runs/123",
                },
            )

            feedback_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_pr_feedback_records(context_name="verireel-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["preview_pr_feedback_id"], feedback_records[0].feedback_id
        )
        self.assertEqual(payload["result"]["delivery_status"], "skipped")
        self.assertIn("Launchplane preview is ready", payload["result"]["comment_markdown"])
        self.assertIn("GITHUB_TOKEN", feedback_records[0].error_message)
        self.assertEqual(feedback_records[0].anchor_pr_number, 42)

    def test_preview_pr_feedback_endpoint_records_unsupported_notice(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-fork-notice.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request_target"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_pr_feedback.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-fork-notice.yml@refs/heads/main"
                        ),
                        event_name="pull_request_target",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "source": "preview-fork-notice",
                    "repository": "every/verireel",
                    "anchor_repo": "verireel",
                    "anchor_pr_number": 43,
                    "anchor_pr_url": "https://github.com/every/verireel/pull/43",
                    "status": "unsupported",
                    "run_url": "https://github.com/every/verireel/actions/runs/124",
                    "failure_summary": "Preview automation is unavailable for forked pull requests.",
                },
            )

            feedback_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_pr_feedback_records(context_name="verireel-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(feedback_records[0].status, "unsupported")
        self.assertIn("preview automation is unavailable", payload["result"]["comment_markdown"])
        self.assertIn("protected preview provisioning path", payload["result"]["comment_markdown"])
        self.assertEqual(payload["result"]["delivery_status"], "skipped")

    def test_preview_pr_feedback_endpoint_accepts_generic_refresh_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/pull/19/merge"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/pull/19/merge"
                        ),
                        event_name="pull_request",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload={
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "source": "preview-control-plane",
                    "repository": "cbusillo/sellyouroutboard",
                    "anchor_repo": "sellyouroutboard",
                    "anchor_pr_number": 19,
                    "anchor_pr_url": "https://github.com/cbusillo/sellyouroutboard/pull/19",
                    "status": "ready",
                    "preview_url": "https://pr-19.syo-preview.shinycomputers.com",
                },
            )

            feedback_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_pr_feedback_records(context_name="sellyouroutboard-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(feedback_records[0].status, "ready")

    def test_preview_pr_feedback_endpoint_accepts_pending_refresh_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-fork-notice.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request_target"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-fork-notice.yml"
                            "@refs/heads/main"
                        ),
                        event_name="pull_request_target",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload={
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "source": "preview-fork-notice",
                    "repository": "cbusillo/sellyouroutboard",
                    "anchor_repo": "sellyouroutboard",
                    "anchor_pr_number": 19,
                    "anchor_pr_url": "https://github.com/cbusillo/sellyouroutboard/pull/19",
                    "status": "pending",
                    "run_url": "https://github.com/cbusillo/sellyouroutboard/actions/runs/123",
                },
            )

            feedback_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_pr_feedback_records(context_name="sellyouroutboard-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(feedback_records[0].status, "pending")
        self.assertIn("preview is waiting", payload["result"]["comment_markdown"])
        self.assertIn("prerequisites are still in flight", payload["result"]["comment_markdown"])

    def test_preview_pr_feedback_endpoint_accepts_generic_destroy_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-cleanup.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_destroy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-cleanup.yml"
                            "@refs/heads/main"
                        ),
                        event_name="pull_request",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload={
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "source": "preview-cleanup",
                    "repository": "cbusillo/sellyouroutboard",
                    "anchor_repo": "sellyouroutboard",
                    "anchor_pr_number": 19,
                    "anchor_pr_url": "https://github.com/cbusillo/sellyouroutboard/pull/19",
                    "status": "destroyed",
                },
            )

            feedback_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_pr_feedback_records(context_name="sellyouroutboard-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(feedback_records[0].status, "destroyed")

    def test_preview_pr_feedback_endpoint_keeps_unsupported_explicit(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/pull/19/merge"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/pull/19/merge"
                        ),
                        event_name="pull_request",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload={
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "source": "preview-control-plane",
                    "repository": "cbusillo/sellyouroutboard",
                    "anchor_repo": "sellyouroutboard",
                    "anchor_pr_number": 19,
                    "anchor_pr_url": "https://github.com/cbusillo/sellyouroutboard/pull/19",
                    "status": "unsupported",
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_preview_pr_feedback_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/pull/42/merge"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-control-plane.yml@refs/pull/42/merge"
                        ),
                        event_name="pull_request",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "repository": "every/verireel",
                    "anchor_repo": "verireel",
                    "anchor_pr_number": 42,
                    "anchor_pr_url": "https://github.com/every/verireel/pull/42",
                    "status": "ready",
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_preview_lifecycle_cleanup_endpoint_records_report_only_cleanup(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_lifecycle_plan_record(
                PreviewLifecyclePlanRecord(
                    plan_id="preview-lifecycle-plan-verireel-testing-20260429T195838Z",
                    product="verireel",
                    context="verireel-testing",
                    planned_at="2026-04-29T19:58:38Z",
                    source="preview-janitor",
                    status="pass",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260429T195837Z",
                    actual_slugs=("pr-41",),
                    orphaned_slugs=("pr-41",),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.cleanup"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
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
                path="/v1/previews/lifecycle-cleanup",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "plan_id": "preview-lifecycle-plan-verireel-testing-20260429T195838Z",
                    "source": "preview-janitor",
                    "apply": False,
                },
            )

            cleanup_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_lifecycle_cleanup_records(context_name="verireel-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["preview_lifecycle_cleanup_id"], cleanup_records[0].cleanup_id
        )
        self.assertEqual(payload["result"]["status"], "report_only")
        self.assertEqual(payload["result"]["planned_slugs"], ["pr-41"])
        self.assertEqual(cleanup_records[0].apply, False)
        self.assertEqual(
            cleanup_records[0].plan_id, "preview-lifecycle-plan-verireel-testing-20260429T195838Z"
        )

    def test_preview_lifecycle_cleanup_endpoint_executes_and_records_destroyed_preview(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-verireel-testing-verireel-pr-41",
                    context="verireel-testing",
                    anchor_repo="verireel",
                    anchor_pr_number=41,
                    anchor_pr_url="https://github.example/every/verireel/pull/41",
                    preview_label="verireel-testing/verireel#41",
                    canonical_url="https://pr-41.preview.example",
                    state="active",
                    created_at="2026-04-20T10:00:00Z",
                    updated_at="2026-04-20T10:00:00Z",
                    eligible_at="2026-04-20T10:00:00Z",
                )
            )
            store.write_preview_lifecycle_plan_record(
                PreviewLifecyclePlanRecord(
                    plan_id="preview-lifecycle-plan-verireel-testing-20260429T195838Z",
                    product="verireel",
                    context="verireel-testing",
                    planned_at="2026-04-29T19:58:38Z",
                    source="preview-janitor",
                    status="pass",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260429T195837Z",
                    actual_slugs=("pr-41",),
                    orphaned_slugs=("pr-41",),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.cleanup"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.workflows.preview_lifecycle_cleanup.execute_verireel_preview_destroy",
                return_value=VeriReelPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-04-29T20:00:00Z",
                    destroy_finished_at="2026-04-29T20:00:05Z",
                    application_name="ver-preview-pr-41-app",
                    application_id="app-41",
                    preview_url="https://pr-41.preview.example",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/previews/lifecycle-cleanup",
                    payload={
                        "product": "verireel",
                        "context": "verireel-testing",
                        "plan_id": "preview-lifecycle-plan-verireel-testing-20260429T195838Z",
                        "source": "preview-janitor",
                        "apply": True,
                        "destroy_reason": "external_preview_janitor_cleanup_completed",
                    },
                )

            updated_preview = FilesystemRecordStore(state_dir=root / "state").read_preview_record(
                "preview-verireel-testing-verireel-pr-41"
            )
            cleanup_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_lifecycle_cleanup_records(context_name="verireel-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["status"], "pass")
        self.assertEqual(payload["result"]["destroyed_slugs"], ["pr-41"])
        self.assertEqual(execute_mock.call_count, 1)
        self.assertEqual(updated_preview.state, "destroyed")
        self.assertEqual(updated_preview.destroyed_at, "2026-04-29T20:00:05Z")
        self.assertEqual(cleanup_records[0].status, "pass")

    def test_preview_lifecycle_cleanup_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
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
                path="/v1/previews/lifecycle-cleanup",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "plan_id": "preview-lifecycle-plan-verireel-testing-20260429T195838Z",
                    "apply": False,
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_prod_deploy_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_prod_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_stable_deploy",
                return_value=VeriReelStableDeployResult(
                    deployment_record_id="deployment-verireel-prod-run-12345-attempt-1",
                    deploy_status="pass",
                    deploy_started_at="2026-04-20T19:20:00Z",
                    deploy_finished_at="2026-04-20T19:21:15Z",
                    target_name="ver-prod-app",
                    target_type="application",
                    target_id="prod-app-123",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-deploy",
                    payload={
                        "product": "verireel",
                        "deploy": {
                            "instance": "prod",
                            "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                            "source_git_ref": "abcdef1234567890",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {"deployment_record_id": "deployment-verireel-prod-run-12345-attempt-1"},
            )
            self.assertEqual(payload["result"]["deploy_status"], "pass")
            self.assertEqual(payload["result"]["target_id"], "prod-app-123")
            execute_mock.assert_called_once()

    def test_verireel_prod_deploy_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["promotion.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
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
                path="/v1/drivers/verireel/prod-deploy",
                payload={
                    "product": "verireel",
                    "deploy": {
                        "instance": "prod",
                        "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                        "source_git_ref": "abcdef1234567890",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_prod_promotion_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_prod_promotion",
                return_value=VeriReelProdPromotionResult(
                    promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    deployment_record_id="deployment-verireel-prod-run-12345-attempt-1",
                    backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    deploy_status="pass",
                    deploy_started_at="2026-04-21T18:20:00Z",
                    deploy_finished_at="2026-04-21T18:21:15Z",
                    target_name="ver-prod-app",
                    target_type="application",
                    target_id="prod-app-123",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-promotion",
                    payload={
                        "product": "verireel",
                        "promotion": {
                            "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                            "source_git_ref": "abcdef1234567890",
                            "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                            "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                            "source_health_status": "success",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    "deployment_record_id": "deployment-verireel-prod-run-12345-attempt-1",
                },
            )
            self.assertEqual(payload["result"]["deploy_status"], "pass")
            self.assertEqual(
                payload["result"]["promotion_record_id"],
                "promotion-verireel-testing-to-prod-run-12345-attempt-1",
            )
            execute_mock.assert_called_once()
            request = execute_mock.call_args.kwargs["request"]
            self.assertEqual(request.source_health_status, "pass")

    def test_odoo_post_deploy_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["odoo_post_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="opw",
                    instance="testing",
                    phase="deploy",
                    post_deploy_status="pass",
                    override_status="pass",
                    override_record_found=True,
                    override_payload_rendered=True,
                    applied_at="2026-04-26T12:05:00Z",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/post-deploy",
                    payload={
                        "product": "odoo",
                        "post_deploy": {
                            "context": "opw",
                            "instance": "testing",
                            "phase": "deploy",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {"transition": "odoo-post-deploy:opw:testing:deploy"},
            )
            self.assertEqual(payload["result"]["post_deploy_status"], "pass")
            self.assertEqual(payload["result"]["override_status"], "pass")
            execute_mock.assert_called_once()

    def test_odoo_post_deploy_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/tenant-opw",
                                "workflow_refs": [
                                    "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo"],
                                "contexts": ["opw"],
                                "actions": ["deployment.write"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/post-deploy",
                payload={
                    "product": "odoo",
                    "post_deploy": {
                        "context": "opw",
                        "instance": "testing",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_odoo_artifact_publish_driver_writes_manifest_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["odoo_artifact_publish.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.ingest_odoo_artifact_publish_evidence",
                return_value=OdooArtifactPublishResult(
                    status="pass",
                    context="opw",
                    instance="testing",
                    artifact_id="artifact-opw-new",
                    image_repository="ghcr.io/cbusillo/odoo-tenant-opw",
                    image_digest="sha256:new",
                    source_commit="2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/artifact-publish",
                    payload={
                        "product": "odoo",
                        "publish": {
                            "context": "opw",
                            "instance": "testing",
                            "manifest": {
                                "artifact_id": "artifact-opw-new",
                                "source_commit": "2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                                "enterprise_base_digest": "sha256:enterprise",
                                "image": {
                                    "repository": "ghcr.io/cbusillo/odoo-tenant-opw",
                                    "digest": "sha256:new",
                                },
                            },
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["records"]["artifact_id"], "artifact-opw-new")
            self.assertEqual(payload["result"]["status"], "pass")
            execute_mock.assert_called_once()

    def test_odoo_artifact_publish_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/tenant-opw",
                                "workflow_refs": [
                                    "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo"],
                                "contexts": ["opw"],
                                "actions": ["odoo_post_deploy.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/artifact-publish",
                payload={
                    "product": "odoo",
                    "publish": {
                        "context": "opw",
                        "instance": "testing",
                        "manifest": {
                            "artifact_id": "artifact-opw-new",
                            "source_commit": "2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                            "enterprise_base_digest": "sha256:enterprise",
                            "image": {
                                "repository": "ghcr.io/cbusillo/odoo-tenant-opw",
                                "digest": "sha256:new",
                            },
                        },
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_odoo_artifact_publish_inputs_returns_build_scoped_environment(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["odoo_artifact_publish_inputs.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.build_odoo_artifact_publish_inputs",
                return_value={
                    "context": "opw",
                    "instance": "testing",
                    "environment": {"ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/runtime:19"},
                },
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/artifact-publish-inputs",
                    payload={
                        "product": "odoo",
                        "inputs": {"context": "opw", "instance": "testing"},
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["records"], {})
            self.assertEqual(
                payload["result"]["environment"],
                {"ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/runtime:19"},
            )

    def test_odoo_prod_backup_gate_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["cm"],
                            "actions": ["odoo_prod_backup_gate.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_odoo_prod_backup_gate",
                return_value=OdooProdBackupGateResult(
                    context="cm",
                    instance="prod",
                    backup_record_id="backup-gate-cm-prod-run-1",
                    backup_status="pass",
                    backup_root="/volumes/data/backups/launchplane",
                    database_dump_path="/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-run-1/cm.dump",
                    filestore_archive_path="/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-run-1/cm-filestore.tar.gz",
                    manifest_path="/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-run-1/manifest.json",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/prod-backup-gate",
                    payload={
                        "product": "odoo",
                        "backup_gate": {
                            "context": "cm",
                            "instance": "prod",
                            "backup_record_id": "backup-gate-cm-prod-run-1",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {"backup_record_id": "backup-gate-cm-prod-run-1"},
            )
            self.assertEqual(payload["result"]["backup_status"], "pass")
            self.assertEqual(
                payload["result"]["database_dump_path"],
                "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-run-1/cm.dump",
            )
            execute_mock.assert_called_once()

    def test_odoo_prod_backup_gate_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/tenant-cm",
                                "workflow_refs": [
                                    "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo"],
                                "contexts": ["cm"],
                                "actions": ["odoo_post_deploy.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/prod-backup-gate",
                payload={
                    "product": "odoo",
                    "backup_gate": {
                        "context": "cm",
                        "instance": "prod",
                        "backup_record_id": "backup-gate-cm-prod-run-1",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_odoo_prod_promotion_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["cm"],
                            "actions": ["odoo_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_odoo_prod_promotion",
                return_value=OdooProdPromotionResult(
                    context="cm",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="artifact-cm-new",
                    backup_record_id="backup-gate-cm-prod-run-1",
                    promotion_record_id="promotion-cm-testing-to-prod",
                    deployment_record_id="deployment-cm-prod",
                    release_tuple_id="cm-prod-artifact-cm-new",
                    promotion_status="pass",
                    deployment_status="pass",
                    post_deploy_status="pass",
                    destination_health_status="pass",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/prod-promotion",
                    payload={
                        "product": "odoo",
                        "promotion": {
                            "context": "cm",
                            "from_instance": "testing",
                            "to_instance": "prod",
                            "artifact_id": "artifact-cm-new",
                            "backup_record_id": "backup-gate-cm-prod-run-1",
                            "source_git_ref": "848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "promotion_record_id": "promotion-cm-testing-to-prod",
                    "deployment_record_id": "deployment-cm-prod",
                    "backup_record_id": "backup-gate-cm-prod-run-1",
                    "release_tuple_id": "cm-prod-artifact-cm-new",
                },
            )
            self.assertEqual(payload["result"]["promotion_status"], "pass")
            self.assertEqual(payload["result"]["destination_health_status"], "pass")
            execute_mock.assert_called_once()
            self.assertIn("database_url", execute_mock.call_args.kwargs)

    def test_odoo_prod_promotion_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/tenant-cm",
                                "workflow_refs": [
                                    "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo"],
                                "contexts": ["cm"],
                                "actions": ["odoo_prod_backup_gate.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/prod-promotion",
                payload={
                    "product": "odoo",
                    "promotion": {
                        "context": "cm",
                        "artifact_id": "artifact-cm-new",
                        "backup_record_id": "backup-gate-cm-prod-run-1",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_odoo_prod_rollback_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["odoo_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_odoo_prod_rollback",
                return_value=OdooProdRollbackResult(
                    context="opw",
                    instance="prod",
                    source_channel="testing",
                    artifact_id="artifact-opw-847c71c1db61785c",
                    promotion_record_id="promotion-opw-testing-to-prod",
                    deployment_record_id="deployment-opw-prod-rollback",
                    release_tuple_id="opw-prod-artifact-opw-847c71c1db61785c",
                    rollback_status="pass",
                    rollback_health_status="pass",
                    post_deploy_status="pass",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/prod-rollback",
                    payload={
                        "product": "odoo",
                        "rollback": {
                            "context": "opw",
                            "instance": "prod",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "promotion_record_id": "promotion-opw-testing-to-prod",
                    "deployment_record_id": "deployment-opw-prod-rollback",
                },
            )
            self.assertEqual(payload["result"]["rollback_status"], "pass")
            self.assertEqual(payload["result"]["rollback_health_status"], "pass")
            self.assertEqual(payload["result"]["post_deploy_status"], "pass")
            execute_mock.assert_called_once()

    def test_odoo_prod_rollback_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/tenant-opw",
                                "workflow_refs": [
                                    "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo"],
                                "contexts": ["opw"],
                                "actions": ["odoo_post_deploy.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/prod-rollback",
                payload={
                    "product": "odoo",
                    "rollback": {
                        "context": "opw",
                        "instance": "prod",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_odoo_prod_rollback_driver_replays_idempotent_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["odoo_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "product": "odoo",
                "rollback": {
                    "context": "opw",
                    "instance": "prod",
                },
            }

            with patch(
                "control_plane.service.execute_odoo_prod_rollback",
                return_value=OdooProdRollbackResult(
                    context="opw",
                    instance="prod",
                    source_channel="testing",
                    artifact_id="artifact-opw-847c71c1db61785c",
                    promotion_record_id="promotion-opw-testing-to-prod",
                    deployment_record_id="deployment-opw-prod-rollback",
                    release_tuple_id="opw-prod-artifact-opw-847c71c1db61785c",
                    rollback_status="pass",
                    rollback_health_status="pass",
                    post_deploy_status="pass",
                ),
            ) as execute_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/prod-rollback",
                    payload=request_payload,
                    headers={"Idempotency-Key": "rollback-opw-prod-once"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/prod-rollback",
                    payload=request_payload,
                    headers={"Idempotency-Key": "rollback-opw-prod-once"},
                )

            self.assertEqual(first_status_code, 202)
            self.assertEqual(second_status_code, 202)
            self.assertNotIn("replayed", first_payload)
            self.assertTrue(second_payload["replayed"])
            self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
            execute_mock.assert_called_once()

    def test_verireel_prod_promotion_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["verireel"],
                                "contexts": ["verireel"],
                                "actions": ["promotion.write"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/prod-promotion",
                payload={
                    "product": "verireel",
                    "promotion": {
                        "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                        "source_git_ref": "abcdef1234567890",
                        "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                        "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_prod_rollback_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_prod_rollback",
                return_value=VeriReelProdRollbackResult(
                    promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    snapshot_name="ver-predeploy-20260421-180000",
                    rollback_status="pass",
                    rollback_health_status="pass",
                    rollback_started_at="2026-04-21T18:20:00Z",
                    rollback_finished_at="2026-04-21T18:21:00Z",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-rollback",
                    payload={
                        "product": "verireel",
                        "rollback": {
                            "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                            "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                },
            )
            self.assertEqual(payload["result"]["rollback_status"], "pass")
            execute_mock.assert_called_once()

    def test_verireel_prod_rollback_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["verireel"],
                                "contexts": ["verireel"],
                                "actions": ["promotion.write"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/prod-rollback",
                payload={
                    "product": "verireel",
                    "rollback": {
                        "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                        "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_driver_unexpected_error_returns_json_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["verireel"],
                                "contexts": ["verireel"],
                                "actions": ["verireel_prod_rollback.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            with (
                patch(
                    "control_plane.service.execute_verireel_prod_rollback",
                    side_effect=RuntimeError("driver exploded"),
                ),
                patch("control_plane.service._LOGGER.exception"),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-rollback",
                    payload={
                        "product": "verireel",
                        "rollback": {
                            "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                            "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                        },
                    },
                )

            self.assertEqual(status_code, 500)
            self.assertEqual(payload["status"], "rejected")
            self.assertEqual(payload["error"]["code"], "internal_error")
            self.assertIn("trace_id", payload)

    def test_verireel_prod_backup_gate_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_prod_backup_gate.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_prod_backup_gate",
                return_value=VeriReelProdBackupGateResult(
                    backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    backup_status="pass",
                    backup_started_at="2026-04-25T00:15:00Z",
                    backup_finished_at="2026-04-25T00:16:00Z",
                    snapshot_name="ver-predeploy-20260425-001500",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-backup-gate",
                    payload={
                        "product": "verireel",
                        "backup_gate": {
                            "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1"
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "backup_gate_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                },
            )
            self.assertEqual(payload["result"]["backup_status"], "pass")
            execute_mock.assert_called_once()
            self.assertTrue(execute_mock.call_args.kwargs["run_async"])

    def test_verireel_prod_backup_gate_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["verireel"],
                                "contexts": ["verireel"],
                                "actions": ["promotion.write"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/prod-backup-gate",
                payload={
                    "product": "verireel",
                    "backup_gate": {
                        "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1"
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_preview_refresh_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
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
                            "actions": ["verireel_preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_refresh",
                return_value=VeriReelPreviewRefreshResult(
                    refresh_status="pass",
                    refresh_started_at="2026-04-21T01:30:00Z",
                    refresh_finished_at="2026-04-21T01:34:00Z",
                    application_name="ver-preview-pr-123-app",
                    application_id="preview-app-123",
                    preview_url="https://pr-123.ver-preview.shinycomputers.com",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-refresh",
                    payload={
                        "product": "verireel",
                        "refresh": {
                            "anchor_pr_number": 123,
                            "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                            "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                            "preview_slug": "pr-123",
                            "preview_url": "https://pr-123.ver-preview.shinycomputers.com",
                            "image_reference": "ghcr.io/every/verireel-app:pr-123-sha-6b3c9d7",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"]["preview_id"], "preview-verireel-testing-verireel-pr-123"
            )
            self.assertEqual(
                payload["records"]["generation_id"],
                "preview-verireel-testing-verireel-pr-123-generation-0001",
            )
            self.assertEqual(payload["records"]["transition"], "verifying")
            self.assertEqual(payload["result"]["refresh_status"], "pass")
            self.assertEqual(payload["result"]["application_id"], "preview-app-123")
            store = FilesystemRecordStore(state_dir=state_dir)
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            generation = store.read_preview_generation_record(
                "preview-verireel-testing-verireel-pr-123-generation-0001"
            )
            self.assertEqual(preview.state, "pending")
            self.assertEqual(preview.canonical_url, "https://pr-123.ver-preview.shinycomputers.com")
            self.assertEqual(generation.state, "verifying")
            self.assertEqual(generation.deploy_status, "pass")
            self.assertEqual(generation.verify_status, "pending")
            self.assertEqual(
                generation.resolved_manifest_fingerprint,
                "verireel-preview-manifest-pr-123-6b3c9d7",
            )
            execute_mock.assert_called_once()

    def test_verireel_preview_refresh_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-refresh",
                payload={
                    "product": "verireel",
                    "refresh": {
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                        "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                        "preview_slug": "pr-123",
                        "preview_url": "https://pr-123.ver-preview.shinycomputers.com",
                        "image_reference": "ghcr.io/every/verireel-app:pr-123-sha-6b3c9d7",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_preview_refresh_driver_writes_failed_generation_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
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
                            "actions": ["verireel_preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_refresh",
                return_value=VeriReelPreviewRefreshResult(
                    refresh_status="fail",
                    refresh_started_at="2026-04-21T01:30:00Z",
                    refresh_finished_at="2026-04-21T01:34:00Z",
                    application_name="ver-preview-pr-123-app",
                    application_id="preview-app-123",
                    preview_url="https://pr-123.ver-preview.shinycomputers.com",
                    error_message="Dokploy update failed.",
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-refresh",
                    payload={
                        "product": "verireel",
                        "refresh": {
                            "anchor_pr_number": 123,
                            "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                            "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                            "preview_slug": "pr-123",
                            "preview_url": "https://pr-123.ver-preview.shinycomputers.com",
                            "image_reference": "ghcr.io/every/verireel-app:pr-123-sha-6b3c9d7",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["records"]["transition"], "failed")
            store = FilesystemRecordStore(state_dir=state_dir)
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            generation = store.read_preview_generation_record(
                "preview-verireel-testing-verireel-pr-123-generation-0001"
            )
            self.assertEqual(preview.state, "failed")
            self.assertEqual(generation.state, "failed")
            self.assertEqual(generation.deploy_status, "fail")
            self.assertEqual(generation.verify_status, "skipped")
            self.assertEqual(generation.failure_stage, "provision")
            self.assertEqual(generation.failure_summary, "Dokploy update failed.")

    def test_verireel_preview_verification_driver_marks_latest_generation_ready(self) -> None:
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
                    preview_label="preview",
                    canonical_url="https://pr-123.ver-preview.shinycomputers.com",
                    state="pending",
                    created_at="2026-04-21T01:30:00Z",
                    updated_at="2026-04-21T01:34:00Z",
                    eligible_at="2026-04-21T01:30:00Z",
                    active_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    latest_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    latest_manifest_fingerprint="verireel-preview-manifest-pr-123-6b3c9d7",
                )
            )
            store.write_preview_generation_record(
                PreviewGenerationRecord(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    sequence=1,
                    state="verifying",
                    requested_reason="external_preview_refresh",
                    requested_at="2026-04-21T01:30:00Z",
                    started_at="2026-04-21T01:30:00Z",
                    resolved_manifest_fingerprint="verireel-preview-manifest-pr-123-6b3c9d7",
                    artifact_id="ghcr.io/every/verireel-app:pr-123-sha-6b3c9d7",
                    anchor_summary=PreviewPullRequestSummary(
                        repo="verireel",
                        pr_number=123,
                        head_sha="6b3c9d7e8f901234567890abcdef1234567890ab",
                        pr_url="https://github.com/every/verireel/pull/123",
                    ),
                    deploy_status="pass",
                    verify_status="pending",
                    overall_health_status="pending",
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-verification",
                payload={
                    "product": "verireel",
                    "verification": {
                        "anchor_pr_number": 123,
                        "verification_status": "pass",
                        "verified_at": "2026-04-21T01:38:00Z",
                    },
                },
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["records"]["transition"], "ready")
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            generation = store.read_preview_generation_record(
                "preview-verireel-testing-verireel-pr-123-generation-0001"
            )
            self.assertEqual(preview.state, "active")
            self.assertEqual(preview.serving_generation_id, generation.generation_id)
            self.assertEqual(generation.state, "ready")
            self.assertEqual(generation.verify_status, "pass")
            self.assertEqual(generation.overall_health_status, "pass")
            self.assertEqual(generation.ready_at, "2026-04-21T01:38:00Z")

    def test_verireel_preview_destroy_driver_executes_for_authorized_workflow(self) -> None:
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
                    preview_label="preview",
                    canonical_url="https://pr-123.ver-preview.shinycomputers.com",
                    state="active",
                    created_at="2026-04-21T01:30:00Z",
                    updated_at="2026-04-21T01:34:00Z",
                    eligible_at="2026-04-21T01:30:00Z",
                    active_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    serving_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    latest_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    latest_manifest_fingerprint="verireel-preview-manifest-pr-123-6b3c9d7",
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
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
                            "actions": ["verireel_preview_destroy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_destroy",
                return_value=VeriReelPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-04-21T01:35:00Z",
                    destroy_finished_at="2026-04-21T01:36:00Z",
                    application_name="ver-preview-pr-123-app",
                    application_id="preview-app-123",
                    preview_url="https://pr-123.ver-preview.shinycomputers.com",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-destroy",
                    payload={
                        "product": "verireel",
                        "destroy": {
                            "anchor_pr_number": 123,
                            "preview_slug": "pr-123",
                            "destroy_reason": "external_preview_pull_request_closed",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"]["preview_id"], "preview-verireel-testing-verireel-pr-123"
            )
            self.assertEqual(payload["records"]["transition"], "destroyed")
            self.assertEqual(payload["result"]["destroy_status"], "pass")
            self.assertEqual(payload["result"]["application_id"], "preview-app-123")
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroy_reason, "external_preview_pull_request_closed")
            self.assertEqual(preview.destroyed_at, "2026-04-21T01:36:00Z")
            execute_mock.assert_called_once()

    def test_verireel_preview_destroy_driver_executes_for_authorized_janitor_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["schedule", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": [
                                "verireel_preview_destroy.execute",
                                "preview_destroyed.write",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="schedule",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_destroy",
                return_value=VeriReelPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-04-24T13:00:00Z",
                    destroy_finished_at="2026-04-24T13:01:00Z",
                    application_name="ver-preview-pr-72-app",
                    application_id="preview-app-72",
                    preview_url="https://pr-72.ver-preview.shinycomputers.com",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-destroy",
                    payload={
                        "product": "verireel",
                        "destroy": {
                            "context": "verireel-testing",
                            "anchor_repo": "verireel",
                            "anchor_pr_number": 72,
                            "preview_slug": "pr-72",
                            "destroy_reason": "external_preview_janitor_cleanup_completed",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["destroy_status"], "pass")
            self.assertEqual(payload["result"]["application_name"], "ver-preview-pr-72-app")
            execute_mock.assert_called_once()

    def test_verireel_preview_destroy_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-destroy",
                payload={
                    "product": "verireel",
                    "destroy": {
                        "anchor_pr_number": 123,
                        "preview_slug": "pr-123",
                        "destroy_reason": "external_preview_pull_request_closed",
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
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
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
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
            self.assertEqual(
                payload["record"]["record_id"], "deployment-20260420T153000Z-opw-testing"
            )
            self.assertEqual(payload["record"]["resolved_target"]["target_id"], "compose-123")

    def test_preview_history_and_recent_operations_endpoints_return_operator_read_models(
        self,
    ) -> None:
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
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
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
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
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
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
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
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
            self.assertEqual(
                history_payload["preview"]["preview_id"], "preview-verireel-testing-verireel-pr-123"
            )
            self.assertEqual(len(history_payload["generations"]), 1)
            self.assertEqual(history_payload["generations"][0]["state"], "ready")

            self.assertEqual(operations_status_code, 200)
            self.assertEqual(operations_payload["context"], "verireel-testing")
            self.assertEqual(operations_payload["storage_backend"], "filesystem")
            self.assertEqual(len(operations_payload["inventory"]), 1)
            self.assertEqual(len(operations_payload["recent_deployments"]), 1)
            self.assertEqual(len(operations_payload["recent_promotions"]), 1)
            self.assertEqual(len(operations_payload["recent_previews"]), 1)

    def test_secret_status_endpoints_return_operator_read_models(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="global",
                    integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
                    name="token",
                    plaintext_value="dokploy-token",
                    binding_key="DOKPLOY_TOKEN",
                    actor="test",
                )
                context_secret = control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context",
                    integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                    name="GITHUB_WEBHOOK_SECRET",
                    plaintext_value="webhook-secret",
                    binding_key="GITHUB_WEBHOOK_SECRET",
                    context_name="opw",
                    actor="test",
                )
            store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "contexts": ["opw"],
                            "actions": ["secret.read", "secret.list"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with patch.dict(
                os.environ,
                {
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                },
                clear=True,
            ):
                list_status_code, list_payload = _invoke_app(
                    app,
                    method="GET",
                    path="/v1/contexts/opw/secrets",
                )
                show_status_code, show_payload = _invoke_app(
                    app,
                    method="GET",
                    path=f"/v1/secrets/{context_secret['secret_id']}",
                )

            self.assertEqual(list_status_code, 200)
            self.assertEqual(list_payload["context"], "opw")
            self.assertEqual(len(list_payload["secrets"]), 2)
            self.assertEqual(show_status_code, 200)
            self.assertEqual(show_payload["secret"]["secret_id"], context_secret["secret_id"])
            self.assertEqual(
                show_payload["secret"]["binding"]["binding_key"], "GITHUB_WEBHOOK_SECRET"
            )

    def test_preview_generation_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
            policy = LaunchplaneAuthzPolicy.model_validate(
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
            app = create_launchplane_service_app(
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
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(github_actions=()),
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
