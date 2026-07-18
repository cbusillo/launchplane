import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Protocol

import click
from pydantic import ValidationError

from control_plane import product_context_audit as control_plane_product_context_audit
from control_plane.cli_shared import (
    DATABASE_URL_ENV_KEYS as _DATABASE_URL_ENV_KEYS,
    direct_db_mutation_acknowledgement_option as _direct_db_mutation_acknowledgement_option,
    require_direct_db_mutation_acknowledgement as _require_direct_db_mutation_acknowledgement,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    MergeTrainPolicyRecordStatus,
    build_merge_train_policy_record_id,
    load_merge_train_policy,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeEnvironmentClass,
    RuntimeKeySafetyPolicyRecord,
    RuntimeKeySafetyPolicyStatus,
    RuntimeKeySafetyTarget,
    RuntimeSecretSafetyRule,
)
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety_from_store,
    latest_active_runtime_key_safety_policy,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.launchplane import LAUNCHPLANE_PREVIEW_ENABLE_LABEL
from control_plane.workflows.ship import utc_now_timestamp


@dataclass(frozen=True)
class PolicyProfileCliCallbacks:
    post_launchplane_service_json: Callable[..., dict[str, object]]


_callbacks: PolicyProfileCliCallbacks | None = None


def register_policy_profile_commands(
    main: click.Group, *, callbacks: PolicyProfileCliCallbacks
) -> None:
    global _callbacks
    _callbacks = callbacks
    main.add_command(authz_policies)
    main.add_command(runtime_key_safety)
    main.add_command(merge_train_policies)
    main.add_command(product_profiles)


def _policy_profile_callbacks() -> PolicyProfileCliCallbacks:
    if _callbacks is None:
        raise click.ClickException("Launchplane policy/profile CLI callbacks are not configured.")
    return _callbacks


def _load_json_file(input_file: Path) -> dict[str, object]:
    payload = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise click.ClickException(f"{input_file} must contain a JSON object.")
    return payload


def _launchplane_action_slug(value: str) -> str:
    compact = "".join(
        character.lower() if character.isalnum() else "-" for character in value.strip()
    )
    normalized = "-".join(part for part in compact.split("-") if part)
    return normalized or "launchplane-preview"


def _post_launchplane_service_json(**kwargs: object) -> dict[str, object]:
    return _policy_profile_callbacks().post_launchplane_service_json(**kwargs)


class _PolicyRecordSummaryFields(Protocol):
    @property
    def record_id(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def updated_at(self) -> str: ...

    @property
    def policy_sha256(self) -> str: ...


def _policy_record_summary_base(record: _PolicyRecordSummaryFields) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "status": record.status,
        "source": record.source,
        "updated_at": record.updated_at,
        "policy_sha256": record.policy_sha256,
    }


def summarize_runtime_key_safety_policy_record(
    record: RuntimeKeySafetyPolicyRecord,
) -> dict[str, object]:
    return _policy_record_summary_base(record) | {
        "rule_count": len(record.rules),
        "binding_keys": [rule.binding_key for rule in record.rules],
    }


def summarize_merge_train_policy_record(record: MergeTrainPolicyRecord) -> dict[str, object]:
    return _policy_record_summary_base(record) | {
        "repository_count": len(record.policy.policies),
        "policy_keys": [
            repository_policy.policy_key for repository_policy in record.policy.policies
        ],
        "scheduler_policy_keys": [
            repository_policy.policy_key
            for repository_policy in record.policy.policies
            if repository_policy.scheduler.enabled
        ],
    }


def _github_repository_id_pair(
    *,
    repository_id: str,
    repository_owner_id: str,
    required: bool,
) -> tuple[str, str]:
    normalized_repository_id = repository_id.strip()
    normalized_repository_owner_id = repository_owner_id.strip()
    if required and not normalized_repository_id and not normalized_repository_owner_id:
        raise click.ClickException(
            "--repository-id and --repository-owner-id are required for new workflow grants."
        )
    if bool(normalized_repository_id) != bool(normalized_repository_owner_id):
        raise click.ClickException(
            "--repository-id and --repository-owner-id must be provided together."
        )
    if normalized_repository_id and (
        not normalized_repository_id.isdecimal() or not normalized_repository_owner_id.isdecimal()
    ):
        raise click.ClickException(
            "--repository-id and --repository-owner-id must be numeric GitHub IDs."
        )
    return normalized_repository_id, normalized_repository_owner_id


