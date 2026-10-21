import json
import os
import re
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass

import click

from control_plane.drivers.route_paths import INGRESS_ROUTE_APPLY_ROUTE


@dataclass(frozen=True)
class IngressCliCallbacks:
    post_launchplane_service_json: Callable[..., dict[str, object]]


_callbacks: IngressCliCallbacks | None = None


def register_ingress_commands(main: click.Group, *, callbacks: IngressCliCallbacks) -> None:
    global _callbacks
    _callbacks = callbacks
    main.add_command(ingress)


def _ingress_callbacks() -> IngressCliCallbacks:
    if _callbacks is None:
        raise click.ClickException("Launchplane ingress CLI callbacks are not configured.")
    return _callbacks


def _post_launchplane_service_json(**kwargs: object) -> dict[str, object]:
    return _ingress_callbacks().post_launchplane_service_json(**kwargs)


@click.group()
def ingress() -> None:
    """Ingress route control commands."""


@ingress.command("health-history-summary")
@click.option(
    "--page", "pages", required=True, multiple=True, type=click.Path(exists=True, path_type=Path)
)
@click.option(
    "--check",
    "checks",
    required=True,
    multiple=True,
    help="Required KIND:NAME check; repeat for the complete lane policy.",
)
@click.option(
    "--identity-check",
    "identity_checks",
    required=True,
    multiple=True,
    help="Required KIND:NAME identity witness, a subset of --check.",
)
@click.option(
    "--exclude-check",
    "excluded_checks",
    multiple=True,
    help="Diagnostic KIND:NAME stream deliberately outside the lane figure.",
)
def health_history_summary(
    pages: tuple[Path, ...],
    checks: tuple[str, ...],
    identity_checks: tuple[str, ...],
    excluded_checks: tuple[str, ...],
) -> None:
    """Account for one lane's saved GET health-history pages, in cursor order."""
    from control_plane.lane_health_history import LaneHealthHistoryResponse, summarize_history_pages
    from control_plane.contracts.public_ingress_monitoring import PublicIngressCheckKind
    from pydantic import TypeAdapter

    try:
        history = [
            LaneHealthHistoryResponse.model_validate_json(page.read_bytes()).history
            for page in pages
        ]
        adapter = TypeAdapter(tuple[tuple[PublicIngressCheckKind, str], ...])
        result = summarize_history_pages(
            history,
            required_checks=adapter.validate_python([check.split(":", 1) for check in checks]),
            identity_checks=adapter.validate_python(
                [check.split(":", 1) for check in identity_checks]
            ),
            excluded_checks=adapter.validate_python(
                [check.split(":", 1) for check in excluded_checks]
            ),
        )
    except (ValueError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(result.model_dump_json(indent=2))


@ingress.command("private-testing-plan")
@click.option("--service-url", required=True)
@click.option("--bearer-token-env", default="LAUNCHPLANE_SERVICE_TOKEN", show_default=True)
@click.option("--product", required=True)
@click.option("--payload-file", required=True, type=click.Path(exists=True, path_type=Path))
def private_testing_plan(
    service_url: str,
    bearer_token_env: str,
    product: str,
    payload_file: Path,
) -> None:
    """Plan/read back private testing TLS through the service; never apply."""
    from control_plane.private_testing_transport import (
        ROUTE,
        PRODUCT_PATTERN,
        PrivateTestingTransportRequest,
    )

    if not service_url.startswith("https://"):
        raise click.ClickException("Private transport planning requires the HTTPS service origin.")
    if not re.fullmatch(PRODUCT_PATTERN, product):
        raise click.ClickException("Product must be a canonical product identifier.")
    try:
        request = PrivateTestingTransportRequest.model_validate_json(payload_file.read_bytes())
    except (ValueError, OSError) as error:
        raise click.ClickException(
            "Private transport payload is invalid; no request sent."
        ) from error
    token = os.environ.get(bearer_token_env, "").strip()
    if not token:
        raise click.ClickException(f"{bearer_token_env} is required.")
    result = _post_launchplane_service_json(
        service_url=service_url,
        path=ROUTE.format(product=product),
        payload=request.model_dump(mode="json"),
        bearer_token=token,
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@ingress.command("private-testing-activate")
@click.option("--service-url", required=True)
@click.option("--bearer-token-env", default="LAUNCHPLANE_SERVICE_TOKEN", show_default=True)
@click.option("--product", required=True)
@click.option("--payload-file", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--idempotency-key", default="")
def private_testing_activate(
    service_url: str, bearer_token_env: str, product: str, payload_file: Path, idempotency_key: str
) -> None:
    """Install/remove reviewed testing source through the service; never deploy."""
    from control_plane.private_testing_activation import ROUTE, PrivateTestingActivationRequest
    from control_plane.private_testing_transport import PRODUCT_PATTERN

    if not service_url.startswith("https://") or not re.fullmatch(PRODUCT_PATTERN, product):
        raise click.ClickException("Use the HTTPS service origin and canonical product.")
    try:
        request = PrivateTestingActivationRequest.model_validate_json(payload_file.read_bytes())
    except (ValueError, OSError) as error:
        raise click.ClickException(
            "Private activation payload is invalid; no request sent."
        ) from error
    if request.mode != "dry-run" and (
        not idempotency_key.strip() or not request.expected_plan_sha256
    ):
        raise click.ClickException("Use the original operation key and reviewed digest.")
    token = os.environ.get(bearer_token_env, "").strip()
    if not token:
        raise click.ClickException(f"{bearer_token_env} is required.")
    result = _post_launchplane_service_json(
        service_url=service_url,
        path=ROUTE.format(product=product),
        payload=request.model_dump(mode="json"),
        bearer_token=token,
        idempotency_key=idempotency_key,
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@ingress.command("route-apply")
@click.option(
    "--service-url",
    required=True,
    help="Deployed Launchplane service base URL. Ingress mutations go through the service API.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a Launchplane bearer token.",
)
@click.option(
    "--session-cookie",
    default="",
    help="Launchplane browser session cookie. Use instead of --bearer-token-env.",
)
@click.option("--product", required=True, help="Launchplane product for ingress authz.")
@click.option("--context", required=True, help="Launchplane context for ingress authz.")
@click.option(
    "--domain",
    "domains",
    multiple=True,
    required=True,
    help="Domain name for the proxy host. Repeat for aliases.",
)
@click.option(
    "--forward-scheme",
    type=click.Choice(["http", "https", "path", "empty", "grpc", "grpcs"]),
    default="http",
    show_default=True,
)
@click.option("--forward-host", required=True, help="Upstream host or address.")
@click.option("--forward-port", type=int, default=None, help="Upstream port.")
@click.option(
    "--certificate-id",
    default="0",
    show_default=True,
    help="NPMplus certificate ID or 'new'.",
)
@click.option("--access-list-id", type=int, default=0, show_default=True)
@click.option("--expected-host-id", type=int, default=None)
@click.option("--disable", "enabled", flag_value=False, default=True)
@click.option("--enable", "enabled", flag_value=True)
@click.option("--no-ssl-forced", "ssl_forced", flag_value=False, default=True)
@click.option("--ssl-forced", "ssl_forced", flag_value=True)
@click.option("--no-http2", "http2_support", flag_value=False, default=True)
@click.option("--http2", "http2_support", flag_value=True)
@click.option("--no-http3", "http3_support", flag_value=False, default=True)
@click.option("--http3", "http3_support", flag_value=True)
@click.option("--noindex", "npmplus_noindex", is_flag=True, default=False)
@click.option(
    "--auth-request",
    type=click.Choice(
        [
            "none",
            "anubis",
            "tinyauth",
            "authelia",
            "authentik",
            "authentik-send-basic-auth",
        ]
    ),
    default="none",
    show_default=True,
)
@click.option(
    "--identity-access-provider",
    type=click.Choice(["none", "anubis", "tinyauth", "authelia", "authentik"]),
    default="none",
    show_default=True,
    help="Provider-neutral identity/access binding provider.",
)
@click.option(
    "--identity-access-send-basic-auth",
    is_flag=True,
    default=False,
    help="Use the Authentik basic-auth forwarding mode for the identity/access binding.",
)
@click.option(
    "--advanced-config",
    default=None,
    help="NPMplus advanced nginx config. Omit to preserve an existing host value.",
)
@click.option("--allow-create/--no-allow-create", default=True, show_default=True)
@click.option("--allow-update/--no-allow-update", default=True, show_default=True)
@click.option("--allow-enable-disable/--no-allow-enable-disable", default=True, show_default=True)
@click.option("--reason", required=True, help="Audit reason for the ingress request.")
@click.option("--idempotency-key", default="")
@click.option("--dry-run", "mode", flag_value="dry-run", default="dry-run")
@click.option("--apply", "mode", flag_value="apply")
def ingress_route_apply(
    service_url: str,
    bearer_token_env: str,
    session_cookie: str,
    product: str,
    context: str,
    domains: tuple[str, ...],
    forward_scheme: str,
    forward_host: str,
    forward_port: int | None,
    certificate_id: str,
    access_list_id: int,
    expected_host_id: int | None,
    enabled: bool,
    ssl_forced: bool,
    http2_support: bool,
    http3_support: bool,
    npmplus_noindex: bool,
    auth_request: str,
    identity_access_provider: str,
    identity_access_send_basic_auth: bool,
    advanced_config: str | None,
    allow_create: bool,
    allow_update: bool,
    allow_enable_disable: bool,
    reason: str,
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
    if mode == "apply" and not idempotency_key.strip():
        raise click.ClickException("--apply requires --idempotency-key.")
    payload = _route_apply_payload(
        product=product,
        context=context,
        domains=domains,
        forward_scheme=forward_scheme,
        forward_host=forward_host,
        forward_port=forward_port,
        certificate_id=certificate_id,
        access_list_id=access_list_id,
        expected_host_id=expected_host_id,
        enabled=enabled,
        ssl_forced=ssl_forced,
        http2_support=http2_support,
        http3_support=http3_support,
        npmplus_noindex=npmplus_noindex,
        auth_request=auth_request,
        identity_access_provider=identity_access_provider,
        identity_access_send_basic_auth=identity_access_send_basic_auth,
        advanced_config=advanced_config,
        allow_create=allow_create,
        allow_update=allow_update,
        allow_enable_disable=allow_enable_disable,
        reason=reason,
        mode=mode,
    )
    response_payload = _post_launchplane_service_json(
        service_url=service_url,
        path=INGRESS_ROUTE_APPLY_ROUTE,
        payload=payload,
        bearer_token=bearer_token,
        session_cookie=session_cookie,
        idempotency_key=idempotency_key,
    )
    click.echo(json.dumps(response_payload, indent=2, sort_keys=True))


def _route_apply_payload(
    *,
    product: str,
    context: str,
    domains: tuple[str, ...],
    forward_scheme: str,
    forward_host: str,
    forward_port: int | None,
    certificate_id: str,
    access_list_id: int,
    expected_host_id: int | None,
    enabled: bool,
    ssl_forced: bool,
    http2_support: bool,
    http3_support: bool,
    npmplus_noindex: bool,
    auth_request: str,
    identity_access_provider: str,
    identity_access_send_basic_auth: bool,
    advanced_config: str | None,
    allow_create: bool,
    allow_update: bool,
    allow_enable_disable: bool,
    reason: str,
    mode: str,
) -> dict[str, object]:
    resolved_certificate_id: int | str
    if certificate_id.strip() == "new":
        resolved_certificate_id = "new"
    else:
        try:
            resolved_certificate_id = int(certificate_id)
        except ValueError as error:
            raise click.ClickException("--certificate-id must be an integer or 'new'.") from error
    identity_access = _identity_access_binding(
        provider=identity_access_provider,
        send_basic_auth=identity_access_send_basic_auth,
    )
    if identity_access and auth_request not in {
        "none",
        _identity_access_auth_request(identity_access),
    }:
        raise click.ClickException(
            "--identity-access-provider conflicts with --auth-request. "
            "Use one provider mode or matching values."
        )
    route: dict[str, object] = {
        "domain_names": list(domains),
        "forward_scheme": forward_scheme,
        "forward_host": forward_host,
        "certificate_id": resolved_certificate_id,
        "ssl_forced": ssl_forced,
        "http2_support": http2_support,
        "npmplus_http3_support": http3_support,
        "access_list_id": access_list_id,
        "npmplus_noindex": npmplus_noindex,
        "npmplus_auth_request": auth_request,
        "enabled": enabled,
    }
    if advanced_config is not None:
        route["advanced_config"] = advanced_config
    if identity_access:
        route["identity_access"] = identity_access
    if forward_port is not None:
        route["forward_port"] = forward_port
    ingress_request: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "route": route,
        "allow_create": allow_create,
        "allow_update": allow_update,
        "allow_enable_disable": allow_enable_disable,
        "reason": reason,
    }
    if expected_host_id is not None:
        ingress_request["expected_host_id"] = expected_host_id
    return {
        "schema_version": 1,
        "product": product,
        "context": context,
        "ingress": ingress_request,
    }


def _identity_access_binding(*, provider: str, send_basic_auth: bool) -> dict[str, object] | None:
    if provider == "none":
        if send_basic_auth:
            raise click.ClickException(
                "--identity-access-send-basic-auth requires --identity-access-provider authentik."
            )
        return None
    if send_basic_auth and provider != "authentik":
        raise click.ClickException(
            "--identity-access-send-basic-auth is only valid with --identity-access-provider authentik."
        )
    return {
        "schema_version": 1,
        "mode": "forward-auth",
        "provider": provider,
        "send_basic_auth": send_basic_auth,
    }


def _identity_access_auth_request(identity_access: dict[str, object]) -> str:
    provider = identity_access["provider"]
    if provider == "authentik" and identity_access["send_basic_auth"] is True:
        return "authentik-send-basic-auth"
    if isinstance(provider, str):
        return provider
    raise click.ClickException("Invalid identity/access provider.")
