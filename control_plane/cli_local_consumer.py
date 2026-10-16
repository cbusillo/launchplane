import json
from pathlib import Path

import click

from control_plane.local_consumer import prepare_consumer, receive_consumer
from control_plane.local_consumer_contract import digest


@click.group("local-consumer")
def local_consumer() -> None:
    """Owner-run testing bridge delivery. Never prints credential values."""


@local_consumer.command("prepare")
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
    """Create a new private destination and a non-secret recipient request."""
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


@local_consumer.command("receive")
@click.option("--directory", type=click.Path(path_type=Path), required=True)
@click.option("--package-file", type=click.Path(path_type=Path), required=True)
def receive(directory: Path, package_file: Path) -> None:
    """Verify and install an encrypted delivery; retry only the original package."""
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
