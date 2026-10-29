"""Complete, bounded compose inventory for preview absence and original-key recovery."""

import click

from control_plane.dokploy import api as dokploy_api


def complete_preview_compose_inventory(
    *, host: str, token: str
) -> tuple[tuple[str, str, str], ...]:
    projects = dokploy_api.dokploy_request(host=host, token=token, path="/api/project.all")
    if not isinstance(projects, list) or len(projects) > 1000:
        raise click.ClickException("Original preview inventory is incomplete.")
    entries: list[tuple[str, str, str]] = []
    environments_seen: set[str] = set()
    composes_seen: set[str] = set()
    for project in projects:
        if not isinstance(project, dict):
            raise click.ClickException("Original preview inventory is incomplete.")
        environments = project.get("environments")
        if not isinstance(environments, list):
            raise click.ClickException("Original preview inventory is incomplete.")
        for environment in environments:
            if not isinstance(environment, dict):
                raise click.ClickException("Original preview inventory is incomplete.")
            environment_id = environment.get("environmentId")
            if (
                not isinstance(environment_id, str)
                or not environment_id.strip()
                or environment_id in environments_seen
                or len(environments_seen) >= 10000
            ):
                raise click.ClickException("Original preview environment inventory is ambiguous.")
            environments_seen.add(environment_id)
            collections = [
                environment[key] for key in ("compose", "composes") if key in environment
            ]
            composes = collections[0] if len(collections) == 1 else None
            if not isinstance(composes, list):
                raise click.ClickException("Original preview compose inventory is incomplete.")
            for compose in composes:
                if not isinstance(compose, dict):
                    raise click.ClickException("Original preview compose inventory is incomplete.")
                name, identifier = compose.get("name"), compose.get("composeId")
                if (
                    not isinstance(name, str)
                    or not name.strip()
                    or not isinstance(identifier, str)
                    or not identifier.strip()
                    or identifier in composes_seen
                    or len(entries) >= 10000
                ):
                    raise click.ClickException("Original preview compose inventory is incomplete.")
                composes_seen.add(identifier)
                entries.append((environment_id, identifier, name))
    return tuple(sorted(entries))
