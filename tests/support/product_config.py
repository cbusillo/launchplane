from typing import cast


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


def _meta_product_config_payload(
    *, mode: str = "dry-run", reason: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "product": "sellyouroutboard",
        "context": "sellyouroutboard",
        "instance": "prod",
        "source_label": "product-config-ui-test",
        "runtime_env": {
            "scope": "instance",
            "env": {
                "NEXT_PUBLIC_META_PIXEL_ID": "123456789012345",
            },
        },
        "secrets": [
            {
                "name": "META_CONVERSIONS_API_TOKEN",
                "binding_key": "META_CONVERSIONS_API_TOKEN",
                "value": "meta-conversions-api-secret-value",
                "scope": "context_instance",
                "description": "Meta conversions API token",
            }
        ],
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def _product_config_secrets(payload: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], payload["secrets"])
