import inspect


def network_members_are_owned(
    network_id: str,
    members: set[str],
    attachments: dict[str, set[str]],
    owned: set[str],
) -> bool:
    attached = {
        identifier for identifier, networks in attachments.items() if network_id in networks
    }
    return bool(network_id) and members <= attached == owned


def docker_network_members_are_owned(
    network: dict[str, object], containers: list[dict[str, object]], owned: set[str]
) -> bool:
    network_id = network.get("Id")
    members = network.get("Containers")
    if not isinstance(network_id, str) or not isinstance(members, dict):
        return False
    member_ids = {identifier for identifier in members if isinstance(identifier, str)}
    if len(member_ids) != len(members):
        return False
    attachments: dict[str, set[str]] = {}
    for container in containers:
        identifier = container.get("Id")
        settings = container.get("NetworkSettings")
        if not isinstance(identifier, str) or not identifier or identifier in attachments:
            return False
        if not isinstance(settings, dict) or not isinstance(settings.get("Networks"), dict):
            return False
        network_ids: set[str] = set()
        for attachment in settings["Networks"].values():
            if not isinstance(attachment, dict):
                return False
            attached_id = attachment.get("NetworkID")
            if not isinstance(attached_id, str) or not attached_id:
                return False
            network_ids.add(attached_id)
        attachments[identifier] = network_ids
    return network_members_are_owned(network_id, member_ids, attachments, owned)


COPY_NETWORK_MEMBERSHIP_LIBRARY = (
    inspect.getsource(network_members_are_owned)
    + "\n"
    + inspect.getsource(docker_network_members_are_owned)
)