def _build_merge_train_policy_record(
    *,
    policy_file: Path,
    source_label: str,
    status: MergeTrainPolicyRecordStatus = "active",
) -> MergeTrainPolicyRecord:
    try:
        policy = load_merge_train_policy(policy_file)
        updated_at = utc_now_timestamp()
        return MergeTrainPolicyRecord(
            record_id=build_merge_train_policy_record_id(
                updated_at=updated_at,
                policy_sha256=policy.policy_sha256,
            ),
            status=status,
            source=source_label.strip() or "cli:import-policy",
            updated_at=updated_at,
            policy_sha256=policy.policy_sha256,
            policy=policy,
        )
    except (OSError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error


def _merge_train_policy_record_payload(record: MergeTrainPolicyRecord) -> dict[str, object]:
    return record.model_dump(mode="json")


def _build_runtime_key_safety_policy_record(
    *,
    policy_file: Path,
    source_label: str,
    status: RuntimeKeySafetyPolicyStatus = "active",
) -> RuntimeKeySafetyPolicyRecord:
    payload = _load_json_file(policy_file)
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        raise click.ClickException("Runtime key-safety policy JSON requires a rules array.")
    try:
        rules = tuple(RuntimeSecretSafetyRule.model_validate(rule) for rule in raw_rules)
        updated_at = str(payload.get("updated_at") or utc_now_timestamp())
        source = str(payload.get("source") or source_label).strip() or source_label
        record = RuntimeKeySafetyPolicyRecord(
            record_id="runtime-key-safety-policy-pending",
            status=status,
            source=source,
            updated_at=updated_at,
            rules=rules,
        )
    except ValidationError as error:
        raise click.ClickException(str(error)) from error
    final_record = record.model_copy(
        update={
            "record_id": "runtime-key-safety-policy-"
            f"{_launchplane_action_slug(updated_at)}-{record.policy_sha256[:12]}",
        }
    )
    return RuntimeKeySafetyPolicyRecord.model_validate(final_record.model_dump(mode="json"))


def summarize_product_profile_record(record: LaunchplaneProductProfileRecord) -> dict[str, object]:
    return {
        "product": record.product,
        "display_name": record.display_name,
        "repository": record.repository,
        "driver_id": record.driver_id,
        "image_repository": record.image.repository,
        "runtime_port": record.runtime_port,
        "health_path": record.health_path,
        "lane_count": len(record.lanes),
        "preview_enabled": record.preview.enabled,
        "preview_context": record.preview.context,
        "preview_enable_label": record.preview.enable_label,
        "updated_at": record.updated_at,
        "source": record.source,
    }


def _parse_product_profile_lanes(lanes_json: str) -> tuple[ProductLaneProfile, ...]:
    normalized_lanes_json = lanes_json.strip()
    if not normalized_lanes_json:
        return ()
    try:
        payload = json.loads(normalized_lanes_json)
    except JSONDecodeError as exc:
        raise click.ClickException(f"--lanes-json must be valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise click.ClickException("--lanes-json must decode to a JSON array.")
    try:
        return tuple(ProductLaneProfile.model_validate(item) for item in payload)
    except ValidationError as exc:
        raise click.ClickException(f"--lanes-json failed validation: {exc}") from exc


def _product_profile_store(database_url: str) -> PostgresRecordStore:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    return store


@click.group("authz-policies")
def authz_policies() -> None:
    """DB-backed Launchplane authorization policy commands."""


@click.group("runtime-key-safety")
def runtime_key_safety() -> None:
    """DB-backed runtime key-safety policy and evaluation commands."""


@click.group("merge-train-policies")
def merge_train_policies() -> None:
    """DB-backed Launchplane merge-train policy commands."""


@click.group("product-profiles")
def product_profiles() -> None:
    """DB-backed Launchplane product profile commands."""


@authz_policies.command("reconcile-managed")
@click.option(
    "--service-url",
    required=True,
    help="Deployed Launchplane service base URL.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a short-lived bearer token for the service.",
)
@click.option(
    "--session-cookie",
    default="",
    help="Launchplane browser session cookie. Use instead of --bearer-token-env.",
)
@click.option(
    "--request-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Managed reconciliation request JSON produced from operator-managed desired state.",
)
@click.option(
    "--idempotency-key",
    default="",
    help="Required stable Idempotency-Key when the request mode is apply.",
)
def authz_policies_reconcile_managed(
    service_url: str,
    bearer_token_env: str,
    session_cookie: str,
    request_file: Path,
    idempotency_key: str,
) -> None:
    bearer_token = ""
    if not session_cookie.strip():
        token_env_key = bearer_token_env.strip() or "LAUNCHPLANE_SERVICE_TOKEN"
        bearer_token = os.environ.get(token_env_key, "").strip()
        if not bearer_token:
            raise click.ClickException(
                f"{token_env_key} is required unless --session-cookie is provided."
            )
    payload = _load_json_file(request_file)
    response_payload = _post_launchplane_service_json(
        service_url=service_url,
        path="/v1/authz-policies/managed-rule-sets/reconcile",
        payload=payload,
        bearer_token=bearer_token,
        session_cookie=session_cookie,
        idempotency_key=idempotency_key,
    )
    click.echo(json.dumps(response_payload, indent=2, sort_keys=True))


@authz_policies.command("grant-workflow")
@click.option(
    "--service-url",
    required=True,
    help="Deployed Launchplane service base URL. Shared/prod grants go through the service API.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a short-lived bearer token for the service.",
)
@click.option(
    "--session-cookie",
    default="",
    help="Launchplane browser session cookie. Use instead of --bearer-token-env.",
)
@click.option(
    "--schema-version",
    type=click.IntRange(1, 2),
    default=1,
    show_default=True,
    help="Grant contract version. Use 2 only after the active policy is migrated to schema v2.",
)
@click.option("--repository", required=True, help="GitHub owner/repo grant target.")
@click.option(
    "--repository-id",
    required=True,
    help="Immutable numeric GitHub repository ID for the grant target.",
)
@click.option(
    "--repository-owner-id",
    required=True,
    help="Immutable numeric GitHub repository-owner ID for the grant target.",
)
@click.option(
    "--workflow-ref", "workflow_refs", multiple=True, help="Allowed workflow_ref pattern."
)
@click.option(
    "--job-workflow-ref",
    "job_workflow_refs",
    multiple=True,
    help="Allowed job_workflow_ref pattern.",
)
@click.option("--event-name", "event_names", multiple=True, help="Allowed GitHub event name.")
@click.option("--ref", "refs", multiple=True, help="Allowed Git ref.")
@click.option("--environment", "environments", multiple=True, help="Allowed GitHub environment.")
@click.option("--product", "products", multiple=True, required=True, help="Allowed product.")
@click.option("--context", "contexts", multiple=True, help="Allowed Launchplane context.")
@click.option("--instance", "instances", multiple=True, help="Allowed Launchplane instance.")
@click.option(
    "--action", "actions", multiple=True, required=True, help="Allowed Launchplane action."
)
@click.option("--reason", default="", help="Required audit reason when --apply is used.")
@click.option(
    "--related-issue",
    default="",
    help="Optional related GitHub issue, e.g. cbusillo/launchplane#83.",
)
@click.option("--source-label", default="cli:authz-grant-workflow", show_default=True)
@click.option(
    "--idempotency-key", default="", help="Optional explicit Idempotency-Key for apply requests."
)
@click.option("--dry-run", "mode", flag_value="dry_run", default="dry_run")
@click.option("--apply", "mode", flag_value="apply")
def authz_policies_grant_workflow(
    service_url: str,
    bearer_token_env: str,
    session_cookie: str,
    schema_version: int,
    repository: str,
    repository_id: str,
    repository_owner_id: str,
    workflow_refs: tuple[str, ...],
    job_workflow_refs: tuple[str, ...],
    event_names: tuple[str, ...],
    refs: tuple[str, ...],
    environments: tuple[str, ...],
    products: tuple[str, ...],
    contexts: tuple[str, ...],
    instances: tuple[str, ...],
    actions: tuple[str, ...],
    reason: str,
    related_issue: str,
    source_label: str,
    idempotency_key: str,
    mode: str,
) -> None:
    repository_id, repository_owner_id = _github_repository_id_pair(
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        required=True,
    )
    bearer_token = ""
    if not session_cookie.strip():
        token_env_key = bearer_token_env.strip() or "LAUNCHPLANE_SERVICE_TOKEN"
        bearer_token = os.environ.get(token_env_key, "").strip()
        if not bearer_token:
            raise click.ClickException(
                f"{token_env_key} is required unless --session-cookie is provided."
            )
    payload = {
        "schema_version": schema_version,
        "product": "launchplane",
        "mode": mode,
        "reason": reason,
        "related_issue": related_issue,
        "grant": {
            "repository": repository,
            "repository_id": repository_id,
            "repository_owner_id": repository_owner_id,
            "workflow_refs": list(workflow_refs),
            "job_workflow_refs": list(job_workflow_refs),
            "event_names": list(event_names),
            "refs": list(refs),
            "environments": list(environments),
            "products": list(products),
            "contexts": list(contexts),
            "instances": list(instances),
            "actions": list(actions),
            "source_label": source_label,
        },
    }
    response_payload = _post_launchplane_service_json(
        service_url=service_url,
        path="/v1/authz-policies/github-actions/grants",
        payload=payload,
        bearer_token=bearer_token,
        session_cookie=session_cookie,
        idempotency_key=idempotency_key,
    )
    click.echo(json.dumps(response_payload, indent=2, sort_keys=True))


@authz_policies.command("remove-workflow-rule")
@click.option(
    "--service-url",
    required=True,
    help="Deployed Launchplane service base URL. Shared/prod removals go through the service API.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a short-lived bearer token for the service.",
)
@click.option(
    "--session-cookie",
    default="",
    help="Launchplane browser session cookie. Use instead of --bearer-token-env.",
)
@click.option(
    "--schema-version",
    type=click.IntRange(1, 2),
    default=1,
    show_default=True,
    help="Removal contract version. Use 2 only after the active policy is migrated to schema v2.",
)
@click.option("--repository", required=True, help="GitHub owner/repo rule target.")
@click.option(
    "--repository-id",
    default="",
    help="Optional immutable numeric GitHub repository ID for an ID-bound rule.",
)
@click.option(
    "--repository-owner-id",
    default="",
    help="Optional immutable numeric GitHub repository-owner ID for an ID-bound rule.",
)
@click.option("--workflow-ref", "workflow_refs", multiple=True, help="Exact workflow_ref pattern.")
@click.option(
    "--job-workflow-ref",
    "job_workflow_refs",
    multiple=True,
    help="Exact job_workflow_ref pattern.",
)
@click.option("--event-name", "event_names", multiple=True, help="Exact GitHub event name.")
@click.option("--ref", "refs", multiple=True, help="Exact Git ref.")
@click.option("--environment", "environments", multiple=True, help="Exact GitHub environment.")
@click.option("--product", "products", multiple=True, required=True, help="Exact product.")
@click.option("--context", "contexts", multiple=True, help="Exact Launchplane context.")
@click.option("--instance", "instances", multiple=True, help="Exact Launchplane instance.")
@click.option("--action", "actions", multiple=True, required=True, help="Exact Launchplane action.")
@click.option("--reason", default="", help="Required audit reason when --apply is used.")
@click.option(
    "--related-issue",
    default="",
    help="Optional related GitHub issue, e.g. cbusillo/launchplane#1049.",
)
@click.option("--source-label", default="cli:authz-remove-workflow-rule", show_default=True)
@click.option(
    "--idempotency-key", default="", help="Optional explicit Idempotency-Key for apply requests."
)
@click.option("--dry-run", "mode", flag_value="dry_run", default="dry_run")
@click.option("--apply", "mode", flag_value="apply")
def authz_policies_remove_workflow_rule(
    service_url: str,
    bearer_token_env: str,
    session_cookie: str,
    schema_version: int,
    repository: str,
    repository_id: str,
    repository_owner_id: str,
    workflow_refs: tuple[str, ...],
    job_workflow_refs: tuple[str, ...],
    event_names: tuple[str, ...],
    refs: tuple[str, ...],
    environments: tuple[str, ...],
    products: tuple[str, ...],
    contexts: tuple[str, ...],
    instances: tuple[str, ...],
    actions: tuple[str, ...],
    reason: str,
    related_issue: str,
    source_label: str,
    idempotency_key: str,
    mode: str,
) -> None:
    repository_id, repository_owner_id = _github_repository_id_pair(
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        required=False,
    )
    bearer_token = ""
    if not session_cookie.strip():
        token_env_key = bearer_token_env.strip() or "LAUNCHPLANE_SERVICE_TOKEN"
        bearer_token = os.environ.get(token_env_key, "").strip()
        if not bearer_token:
            raise click.ClickException(
                f"{token_env_key} is required unless --session-cookie is provided."
            )
    payload = {
        "schema_version": schema_version,
        "product": "launchplane",
        "mode": mode,
        "reason": reason,
        "related_issue": related_issue,
        "removal": {
            "repository": repository,
            "repository_id": repository_id,
            "repository_owner_id": repository_owner_id,
            "workflow_refs": list(workflow_refs),
            "job_workflow_refs": list(job_workflow_refs),
            "event_names": list(event_names),
            "refs": list(refs),
            "environments": list(environments),
            "products": list(products),
            "contexts": list(contexts),
            "instances": list(instances),
            "actions": list(actions),
            "source_label": source_label,
        },
    }
    response_payload = _post_launchplane_service_json(
        service_url=service_url,
        path="/v1/authz-policies/github-actions/removals",
        payload=payload,
        bearer_token=bearer_token,
        session_cookie=session_cookie,
        idempotency_key=idempotency_key,
    )
    click.echo(json.dumps(response_payload, indent=2, sort_keys=True))


@authz_policies.command("grant-human")
@click.option(
    "--service-url",
    required=True,
    help="Deployed Launchplane service base URL. Shared/prod grants go through the service API.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a short-lived bearer token for the service.",
)
@click.option(
    "--session-cookie",
    default="",
    help="Launchplane browser session cookie. Use instead of --bearer-token-env.",
)
@click.option(
    "--schema-version",
    type=click.IntRange(1, 2),
    default=1,
    show_default=True,
    help="Grant contract version. Use 2 only after the active policy is migrated to schema v2.",
)
@click.option("--login", "logins", multiple=True, help="Allowed GitHub login.")
@click.option("--organization", "organizations", multiple=True, help="Allowed GitHub organization.")
@click.option("--team", "teams", multiple=True, help="Allowed GitHub team.")
@click.option(
    "--role",
    "roles",
    multiple=True,
    type=click.Choice(("read_only", "admin")),
    help="Allowed Launchplane role.",
)
@click.option("--product", "products", multiple=True, required=True, help="Allowed product.")
@click.option("--context", "contexts", multiple=True, help="Allowed Launchplane context.")
@click.option("--instance", "instances", multiple=True, help="Allowed Launchplane instance.")
@click.option(
    "--action", "actions", multiple=True, required=True, help="Allowed Launchplane action."
)
@click.option("--reason", default="", help="Required audit reason when --apply is used.")
@click.option(
    "--related-issue",
    default="",
    help="Optional related GitHub issue, e.g. cbusillo/launchplane#153.",
)
@click.option("--source-label", default="cli:authz-grant-human", show_default=True)
@click.option(
    "--idempotency-key", default="", help="Optional explicit Idempotency-Key for apply requests."
)
@click.option("--dry-run", "mode", flag_value="dry_run", default="dry_run")
@click.option("--apply", "mode", flag_value="apply")
def authz_policies_grant_human(
    service_url: str,
    bearer_token_env: str,
    session_cookie: str,
    schema_version: int,
    logins: tuple[str, ...],
    organizations: tuple[str, ...],
    teams: tuple[str, ...],
    roles: tuple[str, ...],
    products: tuple[str, ...],
    contexts: tuple[str, ...],
    instances: tuple[str, ...],
    actions: tuple[str, ...],
    reason: str,
    related_issue: str,
    source_label: str,
    idempotency_key: str,
    mode: str,
) -> None:
    bearer_token = ""
    if not session_cookie.strip():
        token_env_key = bearer_token_env.strip() or "LAUNCHPLANE_SERVICE_TOKEN"
        bearer_token = os.environ.get(token_env_key, "").strip()
        if not bearer_token:
            raise click.ClickException(
                f"{token_env_key} is required unless --session-cookie is provided."
            )
    payload = {
        "schema_version": schema_version,
        "product": "launchplane",
        "mode": mode,
        "reason": reason,
        "related_issue": related_issue,
        "grant": {
            "logins": list(logins),
            "organizations": list(organizations),
            "teams": list(teams),
            "roles": list(roles),
            "products": list(products),
            "contexts": list(contexts),
            "instances": list(instances),
            "actions": list(actions),
            "source_label": source_label,
        },
    }
    response_payload = _post_launchplane_service_json(
        service_url=service_url,
        path="/v1/authz-policies/github-humans/grants",
        payload=payload,
        bearer_token=bearer_token,
        session_cookie=session_cookie,
        idempotency_key=idempotency_key,
    )
    click.echo(json.dumps(response_payload, indent=2, sort_keys=True))


@authz_policies.command("grant-terminal-agent")
@click.option(
    "--service-url",
    required=True,
    help="Deployed Launchplane service base URL. Shared/prod grants go through the service API.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a short-lived bearer token for the service.",
)
@click.option(
    "--session-cookie",
    default="",
    help="Launchplane browser session cookie. Use instead of --bearer-token-env.",
)
@click.option(
    "--schema-version",
    type=click.IntRange(1, 2),
    default=1,
    show_default=True,
    help="Grant contract version. Use 2 only after the active policy is migrated to schema v2.",
)
@click.option(
    "--subject", "subjects", multiple=True, required=True, help="Allowed terminal-agent subject."
)
@click.option(
    "--token-label",
    "token_labels",
    multiple=True,
    required=True,
    help="Allowed terminal-agent token label.",
)
@click.option("--product", "products", multiple=True, required=True, help="Allowed product.")
@click.option("--context", "contexts", multiple=True, help="Allowed Launchplane context.")
@click.option("--instance", "instances", multiple=True, help="Allowed Launchplane instance.")
@click.option(
    "--action", "actions", multiple=True, required=True, help="Allowed Launchplane action."
)
@click.option("--reason", default="", help="Required audit reason when --apply is used.")
@click.option(
    "--related-issue",
    default="",
    help="Optional related GitHub issue, e.g. cbusillo/launchplane#426.",
)
@click.option("--source-label", default="cli:authz-grant-terminal-agent", show_default=True)
@click.option(
    "--idempotency-key", default="", help="Optional explicit Idempotency-Key for apply requests."
)
@click.option("--dry-run", "mode", flag_value="dry_run", default="dry_run")
@click.option("--apply", "mode", flag_value="apply")
def authz_policies_grant_terminal_agent(
    service_url: str,
    bearer_token_env: str,
    session_cookie: str,
    schema_version: int,
    subjects: tuple[str, ...],
    token_labels: tuple[str, ...],
    products: tuple[str, ...],
    contexts: tuple[str, ...],
    instances: tuple[str, ...],
    actions: tuple[str, ...],
    reason: str,
    related_issue: str,
    source_label: str,
    idempotency_key: str,
    mode: str,
) -> None:
    bearer_token = ""
    if not session_cookie.strip():
        token_env_key = bearer_token_env.strip() or "LAUNCHPLANE_SERVICE_TOKEN"
        bearer_token = os.environ.get(token_env_key, "").strip()
        if not bearer_token:
            raise click.ClickException(
                f"{token_env_key} is required unless --session-cookie is provided."
            )
    payload = {
        "schema_version": schema_version,
        "product": "launchplane",
        "mode": mode,
        "reason": reason,
        "related_issue": related_issue,
        "grant": {
            "subjects": list(subjects),
            "token_labels": list(token_labels),
            "products": list(products),
            "contexts": list(contexts),
            "instances": list(instances),
            "actions": list(actions),
            "source_label": source_label,
        },
    }
    response_payload = _post_launchplane_service_json(
        service_url=service_url,
        path="/v1/authz-policies/terminal-agents/grants",
        payload=payload,
        bearer_token=bearer_token,
        session_cookie=session_cookie,
        idempotency_key=idempotency_key,
    )
    click.echo(json.dumps(response_payload, indent=2, sort_keys=True))


@runtime_key_safety.command("list-policies")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for runtime key-safety policy records.",
)
@click.option("--status", "status_filter", default="", help="Optional policy status filter.")
def runtime_key_safety_list_policies(database_url: str, status_filter: str) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        records = postgres_store.list_runtime_key_safety_policy_records(status=status_filter)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "count": len(records),
                "records": [
                    summarize_runtime_key_safety_policy_record(record) for record in records
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


@merge_train_policies.command("list")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for merge-train policy records.",
)
@click.option("--status", "status_filter", default="", help="Optional policy status filter.")
def merge_train_policies_list(database_url: str, status_filter: str) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        records = postgres_store.list_merge_train_policy_records(status=status_filter)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "count": len(records),
                "records": [summarize_merge_train_policy_record(record) for record in records],
            },
            indent=2,
            sort_keys=True,
        )
    )


