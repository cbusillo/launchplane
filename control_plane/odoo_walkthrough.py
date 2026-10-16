"""Testing-only account custody. All external effects remain inside the service."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets as random_secrets
import time
from urllib.parse import urlsplit

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_walkthrough import OdooWalkthroughRequest, OdooWalkthroughResult
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.dokploy.odoo_walkthrough import testing_account_command
from control_plane.odoo_runtime_reads import (
    OdooRuntimeConnection,
    assert_selection_current,
    connect_runtime,
    select_stable_runtime,
)
from control_plane.odoo_testing_drill import require_drill_use_unprotected
from control_plane.odoo_testing_drill_usage import (
    read_drill_recent_signins,
    testing_drill_environment,
)
from control_plane.odoo_testing_restore import generation_binding, read_current_testing_generation
from control_plane.odoo_walkthrough_browser import run_walkthrough_browser
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
)
from control_plane.secrets import _decrypt_secret_value, write_secret_value
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.odoo_preview_runtime import odoo_compose_has_domain

WALKTHROUGH_SECRET_INTEGRATION = "odoo_walkthrough"


@dataclass(frozen=True)
class WalkthroughPlan:
    digest: str
    binding: str
    database: str
    copy_binding: str
    usage_since: str
    secret_name: str
    connection: OdooRuntimeConnection = field(repr=False)


def plan_walkthrough(
    store: PostgresRecordStore, root: Path, request: OdooWalkthroughRequest, actor: str
) -> WalkthroughPlan:
    # This check precedes profile, provider and secret resolution. Even trusted
    # direct callers cannot turn this into a production account operation.
    if request.instance != "testing":
        raise ValueError("Only testing lanes support task accounts.")
    profile = store.read_product_profile_record(request.product)
    if not profile.is_active:
        raise ValueError("The product is not active.")
    selection = select_stable_runtime(store, profile, "testing")
    if selection.identity.context != request.context:
        raise ValueError("The product does not own this testing context.")
    parsed = urlsplit(selection.base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Testing sign-in requires its recorded HTTPS origin.")
    if any(
        lane.instance != "testing"
        and (urlsplit(lane.base_url).hostname or "").rstrip(".") == parsed.hostname.rstrip(".")
        for product in store.list_product_profile_records()
        for lane in product.lanes
    ):
        raise ValueError("Testing origin aliases another lane.")
    generation = read_current_testing_generation(
        store, product=request.product, context=request.context, instance="testing"
    )
    if generation is None or not generation.completed_at:
        raise ValueError("A completed isolated testing generation is required.")
    require_drill_use_unprotected(store, request.product, request.context)
    # This performs the existing real host storage/network/image/copy-witness
    # inspection. Its recent-use boolean is deliberately NOT a walkthrough
    # permission: the account program checks other users independently.
    read_drill_recent_signins(store, generation, time.time())
    connection = connect_runtime(
        store=store, selection=selection, control_plane_root=root, database_url=store.database_url
    )
    if connection.evidence.target_id != generation.target_id:
        raise ValueError("Testing recipient changed.")
    if not odoo_compose_has_domain(
        host=connection.host,
        token=connection.token,
        compose_id=connection.evidence.target_id,
        domain_host=parsed.hostname,
    ):
        raise ValueError("The origin does not belong to this testing target.")
    database = testing_drill_environment(store, request.context).get("ODOO_DB_NAME", "")
    if not database or database != connection.credentials.get("ODOO_DB_NAME"):
        raise ValueError("Testing database is unavailable or changed.")
    copy_binding = generation_binding(generation)
    binding = canonical_json_sha256(
        [request.product, request.context, request.task_id, copy_binding]
    )
    secret_name = "task-" + binding
    secret = store.find_secret_record(
        scope="context_instance",
        integration=WALKTHROUGH_SECRET_INTEGRATION,
        name=secret_name,
        context=request.context,
        instance="testing",
    )
    if request.operation in {"run", "cleanup"} and secret is None:
        raise ValueError("There is no task account for this testing copy.")
    if secret is not None and secret.status != "configured" and request.operation != "cleanup":
        raise ValueError("The task account is closed.")
    if secret is not None and request.operation != "cleanup":
        created = datetime.fromisoformat(secret.created_at.replace("Z", "+00:00"))
        if created.tzinfo is None or datetime.now(timezone.utc) - created > timedelta(hours=4):
            raise ValueError("The task account expired; clean it up and use a new task ID.")
    digest = canonical_json_sha256(
        [
            actor,
            request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"}),
            selection.identity.model_dump(mode="json"),
            selection.base_url,
            copy_binding,
            database,
        ]
    )
    return WalkthroughPlan(
        digest, binding, database, copy_binding, generation.prepared_at, secret_name, connection
    )


class WalkthroughAdapter:
    def __init__(
        self,
        *,
        store: PostgresRecordStore,
        root: Path,
        request: OdooWalkthroughRequest,
        actor: str,
        plan: WalkthroughPlan,
        trace: str,
    ) -> None:
        self.store, self.root, self.request, self.actor, self.plan, self.trace = (
            store,
            root,
            request,
            actor,
            plan,
            trace,
        )

    def target_key(self) -> str:
        return generic_web_provider_target_key(
            provider_id="dokploy",
            context=self.request.context,
            instance="testing",
            target_type="compose",
            target_id=self.plan.connection.evidence.target_id,
        )

    def reconciliation_key(self) -> str:
        return self.plan.digest

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        # Never repeat browser transactions after a lost response/process. The
        # original task account has its independent cleanup operation below.
        return ProviderObservation(
            "present",
            response_status_code=200,
            response_payload=self.response(
                OdooWalkthroughResult(
                    status="unknown",
                    plan_sha256=self.plan.digest,
                    task_id=self.request.task_id,
                    operation=self.request.operation,
                    code="interrupted_task_requires_cleanup",
                )
            ),
        )

    def response(self, result: OdooWalkthroughResult) -> dict[str, object]:
        return {
            "status": "accepted",
            "trace_id": self.trace,
            "result": result.model_dump(mode="json"),
        }

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        result = OdooWalkthroughResult(
            status="unknown",
            plan_sha256=self.plan.digest,
            task_id=self.request.task_id,
            operation=self.request.operation,
            code="task_outcome_unknown",
        )
        try:
            lease.assert_current()
            current = plan_walkthrough(self.store, self.root, self.request, self.actor)
            if current.digest != self.plan.digest:
                raise ValueError("Testing task plan changed.")
            lease.checkpoint_effect("testing_task")
            secret = self.store.find_secret_record(
                scope="context_instance",
                integration=WALKTHROUGH_SECRET_INTEGRATION,
                name=self.plan.secret_name,
                context=self.request.context,
                instance="testing",
            )
            if secret is None:
                if self.request.operation != "provision":
                    raise ValueError("No task credential exists.")
                write_secret_value(
                    record_store=self.store,
                    scope="context_instance",
                    integration=WALKTHROUGH_SECRET_INTEGRATION,
                    name=self.plan.secret_name,
                    plaintext_value=random_secrets.token_urlsafe(48),
                    binding_key="TASK_PASSWORD",
                    context_name=self.request.context,
                    instance_name="testing",
                    actor=self.actor,
                    source_label="service_testing_task",
                )
                secret = self.store.find_secret_record(
                    scope="context_instance",
                    integration=WALKTHROUGH_SECRET_INTEGRATION,
                    name=self.plan.secret_name,
                    context=self.request.context,
                    instance="testing",
                )
            assert secret is not None
            version = self.store.read_secret_version(secret.current_version_id)
            password = _decrypt_secret_value(version.ciphertext, version.key_id)
            connection = self.plan.connection
            assert_selection_current(self.store, connection.selection)
            account = testing_account_command(
                host=connection.host,
                token=connection.token,
                server_id=connection.server_id,
                payload={
                    "instance": "testing",
                    "project": connection.app_name,
                    "identity": connection.selection.identity.model_dump(mode="json"),
                    "identity_key": RUNTIME_IDENTITY_ENV_KEY,
                    "image": connection.selection.identity.image_reference,
                    "database": self.plan.database,
                    "copy_binding": self.plan.copy_binding,
                    "usage_since": self.plan.usage_since,
                    "task_id": self.request.task_id,
                    "binding": self.plan.binding,
                    "groups": self.request.groups,
                    "password": password,
                    "operation": self.request.operation,
                },
            )
            uid = int(str(account["account_id"]))
            if uid <= 2 and not (uid == 0 and self.request.operation == "cleanup"):
                raise ValueError("Invalid testing account ID.")
            result.account_id = uid or None
            if self.request.operation == "cleanup":
                # Revoke in Odoo before disabling the internal secret. Retain
                # encrypted history and the marker for audit/retry, never reuse it.
                now = utc_now_timestamp()
                self.store.write_secret_record(
                    secret.model_copy(
                        update={
                            "status": "disabled",
                            "updated_at": now,
                            "updated_by": self.actor,
                        }
                    )
                )
                for binding in self.store.list_secret_bindings(
                    integration=WALKTHROUGH_SECRET_INTEGRATION,
                    context_name=self.request.context,
                    instance_name="testing",
                ):
                    if binding.secret_id == secret.secret_id:
                        self.store.write_secret_binding(
                            binding.model_copy(
                                update={
                                    "status": "disabled",
                                    "updated_at": now,
                                }
                            )
                        )
            elif self.request.operation == "run":

                def checkpoint() -> None:
                    lease.assert_current()
                    assert_selection_current(self.store, connection.selection)
                    require_drill_use_unprotected(
                        self.store, self.request.product, self.request.context
                    )

                passed, screenshots = run_walkthrough_browser(
                    origin=connection.selection.base_url.rstrip("/"),
                    database=self.plan.database,
                    login="lp_walk_" + self.request.task_id,
                    password=password,
                    account_id=uid,
                    identity=connection.selection.identity,
                    steps=self.request.steps,
                    checkpoint=checkpoint,
                )
                result.signed_in = True
                result.steps_passed = passed
                result.screenshots = screenshots
            result.status, result.code = "pass", ""
        except Exception:
            # Provider/browser errors can echo credentials or page content. No
            # exception string, traceback, body or raw output enters a receipt.
            result.status, result.code = "unknown", "task_outcome_unknown_cleanup_required"
        return ProviderMutationOutcome(
            response_status_code=200, response_payload=self.response(result)
        )
