"""Conservative lifetime non-use from service-owned generation records."""

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.disposable_lane import DisposableTestingResourcePreparation
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration, generation_binding
from control_plane.odoo_testing_usage import OdooTestingUsage

NON_USE_EVIDENCE_PREFIX = "service:odoo-testing-never-served:"


@dataclass(frozen=True)
class NeverServedRecords:
    generation: FreshOdooTestingGeneration
    allocation: DisposableTestingResourcePreparation
    bootstrap_operation: OdooStableTargetReplacementOperationRecord
    target: DokployTargetRecord
    deployments: tuple[DeploymentRecord, ...]
    checks: tuple[PublicIngressObservationRecord, ...]
    protected_use: bool
    inventory: EnvironmentInventory | None = None
    private_checks: tuple[tuple[str, str], ...] = ()


def qualify_never_served(records: NeverServedRecords) -> OdooTestingUsage:
    """The caller supplies complete DB histories, never an HTTP assertion."""
    unknown = OdooTestingUsage(reason_code="never_served_records_incomplete")
    gen, allocation, operation = (
        records.generation,
        records.allocation,
        records.bootstrap_operation,
    )
    try:
        prepared = datetime.fromisoformat(gen.prepared_at.replace("Z", "+00:00"))
        if prepared.utcoffset() is None:
            return unknown
        if (
            gen.instance != "testing"
            or gen.non_use_tracking_started_at != gen.prepared_at
            or not gen.non_use_tracking_started_at
            or gen.non_use_invalidated
            or gen.completed_at
            or gen.drill_database
            or (gen.product, gen.context, gen.instance)
            != (operation.product, operation.context, operation.instance)
            or operation.operation_id != gen.operation_id
            or allocation.replacement_target_id != gen.target_id
            or allocation.resource_namespace != gen.namespace
            or allocation.resources_binding_sha256 != gen.resources_binding_sha256
            or allocation.retained_target_id == allocation.replacement_target_id
            or records.target.instance != "testing"
            or records.target.context != gen.context
        ):
            return unknown
        if records.target.policies.staff_testing_hold is not None or records.protected_use:
            return OdooTestingUsage(reason_code="never_served_protection_seen")
        if (
            records.inventory is not None
            and datetime.fromisoformat(records.inventory.updated_at.replace("Z", "+00:00"))
            >= prepared
        ):
            return unknown
        # Any deployment since creation could have served, including failed final
        # health checks and an uncertain write. Do not count only passing deploys.
        for deployment in records.deployments:
            started = deployment.deploy.started_at
            if not started:
                return unknown
            if datetime.fromisoformat(started.replace("Z", "+00:00")) >= prepared:
                return unknown
        kinds: set[str] = set()
        for check in records.checks:
            at = datetime.fromisoformat(check.observed_at.replace("Z", "+00:00"))
            if at < prepared:
                continue
            if (check.product, check.context, check.instance) != (
                gen.product,
                gen.context,
                gen.instance,
            ):
                return unknown
            if check.check_kind not in {"public_http", "private_http"}:
                continue
            bound = (
                urlsplit(check.base_url).hostname == gen.domain_host
                and (not check.health_url or urlsplit(check.health_url).hostname == gen.domain_host)
                and all(urlsplit(t.url).hostname == gen.domain_host for t in check.targets)
                if check.check_kind == "public_http"
                else (check.check_name, check.health_url) in records.private_checks
                and all(
                    t.target == "private_health_url" and t.url == check.health_url
                    for t in check.targets
                )
            )
            if not bound:
                # A failed pre-route check says nothing about this generation.
                if check.status == "fail" and all(t.status == "fail" for t in check.targets):
                    continue
                return unknown
            # A mixed result can contain a successful public/private target even
            # when the aggregate check failed. It also defeats lifetime non-use.
            if check.status != "fail" or any(t.status != "fail" for t in check.targets):
                return unknown
            kinds.add(check.check_kind)
        if kinds != {"public_http", "private_http"}:
            return unknown
    except (ValueError, TypeError):
        return unknown
    return OdooTestingUsage(
        usage="unused",
        reason_code="never_served",
        # These remain null: no session/login read is claimed by this proof.
        proof_sha256=canonical_json_sha256({"generation": generation_binding(gen)}),
    )


def read_never_served_testing_usage(
    store: object, *, product: str, context: str, target_id: str
) -> OdooTestingUsage:
    try:
        reader = getattr(store, "read_testing_non_use_records", None)
        records = reader(product, context, target_id) if callable(reader) else None
    except (ValueError, TypeError, FileNotFoundError):
        records = None
    return (
        qualify_never_served(records)
        if records is not None
        else OdooTestingUsage(reason_code="never_served_records_incomplete")
    )
