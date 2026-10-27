import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import click
import requests
from pydantic import ValidationError

from control_plane.production_backup_credentials import BackupCredentialBindingRequest


@click.command(
    "backup-capture-key", help="Generate or read back a guest capture key in the deployed service."
)
@click.option("--service-url", envvar="LAUNCHPLANE_OPERATOR_URL", required=True)
@click.option("--product", required=True)
@click.option("--mode", type=click.Choice(("dry-run", "apply", "readback")), required=True)
@click.option("--request-file", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--reviewed-plan-sha256", default="")
@click.option("--idempotency-key", default="")
def backup_capture_key(
    service_url: str,
    product: str,
    mode: str,
    request_file: Path | None,
    reviewed_plan_sha256: str,
    idempotency_key: str,
) -> None:
    parsed = urlsplit(service_url)
    token = os.environ.get("LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", "").strip()
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not token
    ):
        raise click.ClickException(
            "Use the configured HTTPS service origin and existing operator token."
        )
    route = f"/v1/products/{quote(product, safe='')}/environments/prod/backup-worker-bindings"
    body = None
    if mode != "readback":
        if request_file is None or (mode == "apply" and not idempotency_key.strip()):
            raise click.ClickException(
                "Supply the public request and original operation key for apply."
            )
        try:
            payload = json.loads(request_file.read_text())
            body = BackupCredentialBindingRequest.model_validate(
                {
                    "bindings": {},
                    **payload,
                    "mode": mode,
                    "reviewed_plan_sha256": reviewed_plan_sha256,
                }
            )
            if not body.generate_capture_key:
                raise ValueError("Generation request required.")
        except (ValueError, TypeError, ValidationError):
            raise click.ClickException(
                "Invalid public capture-key request; no private value is accepted."
            ) from None
        route += "/apply"
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(
                "GET" if mode == "readback" else "POST",
                service_url.rstrip("/") + route,
                json=body.model_dump(mode="json") if body else None,
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idempotency_key},
                timeout=(10, 60),
                allow_redirects=False,
            )
            if response.status_code not in {200, 202}:
                error = response.json()
                click.echo(
                    json.dumps(
                        {
                            "http_status": response.status_code,
                            "trace_id": error.get("trace_id", ""),
                            "error_code": error.get("error", {}).get("code", "service_error"),
                            "message": error.get("error", {}).get("message", "Service refused."),
                        }
                    )
                )
                raise click.ClickException(
                    "Service refused; preserve the original request and key."
                )
            result = response.json()
    except (requests.RequestException, ValueError):
        raise click.ClickException(
            "Response unavailable; preserve the original request/key and read back before retrying."
        ) from None
    allowed = {
        "plan_sha256",
        "changed",
        "applied",
        "product",
        "context",
        "instance",
        "capture_started",
        "boundary_binding_id",
        "public_key",
        "fingerprint",
        "private_key_binding",
        "known_hosts_binding",
        "generated_capture_keys",
        "credentials",
    }
    click.echo(
        json.dumps(
            {
                "trace_id": result.get("trace_id", ""),
                "replayed": result.get("replayed", False),
                "result": {
                    key: value
                    for key, value in (result.get("result") or {}).items()
                    if key in allowed
                },
            },
            indent=2,
        )
    )
