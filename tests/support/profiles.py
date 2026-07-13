from __future__ import annotations

from typing import cast


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


def _odoo_preview_profile_payload(product: str = "odoo-tenant-cm") -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "display_name": "CM Odoo",
        "repository": f"cbusillo/{product}",
        "driver_id": "odoo",
        "image": {"repository": f"ghcr.io/cbusillo/{product}"},
        "runtime_port": 8069,
        "health_path": "/web/health",
        "lanes": (
            {
                "instance": "testing",
                "context": "cm",
                "base_url": "https://cm-testing.example.com",
                "health_url": "https://cm-testing.example.com/web/health",
            },
        ),
        "preview": {
            "enabled": True,
            "context": "cm",
            "slug_template": "pr-{number}",
            "app_name_prefix": "cm-odoo-preview",
        },
        "updated_at": "2026-05-09T12:00:00Z",
        "source": "test",
    }


def _odoo_profile_payload_with_prod_lane(
    product: str = "odoo-tenant-cm",
) -> dict[str, object]:
    payload = _odoo_preview_profile_payload(product)
    lanes = list(cast(tuple[dict[str, object], ...], payload["lanes"]))
    lanes.append(
        {
            "instance": "prod",
            "context": "cm",
            "base_url": "https://cm.example.com",
            "health_url": "https://cm.example.com/web/health",
        }
    )
    payload["lanes"] = tuple(lanes)
    return payload


def _product_profile_payload_with_prod(product: str = "sellyouroutboard") -> dict[str, object]:
    payload = _product_profile_payload(product)
    lanes = list(cast(tuple[dict[str, object], ...], payload["lanes"]))
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


def _generic_site_profile_payload(product: str = "example-site") -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "display_name": "Example Site",
        "repository": f"every/{product}",
        "driver_id": "generic-web",
        "image": {"repository": f"ghcr.io/every/{product}"},
        "runtime_port": 3000,
        "health_path": "/healthz",
        "lanes": (
            {
                "instance": "testing",
                "context": product,
                "base_url": f"https://testing.{product}.example",
                "health_url": f"https://testing.{product}.example/healthz",
            },
            {
                "instance": "prod",
                "context": product,
                "base_url": f"https://{product}.example",
                "health_url": f"https://{product}.example/healthz",
            },
        ),
        "preview": {
            "enabled": True,
            "context": product,
            "slug_template": "pr-{number}",
        },
        "expected_config": {
            "runtime_environment_keys": [
                {"key": "INTERNAL_CALLBACK_URL", "context": product, "instance": "prod"},
                {"key": "RESEND_FROM_EMAIL", "context": product, "instance": "prod"},
            ],
            "managed_secret_bindings": [
                {"binding_key": "SMTP_PASSWORD", "context": product, "instance": "prod"},
                {"binding_key": "RESEND_API_KEY", "context": product, "instance": "prod"},
            ],
        },
        "updated_at": "2026-05-02T22:30:00Z",
        "source": "test",
    }
