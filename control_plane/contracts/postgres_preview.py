from __future__ import annotations

from typing import TYPE_CHECKING

from typing import Literal


if TYPE_CHECKING:
    from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


PreviewSecretEncoding = Literal["urlsafe", "base64"]


def migrate_legacy_postgres_preview(value: object) -> object:
    if not isinstance(value, dict) or value.get("transport_extension") != "verireel-postgres":
        return value
    return {
        **value,
        "transport_extension": "postgres",
        "generated_secret_env": {
            "BETTER_AUTH_SECRET": "urlsafe",
            "VERIREEL_SECRETS_MASTER_KEY": "base64",
            "VERIREEL_CRON_SECRET": "urlsafe",
            "VERIREEL_SMOKE_MAINTENANCE_SECRET": "urlsafe",
        },
        "preview_url_env_keys": ("VERIREEL_APP_URL", "BETTER_AUTH_URL"),
        "preview_domain_env_keys": ("DOKPLOY_PREVIEW_DOMAIN", "NEXT_PUBLIC_DOKPLOY_PREVIEW_DOMAIN"),
        "domain_certificate_type": "none",
    }


def effective_postgres_preview_profile(
    profile: "LaunchplaneProductProfileRecord",
) -> "LaunchplaneProductProfileRecord":
    from control_plane.contracts.product_profile_record import ProductPreviewProfile

    if profile.preview.transport_extension != "verireel-postgres":
        return profile
    preview = ProductPreviewProfile.model_validate(
        migrate_legacy_postgres_preview(profile.preview.model_dump(mode="json"))
    )
    return profile.model_copy(
        update={"preview": preview, "runtime_port": 3000, "health_path": "/api/health"}
    )
