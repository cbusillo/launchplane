from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
import time

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.testing_address import TestingAddressPlan, address_binding
from control_plane.dokploy import api, source
from control_plane.dokploy.testing_route import (
    amend_testing_route_address,
    observe_testing_route,
    possible_lane_routers,
)
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.dokploy_inventory import (
    read_dokploy_inventory,
    InventoryUnavailable,
    InventoryChanged,
)
from control_plane.dokploy.native_copy_stream import (
    NativeSourceAuthorizationDenied,
    require_registered_remote_server,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_publication_storage import read_snapshot
from control_plane.testing_serving_copy import published_testing_copy


def qualify_address_domains(
    *, host: str, token: str, hostname: str, retained_ids: frozenset[str], server_id: str
) -> tuple[str, tuple[str, ...]]:
    try:
        return _qualify_address_domains(
            host=host,
            token=token,
            hostname=hostname,
            retained_ids=retained_ids,
            server_id=server_id,
        )
    except (InventoryUnavailable, InventoryChanged, api.DokployRequestFailed) as error:
        cause: BaseException | None = error
        while cause is not None:
            if isinstance(cause, api.DokployRequestFailed) and cause.status_code in {401, 403}:
                raise NativeSourceAuthorizationDenied(
                    "The existing provider identity denied the testing hostname recipient read."
                ) from error
            cause = cause.__cause__
        raise ValueError(
            "The testing hostname recipient inventory could not be qualified."
        ) from error


def _qualify_address_domains(
    *, host: str, token: str, hostname: str, retained_ids: frozenset[str], server_id: str
) -> tuple[str, tuple[str, ...]]:
    offset, digest = 0, ""
    started = time.monotonic()
    rows: list[dict[str, object]] = []
    projects: set[str] = set()
    seen: set[str] = set()
    while True:
        page = read_dokploy_inventory(
            host=host, token=token, offset=offset, limit=1, inventory_digest=digest
        )
        digest = page.inventory_digest
        if page.total_targets > 500:
            raise ValueError("The testing hostname recipient census exceeds its bounded budget.")
        for project in page.projects:
            for environment in project.environments:
                for target in environment.targets:
                    if time.monotonic() - started > 120:
                        raise ValueError(
                            "The testing hostname census exceeded its observation window."
                        )
                    if target.category not in {"compose", "application"}:
                        continue
                    actual = api.fetch_dokploy_target_payload(
                        host=host,
                        token=token,
                        target_type=target.category,
                        target_id=target.target_id,
                    )
                    if (
                        actual.get(target.category + "Id") != target.target_id
                        or actual.get("serverId") != target.placement.server_id
                    ):
                        raise ValueError(
                            "A hostname recipient changed placement during its census."
                        )
                    domains = actual.get("domains")
                    if not isinstance(domains, list) or any(
                        not isinstance(item, dict) or not isinstance(item.get("host"), str)
                        for item in domains
                    ):
                        raise ValueError("A hostname recipient has an incomplete domain inventory.")
                    matches = []
                    for item in domains:
                        if not isinstance(item, dict) or not isinstance(item.get("host"), str):
                            raise ValueError("A hostname domain entry is unavailable.")
                        domain_host = item["host"]
                        if (
                            isinstance(domain_host, str)
                            and domain_host.casefold() == hostname.casefold()
                        ):
                            matches.append(item)
                    if matches and target.target_id not in retained_ids:
                        raise ValueError(
                            "The requested testing hostname is declared by another recipient."
                        )
                    if target.target_id in retained_ids:
                        if (
                            target.category != "compose"
                            or actual.get("serverId") != server_id
                            or len(matches) != 1
                            or not actual.get("appName")
                        ):
                            raise ValueError("The original retained hostname recipient changed.")
                        if actual.get("autoDeploy") is not False or api.list_dokploy_schedules(
                            host=host,
                            token=token,
                            target_id=target.target_id,
                            schedule_type="compose",
                        ):
                            raise ValueError(
                                "The original retained recipient can still deploy automatically."
                            )
                        seen.add(target.target_id)
                        projects.add(str(actual["appName"]))
                        rows.append(
                            {
                                "id": target.target_id,
                                "server": actual.get("serverId"),
                                "project": actual["appName"],
                                "matches": matches,
                            }
                        )
        if page.next_offset is None:
            break
        offset = page.next_offset
    check = read_dokploy_inventory(
        host=host, token=token, offset=0, limit=1, inventory_digest=digest
    )
    if check.inventory_digest != digest or seen != retained_ids:
        raise ValueError("The hostname recipient inventory is incomplete or changed.")
    return canonical_json_sha256(sorted(rows, key=lambda item: str(item["id"]))), tuple(
        sorted(projects)
    )


def qualify_retained_address_domains(
    *, host: str, token: str, hostname: str, retained_ids: frozenset[str], server_id: str
) -> tuple[str, tuple[str, ...]]:
    rows = []
    projects = set()
    for target_id in sorted(retained_ids):
        actual = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target_id
        )
        domains = actual.get("domains")
        if not isinstance(domains, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("host"), str) for item in domains
        ):
            raise ValueError("The retained hostname domain inventory is incomplete.")
        matches = [
            item
            for item in domains
            if isinstance(item, dict)
            and isinstance(item.get("host"), str)
            and str(item["host"]).casefold() == hostname.casefold()
        ]
        if (
            actual.get("composeId") != target_id
            or actual.get("serverId") != server_id
            or not actual.get("appName")
            or len(matches) != 1
            or actual.get("autoDeploy") is not False
            or api.list_dokploy_schedules(
                host=host, token=token, target_id=target_id, schedule_type="compose"
            )
        ):
            raise ValueError("The retained hostname recipient changed before dispatch.")
        projects.add(str(actual["appName"]))
        rows.append(
            {
                "id": target_id,
                "server": actual.get("serverId"),
                "project": actual["appName"],
                "matches": matches,
            }
        )
    return canonical_json_sha256(rows), tuple(sorted(projects))