@merge_train_policies.command("build-import-request")
@click.option(
    "--policy-file",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="TOML policy file containing merge-train repository policies.",
)
@click.option("--status", type=click.Choice(["active", "superseded"]), default="active")
@click.option("--source-label", default="cli:import-policy", show_default=True)
@click.option("--reason", default="", help="Required audit reason when --apply is used.")
@click.option("--dry-run", "mode", flag_value="dry_run", default="dry_run")
@click.option("--apply", "mode", flag_value="apply")
def merge_train_policies_build_import_request(
    policy_file: Path,
    status: MergeTrainPolicyRecordStatus,
    source_label: str,
    reason: str,
    mode: str,
) -> None:
    normalized_reason = reason.strip()
    if mode == "apply" and not normalized_reason:
        raise click.ClickException("reason is required when apply is true")
    record = _build_merge_train_policy_record(
        policy_file=policy_file,
        source_label=source_label,
        status=status,
    )
    click.echo(
        json.dumps(
            {
                "schema_version": 1,
                "product": "launchplane",
                "mode": mode,
                "reason": normalized_reason,
                "record": _merge_train_policy_record_payload(record),
            },
            indent=2,
            sort_keys=True,
        )
    )


@merge_train_policies.command("import-policy")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    default="",
    help="Postgres connection string for merge-train policy records.",
)
@click.option(
    "--service-url",
    default="",
    help="Deployed Launchplane service base URL. Shared/prod imports go through the service API.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a Launchplane bearer token for --service-url.",
)
@click.option(
    "--session-cookie",
    default="",
    help="Launchplane browser session cookie for --service-url. Use instead of --bearer-token-env.",
)
@click.option(
    "--policy-file",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="TOML policy file containing merge-train repository policies.",
)
@click.option("--status", type=click.Choice(["active", "superseded"]), default="active")
@click.option("--source-label", default="cli:import-policy", show_default=True)
@click.option("--reason", default="", help="Required audit reason for --service-url apply.")
@click.option(
    "--idempotency-key", default="", help="Optional explicit Idempotency-Key for service import."
)
@click.option("--dry-run", "mode", flag_value="dry_run", default="dry_run")
@click.option("--apply", "mode", flag_value="apply")
@_direct_db_mutation_acknowledgement_option
def merge_train_policies_import_policy(
    database_url: str,
    service_url: str,
    bearer_token_env: str,
    session_cookie: str,
    policy_file: Path,
    status: MergeTrainPolicyRecordStatus,
    source_label: str,
    reason: str,
    idempotency_key: str,
    mode: str,
    allow_direct_db_mutation: bool,
) -> None:
    record = _build_merge_train_policy_record(
        policy_file=policy_file,
        source_label=source_label,
        status=status,
    )
    if service_url.strip():
        bearer_token = ""
        if not session_cookie.strip():
            token_env_key = bearer_token_env.strip() or "LAUNCHPLANE_SERVICE_TOKEN"
            bearer_token = os.environ.get(token_env_key, "").strip()
            if not bearer_token:
                raise click.ClickException(
                    f"{token_env_key} is required unless --session-cookie is provided."
                )
        response_payload = _post_launchplane_service_json(
            service_url=service_url,
            path="/v1/merge-train/policies/import",
            payload={
                "schema_version": 1,
                "product": "launchplane",
                "mode": mode,
                "reason": reason,
                "record": _merge_train_policy_record_payload(record),
            },
            bearer_token=bearer_token,
            session_cookie=session_cookie,
            idempotency_key=idempotency_key,
        )
        click.echo(json.dumps(response_payload, indent=2, sort_keys=True))
        return
    if mode != "apply":
        raise click.ClickException("Direct database import requires --apply.")
    if not database_url.strip():
        raise click.ClickException("Provide --service-url or --database-url.")
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        postgres_store.write_merge_train_policy_record(record)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {"status": "ok", "record": summarize_merge_train_policy_record(record)},
            indent=2,
            sort_keys=True,
        )
    )


