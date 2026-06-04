from collections.abc import Callable
from json import JSONDecodeError
import json
from pathlib import Path

import click
from pydantic import ValidationError

from control_plane import product_config as control_plane_product_config
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.product_onboarding import apply_product_onboarding_manifest


_DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)


def register_product_config_commands(
    main: click.Group,
    *,
    summarize_product_profile_record: Callable[
        [LaunchplaneProductProfileRecord], dict[str, object]
    ],
    summarize_dokploy_target_record: Callable[..., dict[str, object]],
    dokploy_target_route: Callable[[DokployTargetRecord | DokployTargetIdRecord], tuple[str, str]],
    target_id_map: Callable[[tuple[DokployTargetIdRecord, ...]], dict[tuple[str, str], str]],
    summarize_runtime_environment_record: Callable[[RuntimeEnvironmentRecord], dict[str, object]],
    summarize_secret_binding_record: Callable[[SecretBinding], dict[str, object]],
) -> None:
    global _PRODUCT_ONBOARDING_APPLY_CALLBACKS
    _PRODUCT_ONBOARDING_APPLY_CALLBACKS = _ProductOnboardingApplyCallbacks(
        summarize_product_profile_record=summarize_product_profile_record,
        summarize_dokploy_target_record=summarize_dokploy_target_record,
        dokploy_target_route=dokploy_target_route,
        target_id_map=target_id_map,
        summarize_runtime_environment_record=summarize_runtime_environment_record,
        summarize_secret_binding_record=summarize_secret_binding_record,
    )
    main.add_command(product_config)
    main.add_command(product_onboarding)


@click.group("product-config")
def product_config() -> None:
    """Trusted product runtime config apply commands."""


@click.group("product-onboarding")
def product_onboarding() -> None:
    """Idempotent product onboarding record commands."""


@product_config.command("apply")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string from a trusted Launchplane context.",
)
@click.option(
    "--input-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Operator-approved JSON bundle containing runtime_env and secrets.",
)
@click.option("--actor", default="cli", show_default=True)
@click.option("--source-label", default="product-config-apply", show_default=True)
@click.option("--dry-run", "dry_run", is_flag=True, default=False)
@click.option("--apply", "apply_changes", is_flag=True, default=False)
def product_config_apply(
    database_url: str,
    input_file: Path,
    actor: str,
    source_label: str,
    dry_run: bool,
    apply_changes: bool,
) -> None:
    if dry_run == apply_changes:
        raise click.ClickException("Choose exactly one of --dry-run or --apply.")

    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        payload = control_plane_product_config.apply_product_config_bundle(
            record_store=postgres_store,
            payload=control_plane_product_config.load_product_config_apply_payload(input_file),
            mode="apply" if apply_changes else "dry-run",
            actor=actor,
            source_label=source_label,
        )
    except control_plane_product_config.ProductConfigError as error:
        raise click.ClickException(str(error)) from error
    finally:
        postgres_store.close()
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@product_onboarding.command("apply")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane product onboarding records.",
)
@click.option(
    "--manifest-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Operator-approved JSON product onboarding manifest.",
)
@click.option("--updated-at", default="", help="Override manifest updated_at timestamp.")
def product_onboarding_apply(database_url: str, manifest_file: Path, updated_at: str) -> None:
    callbacks = _product_onboarding_apply_callbacks()
    try:
        manifest_payload = json.loads(manifest_file.read_text())
    except JSONDecodeError as exc:
        raise click.ClickException(
            f"Product onboarding manifest must be valid JSON: {exc}"
        ) from exc
    try:
        manifest = ProductOnboardingManifest.model_validate(manifest_payload)
    except ValidationError as exc:
        raise click.ClickException(f"Product onboarding manifest failed validation: {exc}") from exc

    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        result = apply_product_onboarding_manifest(
            record_store=store,
            manifest=manifest,
            updated_at=updated_at,
        )
    finally:
        store.close()

    target_id_map = callbacks.target_id_map(result.provider_target_ids)
    provider_targets = [
        callbacks.summarize_dokploy_target_record(
            record,
            target_id=target_id_map.get(callbacks.dokploy_target_route(record), ""),
        )
        for record in result.provider_targets
    ]
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "product": result.product,
                "product_profile": callbacks.summarize_product_profile_record(
                    result.product_profile
                ),
                "provider_target_count": len(result.provider_targets),
                "provider_targets": provider_targets,
                "runtime_environment_record_count": len(result.runtime_environments),
                "runtime_environment_records": [
                    callbacks.summarize_runtime_environment_record(record)
                    for record in result.runtime_environments
                ],
                "secret_binding_count": len(result.secret_bindings),
                "secret_bindings": [
                    callbacks.summarize_secret_binding_record(binding)
                    for binding in result.secret_bindings
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


class _ProductOnboardingApplyCallbacks:
    def __init__(
        self,
        *,
        summarize_product_profile_record: Callable[
            [LaunchplaneProductProfileRecord], dict[str, object]
        ],
        summarize_dokploy_target_record: Callable[..., dict[str, object]],
        dokploy_target_route: Callable[
            [DokployTargetRecord | DokployTargetIdRecord], tuple[str, str]
        ],
        target_id_map: Callable[[tuple[DokployTargetIdRecord, ...]], dict[tuple[str, str], str]],
        summarize_runtime_environment_record: Callable[
            [RuntimeEnvironmentRecord], dict[str, object]
        ],
        summarize_secret_binding_record: Callable[[SecretBinding], dict[str, object]],
    ) -> None:
        self.summarize_product_profile_record = summarize_product_profile_record
        self.summarize_dokploy_target_record = summarize_dokploy_target_record
        self.dokploy_target_route = dokploy_target_route
        self.target_id_map = target_id_map
        self.summarize_runtime_environment_record = summarize_runtime_environment_record
        self.summarize_secret_binding_record = summarize_secret_binding_record


_PRODUCT_ONBOARDING_APPLY_CALLBACKS: _ProductOnboardingApplyCallbacks | None = None


def _product_onboarding_apply_callbacks() -> _ProductOnboardingApplyCallbacks:
    if _PRODUCT_ONBOARDING_APPLY_CALLBACKS is None:
        raise click.ClickException("Product onboarding CLI callbacks were not registered.")
    return _PRODUCT_ONBOARDING_APPLY_CALLBACKS