def address_transport(
    root: Path, store: PostgresRecordStore, plan: TestingAddressPlan, *, recovery: bool = False
) -> DokployTestingRouteTransport:
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    identity = plan.before.inventory.runtime_identity if plan.before.inventory else None
    if recovery:
        from control_plane.testing_address_storage import read_testing_address_recovery_copy

        current = read_testing_address_recovery_copy(store, plan)
        identity = current.inventory.runtime_identity if current.inventory else None
    if identity is None:
        raise ValueError("The current testing copy requires its runtime identity.")
    return DokployTestingRouteTransport(
        host=host,
        token=token,
        server_id=plan.server_id,
        old_project=plan.provider_project,
        product=plan.product,
        context=plan.context,
        public_host=plan.route_after.host,
        public_aliases=plan.route_after.aliases,
        entrypoints=plan.route_after.public_entrypoints,
        old_runtime_identity=identity,
        address_recipients=None
        if recovery
        else {
            "hostname": plan.route_after.host,
            "projects": plan.retained_projects,
            "entrypoints": plan.route_after.public_entrypoints,
            "priority": min(
                item["priority"] for item in plan.route_after.template["http"]["routers"].values()
            ),
            "expected_sha256": plan.recipient_sha256,
        },
    )


@contextmanager
def exclude_address_executors(
    store: PostgresRecordStore, operations: tuple[str, ...]
) -> Iterator[None]:
    with ExitStack() as stack:
        for operation in sorted(set(operations)):
            check = stack.enter_context(store.testing_replacement_execution_lock(operation))
            if check is None:
                raise ValueError("An original testing executor cannot be excluded.")
            check()
        yield


