import json
import os
from collections.abc import Callable
from dataclasses import dataclass

import click


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
@click.option("--advanced-config", default="")
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
    advanced_config: str,
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
        path="/v1/drivers/ingress/route-apply",
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
    advanced_config: str,
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
        "advanced_config": advanced_config,
        "enabled": enabled,
    }
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