@runtime_key_safety.command("import-policy")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for runtime key-safety policy records.",
)
@click.option(
    "--policy-file",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="JSON policy file containing runtime key-safety rules.",
)
@click.option("--status", type=click.Choice(["active", "superseded"]), default="active")
@click.option("--source-label", default="cli:import-policy", show_default=True)
@_direct_db_mutation_acknowledgement_option
def runtime_key_safety_import_policy(
    database_url: str,
    policy_file: Path,
    status: RuntimeKeySafetyPolicyStatus,
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    record = _build_runtime_key_safety_policy_record(
        policy_file=policy_file,
        source_label=source_label,
        status=status,
    )
    try:
        postgres_store.write_runtime_key_safety_policy_record(record)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {"status": "ok", "record": summarize_runtime_key_safety_policy_record(record)},
            indent=2,
            sort_keys=True,
        )
    )


@runtime_key_safety.command("evaluate")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for runtime key-safety policy records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option(
    "--environment-class",
    "environment_class",
    type=click.Choice(["prod", "testing", "preview", "dev", "unknown"]),
    required=True,
)
@click.option("--binding-key", "binding_keys", multiple=True, required=True)
def runtime_key_safety_evaluate(
    database_url: str,
    context_name: str,
    instance_name: str,
    environment_class: RuntimeEnvironmentClass,
    binding_keys: tuple[str, ...],
) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        policy_record = latest_active_runtime_key_safety_policy(postgres_store)
        evaluation = evaluate_runtime_key_safety_from_store(
            record_store=postgres_store,
            policy_record=policy_record,
            target=RuntimeKeySafetyTarget(
                context=context_name,
                instance=instance_name,
                environment_class=environment_class,
            ),
            required_binding_keys=binding_keys,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "policy": summarize_runtime_key_safety_policy_record(policy_record),
                "evaluation": evaluation.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
    )


