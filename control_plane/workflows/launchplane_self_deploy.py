from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.service_auth import parse_authz_policy_toml
from control_plane.workflows.ingress_provider import (
    NPMPLUS_BASE_URL_ENV_KEY,
    NPMPLUS_IDENTITY_ENV_KEY,
    NPMPLUS_SECRET_ENV_KEY,
)

LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY = "DOCKER_IMAGE_REFERENCE"
LAUNCHPLANE_SELF_DEPLOY_OAUTH_ENV_KEYS = frozenset(
    {
        "LAUNCHPLANE_GITHUB_CLIENT_ID",
        "LAUNCHPLANE_GITHUB_CLIENT_SECRET",
        "LAUNCHPLANE_PUBLIC_URL",
        "LAUNCHPLANE_SESSION_SECRET",
        "LAUNCHPLANE_COOKIE_SECURE",
        "LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS",
        "LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK",
        "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET",
        "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN",
        "LAUNCHPLANE_EVERY_CODE_GITHUB_ACTOR",
        "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
        "LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN",
        "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT",
        "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL",
        "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
        "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT",
        "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL",
        "LAUNCHPLANE_LOCAL_ADMIN_TOKEN",
        "LAUNCHPLANE_LOCAL_ADMIN_SUBJECT",
        "LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL",
        "LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER",
        "LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER",
        "LAUNCHPLANE_WORK_GRAPH_PROJECT_LIMIT",
        "LAUNCHPLANE_WORK_GRAPH_PROJECT_SIGNAL_LIMIT",
        "LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_REPOSITORIES",
        "LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_LIMIT",
        "LAUNCHPLANE_WORK_GRAPH_GH_BINARY",
        "LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN",
        "GH_TOKEN",
        NPMPLUS_BASE_URL_ENV_KEY,
        NPMPLUS_IDENTITY_ENV_KEY,
        NPMPLUS_SECRET_ENV_KEY,
    }
)


class LaunchplaneSelfDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["compose", "application"]
    target_id: str
    image_reference: str
    policy_b64: str = ""
    oauth_env: dict[str, str] = Field(default_factory=dict)
    oauth_env_removals: tuple[str, ...] = ()
    no_cache: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        updated = dict(data)
        raw_target_type = updated.get("target_type")
        if isinstance(raw_target_type, str):
            updated["target_type"] = raw_target_type.strip().lower()
        return updated

    @model_validator(mode="after")
    def _validate_values(self) -> "LaunchplaneSelfDeployRequest":
        if not self.target_id.strip():
            raise ValueError("Launchplane self deploy requires target_id.")
        if not self.image_reference.strip():
            raise ValueError("Launchplane self deploy requires image_reference.")
        normalized_policy_b64 = self.policy_b64.strip()
        if normalized_policy_b64:
            try:
                policy_text = base64.b64decode(normalized_policy_b64, validate=True).decode("utf-8")
            except Exception as error:
                raise ValueError(
                    "Launchplane self deploy requires valid base64 policy_b64."
                ) from error
            parse_authz_policy_toml(policy_text)
        self.target_id = self.target_id.strip()
        self.image_reference = self.image_reference.strip()
        self.policy_b64 = normalized_policy_b64
        normalized_oauth_env: dict[str, str] = {}
        for env_key, raw_value in self.oauth_env.items():
            normalized_key = env_key.strip()
            if normalized_key not in LAUNCHPLANE_SELF_DEPLOY_OAUTH_ENV_KEYS:
                raise ValueError(
                    f"Launchplane self deploy does not accept oauth_env key {normalized_key!r}."
                )
            normalized_value = raw_value.strip()
            if "\n" in normalized_value or "\r" in normalized_value:
                raise ValueError(
                    f"Launchplane self deploy oauth_env key {normalized_key!r} must be a single line."
                )
            if normalized_value:
                normalized_oauth_env[normalized_key] = normalized_value
        self.oauth_env = normalized_oauth_env
        normalized_oauth_env_removals: list[str] = []
        for env_key in self.oauth_env_removals:
            normalized_key = env_key.strip()
            if normalized_key not in LAUNCHPLANE_SELF_DEPLOY_OAUTH_ENV_KEYS:
                raise ValueError(
                    f"Launchplane self deploy does not accept oauth_env_removals key {normalized_key!r}."
                )
            if normalized_key in normalized_oauth_env:
                raise ValueError(
                    f"Launchplane self deploy cannot update and remove oauth env key {normalized_key!r}."
                )
            if normalized_key not in normalized_oauth_env_removals:
                normalized_oauth_env_removals.append(normalized_key)
        self.oauth_env_removals = tuple(normalized_oauth_env_removals)
        return self


class LaunchplaneSelfDeployResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["compose", "application"]
    target_id: str
    image_reference: str
    image_reference_changed: bool
    authz_policy_changed: bool
    authz_policy_sha256: str = ""
    oauth_env_keys_changed: tuple[str, ...] = ()
    oauth_env_keys_removed: tuple[str, ...] = ()


def execute_launchplane_self_deploy(
    *,
    control_plane_root_path: Path,
    request: LaunchplaneSelfDeployRequest,
) -> LaunchplaneSelfDeployResult:
    host, token = control_plane_dokploy.read_dokploy_config(
        control_plane_root=control_plane_root_path
    )
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=request.target_type,
        target_id=request.target_id,
    )
    raw_env_text = str(target_payload.get("env") or "")
    previous_env_map = control_plane_dokploy.parse_dokploy_env_text(raw_env_text)
    updates = {LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY: request.image_reference}
    updates.update(request.oauth_env)
    removals: tuple[str, ...] = request.oauth_env_removals
    if request.policy_b64:
        updates["LAUNCHPLANE_POLICY_B64"] = request.policy_b64
        removals = (
            *removals,
            "LAUNCHPLANE_POLICY_TOML",
            "LAUNCHPLANE_POLICY_FILE",
        )
    updated_env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
        raw_env_text,
        updates=updates,
        removals=removals,
    )
    if updated_env_text != raw_env_text:
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type=request.target_type,
            target_id=request.target_id,
            target_payload=target_payload,
            env_text=updated_env_text,
        )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=request.target_type,
        target_id=request.target_id,
        no_cache=request.no_cache,
    )
    return LaunchplaneSelfDeployResult(
        target_type=request.target_type,
        target_id=request.target_id,
        image_reference=request.image_reference,
        image_reference_changed=previous_env_map.get(LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY, "")
        != request.image_reference,
        authz_policy_changed=bool(request.policy_b64)
        and previous_env_map.get("LAUNCHPLANE_POLICY_B64", "") != request.policy_b64,
        authz_policy_sha256=(
            hashlib.sha256(base64.b64decode(request.policy_b64, validate=True)).hexdigest()
            if request.policy_b64
            else ""
        ),
        oauth_env_keys_changed=tuple(
            sorted(
                env_key
                for env_key in request.oauth_env
                if previous_env_map.get(env_key, "") != request.oauth_env[env_key]
            )
        ),
        oauth_env_keys_removed=tuple(
            sorted(env_key for env_key in request.oauth_env_removals if env_key in previous_env_map)
        ),
    )
