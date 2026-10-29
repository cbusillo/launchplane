from typing import Literal


PreviewSecretEncoding = Literal["urlsafe", "base64"]


def migrate_legacy_postgres_preview(value: object) -> object:
    if not isinstance(value, dict) or value.get("transport_extension") != "verireel-postgres":
        return value
    return {
        **value,
        "transport_extension": "postgres",
        "generated_secret_env": value.get(
            "generated_secret_env",
            {
                "BETTER_AUTH_SECRET": "urlsafe",
                "VERIREEL_SECRETS_MASTER_KEY": "base64",
                "VERIREEL_CRON_SECRET": "urlsafe",
                "VERIREEL_SMOKE_MAINTENANCE_SECRET": "urlsafe",
            },
        ),
        "preview_url_env_keys": value.get("preview_url_env_keys")
        or (
            "VERIREEL_APP_URL",
            "BETTER_AUTH_URL",
        ),
        "preview_domain_env_keys": value.get("preview_domain_env_keys")
        or (
            "DOKPLOY_PREVIEW_DOMAIN",
            "NEXT_PUBLIC_DOKPLOY_PREVIEW_DOMAIN",
        ),
    }