@product_profiles.command("list")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane product profile records.",
)
@click.option("--driver-id", default="", help="Optional driver id filter.")
def product_profiles_list(database_url: str, driver_id: str) -> None:
    store = _product_profile_store(database_url)
    try:
        records = store.list_product_profile_records(driver_id=driver_id)
    finally:
        store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "count": len(records),
                "driver_id": driver_id.strip(),
                "profiles": [summarize_product_profile_record(record) for record in records],
            },
            indent=2,
            sort_keys=True,
        )
    )


@product_profiles.command("show")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane product profile records.",
)
@click.option("--product", required=True, help="Product key to show.")
def product_profiles_show(database_url: str, product: str) -> None:
    store = _product_profile_store(database_url)
    try:
        record = store.read_product_profile_record(product)
    finally:
        store.close()
    click.echo(
        json.dumps(
            {"status": "ok", "profile": record.model_dump(mode="json")},
            indent=2,
            sort_keys=True,
        )
    )


@product_profiles.command("audit-context-cutover")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane product and runtime records.",
)
@click.option("--product", required=True, help="Product key to audit.")
@click.option("--source-context", required=True, help="Legacy context to inspect.")
@click.option("--target-context", required=True, help="Canonical context to inspect.")
@click.option(
    "--preview-context",
    default="",
    help="Preview context to inspect; defaults to the product profile preview context.",
)
def product_profiles_audit_context_cutover(
    database_url: str,
    product: str,
    source_context: str,
    target_context: str,
    preview_context: str,
) -> None:
    record_store = _product_profile_store(database_url)
    try:
        payload = control_plane_product_context_audit.build_product_context_cutover_audit(
            record_store=record_store,
            product=product,
            source_context=source_context,
            target_context=target_context,
            preview_context=preview_context,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    finally:
        record_store.close()

    click.echo(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        )
    )


