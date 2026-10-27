import json
from pathlib import Path

import click

from control_plane.local_consumer import deliver_consumer, prepare_consumer, receive_consumer
from control_plane.local_consumer_contract import digest


@click.group("local-consumer", help="Deliver one isolated testing bridge credential privately.")
def local_consumer() -> None:
    pass


@local_consumer.command("prepare", help="Prepare a private testing recipient and request.")
@click.option("--directory", type=click.Path(path_type=Path), required=True)
@click.option("--service-origin", required=True)
@click.option("--product", required=True)
@click.option("--context", required=True)
@click.option("--endpoint-key", required=True)
@click.option("--server-url", required=True)
def prepare(
    directory: Path,
    service_origin: str,
    product: str,
    context: str,
    endpoint_key: str,
    server_url: str,
) -> None:
    try:
        request = prepare_consumer(
            directory=directory,
            service_origin=service_origin,
            product=product,
            context=context,
            endpoint_key=endpoint_key,
            server_url=server_url,
        )
    except Exception:
        raise click.ClickException(
            "Preparation incomplete. Choose a new directory under an existing private parent; "
            "verify the HTTPS service and WSS testing destination. No existing client was replaced."
        ) from None
    click.echo(
        json.dumps(
            {
                "request_file": str(Path(request.directory) / "request.json"),
                "fingerprint": digest(request),
            }
        )
    )


@local_consumer.command("deliver", help="Review or deliver a recipient-encrypted testing package.")
@click.option("--directory", type=click.Path(path_type=Path), required=True)
@click.option("--service-url", envvar="LAUNCHPLANE_SERVICE_URL", required=True)
@click.option("--bearer-token-env", default="LAUNCHPLANE_SERVICE_TOKEN", show_default=True)
@click.option("--reviewed-plan-digest", default="")
def deliver(
    directory: Path, service_url: str, bearer_token_env: str, reviewed_plan_digest: str
) -> None:
    try:
        result = deliver_consumer(
            directory,
            service_url=service_url,
            bearer_token_env=bearer_token_env,
            reviewed_plan_digest=reviewed_plan_digest,
        )
    except Exception:
        raise click.ClickException(
            "Delivery incomplete. Verify testing credential readiness, transport, existing "
            "exact-lane authority and the original review. Keep the original directory and "
            "retry only the same request."
        ) from None
    click.echo(json.dumps(result))


@local_consumer.command(
    "receive", help="Verify and install the original encrypted testing package."
)
@click.option("--directory", type=click.Path(path_type=Path), required=True)
@click.option("--package-file", type=click.Path(path_type=Path), required=True)
def receive(directory: Path, package_file: Path) -> None:
    try:
        result = receive_consumer(directory, package_file)
    except Exception:
        raise click.ClickException(
            "Delivery incomplete. Keep the original request/package for retry. Verify testing "
            "server sync, identity and receipt; changed or expired reviews need a new directory."
        ) from None
    click.echo(json.dumps(result))


def register_local_consumer_commands(root: click.Group) -> None:
    root.add_command(local_consumer)