def plan_testing_address(
    root: Path,
    store: PostgresRecordStore,
    *,
    product: str,
    context: str,
    retirement_operation_id: str,
    reservation_operation_id: str,
    hostname: str,
    reason: str,
) -> TestingAddressPlan:
    retained = store.read_retained_testing_generation(retirement_operation_id)
    if retained is None or (retained.plan.product, retained.plan.context) != (product, context):
        raise ValueError("The original retirement inspection is unavailable.")
    publication = published_testing_copy(store, product=product, context=context)
    if publication is None:
        raise ValueError("Canonical address amendment requires its published native testing copy.")
    profile = store.read_product_profile_record(product)
    before = read_snapshot(store, product=product, context=context)
    if before.target.policies.staff_testing_hold is not None or before.binding is None:
        raise ValueError("The testing copy is held or has no route binding.")
    with store._session_factory() as session:
        reservations = tuple(
            item
            for item in store._retained_disposable_hostname_reservations(session)
            if item.hostname == hostname
        )
    if not reservations or any(
        (item.product, item.context, item.operation_id)
        != (product, context, reservation_operation_id)
        or not item.target_id
        for item in reservations
    ):
        raise ValueError("The hostname needs exact retained reservation custody in this lane.")
    reservation = store.read_odoo_stable_target_replacement_operation_record(
        reservation_operation_id
    )
    if reservation.status != "superseded":
        raise ValueError("The hostname recipient's original operation has not been superseded.")
    operations = {
        key: canonical_json_sha256(
            store._payload_dict(store.read_odoo_stable_target_replacement_operation_record(key))
        )
        for key in {retirement_operation_id, reservation_operation_id}
    }
    with exclude_address_executors(store, tuple(operations)):
        host, token = source.read_dokploy_config(
            control_plane_root=root, database_url=store.database_url
        )
        try:
            current = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=before.target_id.target_id
            )
        except api.DokployRequestFailed as error:
            if error.status_code in {401, 403}:
                raise NativeSourceAuthorizationDenied(
                    "The existing provider identity denied the current testing target read."
                ) from error
            raise
        if (
            current.get("composeId") != before.target_id.target_id
            or current.get("name") != before.target.target_name
            or current.get("domains") != []
            or not current.get("appName")
            or current.get("serverId") != publication.plan.candidate.server_id
            or not isinstance(current.get("serverId"), str)
            or not current["serverId"]
        ):
            raise ValueError(
                "The published testing target changed its domainless managed placement."
            )
        server_id = require_registered_remote_server(current["serverId"])
        inventory, projects = qualify_address_domains(
            host=host,
            token=token,
            hostname=hostname,
            retained_ids=frozenset(str(item.target_id) for item in reservations),
            server_id=server_id,
        )
        route = amend_testing_route_address(publication.plan.route, hostname)
        identity = before.inventory.runtime_identity if before.inventory else None
        if identity is None:
            raise ValueError("The testing runtime identity is unavailable.")
        transport = DokployTestingRouteTransport(
            host=host,
            token=token,
            server_id=server_id,
            old_project=str(current["appName"]),
            product=product,
            context=context,
            public_host=hostname,
            public_aliases=route.aliases,
            entrypoints=route.public_entrypoints,
            old_runtime_identity=identity,
            address_recipients={
                "hostname": hostname,
                "projects": projects,
                "entrypoints": route.public_entrypoints,
                "priority": min(
                    item["priority"] for item in route.template["http"]["routers"].values()
                ),
            },
        )
        frame = transport.read()
        import json

        if (
            frame["owned_file"] is None
            or possible_lane_routers(frame["raw"], hostname, route.public_entrypoints)
            or observe_testing_route(
                publication.plan.route, frame["raw"], json.loads(frame["owned_file"])
            )
            != route.new_backend
            or route.new_backend not in frame["old_endpoints"]
        ):
            raise ValueError("The original testing route or new canonical hostname is unverified.")
        return TestingAddressPlan(
            product=product,
            context=context,
            retirement_operation_id=retirement_operation_id,
            reason=reason,
            publication_key=publication.plan.key,
            publication_sha256=canonical_json_sha256(publication.model_dump(mode="json")),
            profile=profile,
            before=before,
            route_before=publication.plan.route,
            route_after=route,
            binding_after=address_binding(before.binding, route),
            reservations=reservations,
            server_id=server_id,
            provider_project=str(current["appName"]),
            retained_projects=projects,
            recipient_sha256=frame["address_recipients_sha256"],
            domain_inventory_sha256=inventory,
            operation_sha256=operations,
            retirement_sha256=canonical_json_sha256(store._payload_dict(retained)),
            created_at=datetime.now(UTC),
        )