@product_profiles.command("upsert")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane product profile records.",
)
@click.option("--product", required=True, help="Stable product key.")
@click.option("--display-name", required=True, help="Human-facing product name.")
@click.option("--repository", required=True, help="Owning GitHub repository, owner/name.")
@click.option("--driver-id", default="generic-web", show_default=True)
@click.option("--image-repository", required=True, help="Container image repository.")
@click.option("--runtime-port", type=int, required=True, help="Container HTTP port.")
@click.option("--health-path", required=True, help="HTTP health path, starting with /.")
@click.option(
    "--lanes-json",
    default="[]",
    show_default=True,
    help="JSON array of lane objects with instance, context, base_url, and health_url.",
)
@click.option("--preview-enabled/--preview-disabled", default=False, show_default=True)
@click.option("--preview-context", default="", help="Preview context when previews are enabled.")
@click.option(
    "--preview-enable-label",
    default=LAUNCHPLANE_PREVIEW_ENABLE_LABEL,
    show_default=True,
    help="Pull request label that opts this product into Launchplane previews.",
)
@click.option("--preview-slug-template", default="pr-{number}", show_default=True)
@click.option("--updated-at", default="", help="Override updated timestamp.")
@click.option("--source-label", default="cli:product-profiles:upsert", show_default=True)
@_direct_db_mutation_acknowledgement_option
def product_profiles_upsert(
    database_url: str,
    product: str,
    display_name: str,
    repository: str,
    driver_id: str,
    image_repository: str,
    runtime_port: int,
    health_path: str,
    lanes_json: str,
    preview_enabled: bool,
    preview_context: str,
    preview_enable_label: str,
    preview_slug_template: str,
    updated_at: str,
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    try:
        record = LaunchplaneProductProfileRecord(
            product=product,
            display_name=display_name,
            repository=repository,
            driver_id=driver_id,
            image=ProductImageProfile(repository=image_repository),
            runtime_port=runtime_port,
            health_path=health_path,
            lanes=_parse_product_profile_lanes(lanes_json),
            preview=ProductPreviewProfile(
                enabled=preview_enabled,
                context=preview_context,
                enable_label=preview_enable_label,
                slug_template=preview_slug_template,
            ),
            updated_at=updated_at.strip() or utc_now_timestamp(),
            source=source_label,
        )
    except ValidationError as exc:
        raise click.ClickException(f"Product profile failed validation: {exc}") from exc
    store = _product_profile_store(database_url)
    try:
        store.write_product_profile_record(record)
    finally:
        store.close()
    click.echo(
        json.dumps(
            {"status": "ok", "profile": record.model_dump(mode="json")},
            indent=2,
            sort_keys=True,
        )
    )
