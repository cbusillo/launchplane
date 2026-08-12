from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _write_odoo_preview_template_runtime_environment(
    *, store: Any, context: str = "cm", instance: str = "testing"
) -> None:
    store.write_runtime_environment_record(
        RuntimeEnvironmentRecord(
            scope="instance",
            context=context,
            instance=instance,
            env={"ODOO_DB_USER": "odoo"},
            updated_at="2026-05-09T12:30:00Z",
            source_label="test",
        )
    )
    with patch.dict(
        os.environ,
        {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
        clear=True,
    ):
        for name, binding_key, value in (
            ("db-password", "ODOO_DB_PASSWORD", "template-db-secret"),
            ("master-password", "ODOO_MASTER_PASSWORD", "template-master-secret"),
            ("admin-password", "ODOO_ADMIN_PASSWORD", "template-admin-secret"),
        ):
            control_plane_secrets.write_secret_value(
                record_store=store,
                scope="context_instance",
                integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                name=name,
                plaintext_value=value,
                binding_key=binding_key,
                context_name=context,
                instance_name=instance,
                actor="test",
                source_label="test",
            )


def _write_runtime_key_safety_policy(
    *,
    database_url: str,
    context_name: str = "sellyouroutboard-prod",
    instance_name: str = "prod",
    rules: tuple[RuntimeSecretSafetyRule, ...] | None = None,
) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-service-test",
                status="active",
                source="test",
                updated_at="2026-05-05T20:00:00Z",
                rules=rules
                or (
                    RuntimeSecretSafetyRule(
                        binding_key="SMTP_PASSWORD",
                        secret_class="prod_only",
                        allowed_contexts=(context_name,),
                        allowed_instances=(instance_name,),
                    ),
                ),
            )
        )
    finally:
        store.close()


def _seed_tracked_target_records(
    *,
    database_url: str,
    context: str,
    instance: str,
    target_id: str,
    target_type: Literal["compose", "application"],
    target_name: str,
    domains: tuple[str, ...] = (),
    deploy_timeout_seconds: int | None = None,
    source_type: str = "raw",
    custom_git_url: str = "",
    env: dict[str, str] | None = None,
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
                source_type=source_type,
                custom_git_url=custom_git_url,
                env=env or {},
                deploy_timeout_seconds=deploy_timeout_seconds,
                domains=domains,
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


def _seed_generic_web_deploy_target_records(
    *,
    store: PostgresRecordStore,
    context: str,
    instance: str,
    target_id: str,
    target_name: str,
) -> None:
    normalized_context = context.strip()
    normalized_instance = instance.strip()
    store.write_provider_target_record(
        ProviderTargetRecord(
            context=normalized_context,
            instance=normalized_instance,
            provider_id="dokploy",
            target_category="application",
            target_id=target_id,
            display_name=target_name,
            provider_target_type="application",
            updated_at="2026-05-01T00:00:00Z",
            source_label="test:generic-web-provider-target",
        )
    )
    store.write_dokploy_target_record(
        DokployTargetRecord(
            context=normalized_context,
            instance=normalized_instance,
            target_type="application",
            target_name=target_name,
            updated_at="2026-05-01T00:00:00Z",
            source_label="test:generic-web-provider-target",
        )
    )
    store.write_dokploy_target_id_record(
        DokployTargetIdRecord(
            context=normalized_context,
            instance=normalized_instance,
            target_id=target_id,
            updated_at="2026-05-01T00:00:00Z",
            source_label="test:generic-web-provider-target",
        )
    )
