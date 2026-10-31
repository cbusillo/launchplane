"""Testing-only account custody. All external effects remain inside the service."""

from control_plane.contracts.provider_target_origin import ProviderTargetOrigin

from dataclasses import dataclass, field
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal, NamedTuple, cast
import secrets as random_secrets
import time
import hashlib
import base64
from urllib.parse import urlsplit

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_walkthrough import (
    OdooWalkthroughRequest,
    OdooWalkthroughResult,
    WALKTHROUGH_LOGIN_PREFIX,
    WALKTHROUGH_CREDENTIAL_LIFETIME_SECONDS,
    WALKTHROUGH_DISPATCH_SECONDS,
    WALKTHROUGH_READONLY_FIXTURE,
)
from control_plane.contracts.preview_fixture import PreviewFixtureRequest
from control_plane.preview_fixture import (
    PreviewFixtureAdapter,
    fixture_preview_slot,
    require_fixture_target_ownership,
)
from control_plane.disposable_lanes import resolve_disposable_preview, DisposableLaneStore
from control_plane.contracts.preview_execution import (
    odoo_preview_target_key,
    odoo_preview_reconciliation_key,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY
from control_plane.dokploy.native_copy_stream import (
    inspect_native_testing_receiver,
    NativeProgramFailed,
    NativeSourceUnavailable,
)
from control_plane.copy_readback_diagnostics import ReadbackPredicate
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.dokploy.odoo_walkthrough import (
    TestingRead,
    read_testing_connection_isolation,
    read_testing_account_presence,
    read_testing_task_usage,
    testing_account_command,
    testing_account_dispatch_finished,
    testing_task_unused,
)
from control_plane.dokploy import source
from control_plane.odoo_runtime_reads import (
    OdooRuntimeConnection,
    OdooRuntimeReadError,
    assert_selection_current,
    connect_runtime,
    select_stable_runtime,
)
from control_plane.odoo_testing_drill import DrillEvidenceUnavailable, require_drill_use_unprotected
from control_plane.odoo_testing_drill_usage import (
    read_drill_recent_signins,
    DrillUsageUnreadable,
    testing_drill_environment,
)
from control_plane.odoo_testing_restore import generation_binding, read_current_testing_generation
from control_plane.odoo_walkthrough_browser import (
    run_walkthrough_browser,
    ReadonlyBrowserCheckFailed,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
)
from control_plane.secrets import _decrypt_secret_value, write_secret_value
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.odoo_preview_runtime import odoo_compose_has_domain

WALKTHROUGH_SECRET_INTEGRATION = "odoo_walkthrough"


def readonly_secret_prefix(task_id: str) -> str:
    return "readonly-" + task_id + "-"


def walkthrough_secret_name(request: OdooWalkthroughRequest, binding: str) -> str:
    return (readonly_secret_prefix(request.task_id) if request.read_only else "task-") + binding


def disable_walkthrough_credentials(
    store: PostgresRecordStore,
    request: OdooWalkthroughRequest,
    actor: str,
    *,
    secret_id: str = "",
    keep_secret_id: str = "",
) -> None:
    records = store.list_secret_records(
        integration=WALKTHROUGH_SECRET_INTEGRATION,
        context_name=request.context,
        instance_name=request.instance,
    )
    bindings = store.list_secret_bindings(
        integration=WALKTHROUGH_SECRET_INTEGRATION,
        context_name=request.context,
        instance_name=request.instance,
    )
    now = utc_now_timestamp()
    for record in records:
        selected = (
            record.name.startswith(readonly_secret_prefix(request.task_id))
            if request.read_only
            else record.secret_id == secret_id
        )
        if not selected or record.secret_id == keep_secret_id:
            continue
        if record.status == "configured":
            store.write_secret_record(
                record.model_copy(
                    update={"status": "disabled", "updated_at": now, "updated_by": actor}
                )
            )
        for binding in bindings:
            if binding.secret_id == record.secret_id:
                store.write_secret_binding(
                    binding.model_copy(update={"status": "disabled", "updated_at": now})
                )


class WalkthroughPrerequisite(StrEnum):
    NOT_REACHED = "testing_task_prerequisite_not_reached"
    IDENTITY = "testing_task_identity_unavailable"
    GENERATION = "testing_task_generation_unverified"
    GENERATION_MISSING = "testing_task_generation_missing"
    GENERATION_INCOMPLETE = "testing_task_generation_incomplete"
    USE = "testing_task_use_protected"
    PHYSICAL_COPY = "testing_task_physical_copy_unverified"
    RUNTIME = "testing_task_runtime_unavailable"
    ORIGIN = "testing_task_origin_unverified"
    DATABASE = "testing_task_database_unverified"
    ISOLATION_OR_USAGE = "testing_task_isolation_or_usage_unverified"
    # Observed negatives of the native isolation-or-usage read. Its unreadable
    # or invalid outcome stays ISOLATION_OR_USAGE and is reported as unknown.
    RUNNER_UNSELECTED = "testing_task_runner_unselected"
    RUNNER_CHANGED = "testing_task_runner_changed"
    COPY_WITNESS_CHANGED = "testing_task_copy_witness_changed"
    ISOLATION_REFUSED = "testing_task_isolation_refused"
    PROTECTED_SIGNIN = "testing_task_protected_signin_observed"
    ACCOUNT_CHANGED = "testing_task_account_changed"
    CREDENTIAL = "testing_task_credential_custody_unavailable"


class WalkthroughPrerequisiteError(ValueError):
    """Only a code-owned checkpoint may leave the private planning boundary."""

    def __init__(
        self,
        code: WalkthroughPrerequisite | ReadbackPredicate,
        *,
        native_failure: NativeProgramFailed | None = None,
    ) -> None:
        self.code = code
        self.diagnostic_code = native_failure.code if native_failure is not None else code.value
        super().__init__("The testing task prerequisite did not pass: " + code.value + ".")


class WalkthroughEvidenceUnavailable(RuntimeError):
    """A native read returned no usable evidence; never a failed predicate."""


_OBSERVED_READ_REFUSALS = {
    TestingRead.RUNNER_UNSELECTED: WalkthroughPrerequisite.RUNNER_UNSELECTED,
    TestingRead.RUNNER_CHANGED: WalkthroughPrerequisite.RUNNER_CHANGED,
    TestingRead.COPY_CHANGED: WalkthroughPrerequisite.COPY_WITNESS_CHANGED,
    TestingRead.ISOLATION_REFUSED: WalkthroughPrerequisite.ISOLATION_REFUSED,
    TestingRead.PROTECTED_SIGNIN: WalkthroughPrerequisite.PROTECTED_SIGNIN,
    TestingRead.TASK_ACCOUNT_CHANGED: WalkthroughPrerequisite.ACCOUNT_CHANGED,
}


def _require_clear_read(read: TestingRead) -> None:
    if read is TestingRead.CLEAR:
        return
    if read in _OBSERVED_READ_REFUSALS:
        raise WalkthroughPrerequisiteError(_OBSERVED_READ_REFUSALS[read])
    # Unavailable evidence, or an outcome this read mode cannot produce.
    raise WalkthroughEvidenceUnavailable


@contextmanager
def _prerequisite(code: WalkthroughPrerequisite) -> Iterator[None]:
    try:
        yield
    except WalkthroughPrerequisiteError:
        raise
    except NativeProgramFailed as error:
        raise WalkthroughPrerequisiteError(error.predicate, native_failure=error) from None
    except Exception:
        # Provider, validation and secret-store failures can carry credentials
        # or private topology. Never serialize their type, text or traceback.
        raise WalkthroughPrerequisiteError(code) from None


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
    if request.instance != "testing":
        return _plan_preview_walkthrough(store, root, request, actor)
    plan = _walkthrough_preflight(
        store, root, request.product, request.context, request=request, actor=actor
    )
    assert plan is not None
    return plan


def _plan_preview_walkthrough(
    store: PostgresRecordStore, root: Path, request: OdooWalkthroughRequest, actor: str
) -> WalkthroughPlan:
    if (
        not request.read_only
        or request.instance.strip().lower() in {"prod", "production"}
        or not request.preview_id
        or not request.generation_id
    ):
        raise ValueError("Preview sign-in requires an exact read-only target.")
    fixture = PreviewFixtureAdapter(
        store=store,
        root=root,
        product=request.product,
        trace_id="",
        request=PreviewFixtureRequest(
            action="preview-fixture",
            intent="preview-fixture",
            context=request.context,
            instance=request.instance,
            preview_id=request.preview_id,
            generation_id=request.generation_id,
            fixture=WALKTHROUGH_READONLY_FIXTURE,
            reason="Qualify the product-owned read-only test identity.",
        ),
    )
    connection = fixture.connect(mutation=request.operation == "provision")
    if request.operation == "cleanup":
        profile = store.read_product_profile_record(request.product)
        preview = store.read_preview_record(request.preview_id)
        slot = fixture_preview_slot(store, request.product, preview.anchor_pr_number)
        owned = resolve_disposable_preview(
            cast(DisposableLaneStore, store), profile, request.context, slot
        )
        if owned.preview_id != request.preview_id:
            raise ValueError("The cleanup target is not this product's preview.")
        safety = store.read_disposable_lane_safety(request.product, request.context, slot)
        if safety is not None and safety.production_resources in {"shared", "separation_ready"}:
            raise ValueError("Shared production resources forbid test-account cleanup.")
        require_fixture_target_ownership(
            store,
            product=request.product,
            context=request.context,
            anchor_pr_number=preview.anchor_pr_number,
            target_id=connection.evidence.target_id,
            mutation=True,
        )
    parsed = urlsplit(connection.selection.base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Preview sign-in requires its recorded HTTPS origin.")
    if any(
        (urlsplit(lane.base_url).hostname or "").rstrip(".") == (parsed.hostname or "").rstrip(".")
        for profile in store.list_product_profile_records()
        for lane in profile.lanes
    ) or any(
        preview.preview_id != request.preview_id
        and preview.state != "destroyed"
        and (urlsplit(preview.canonical_url).hostname or "").rstrip(".")
        == (parsed.hostname or "").rstrip(".")
        for preview in store.list_preview_records()
    ):
        raise ValueError("The preview origin aliases another lane.")
    database = connection.credentials["ODOO_DB_NAME"]
    copy_binding = canonical_json_sha256(
        [
            request.product,
            request.context,
            request.instance,
            request.preview_id,
            request.generation_id,
            connection.evidence.target_id,
            database,
        ]
    )
    reader = (
        read_testing_connection_isolation
        if request.operation == "cleanup"
        else read_testing_account_presence
    )
    _require_clear_read(
        reader(
            host=connection.host,
            token=connection.token,
            server_id=connection.server_id,
            intent={
                "project": connection.app_name,
                "identity": connection.selection.identity.model_dump(mode="json"),
                "identity_key": RUNTIME_IDENTITY_ENV_KEY,
                "image": connection.selection.identity.image_reference,
                "database": database,
                "copy_binding": copy_binding,
                "task_id": request.task_id,
                "read_only": True,
            },
        )
    )
    account_binding = canonical_json_sha256(
        [request.product, request.context, request.task_id, copy_binding]
    )
    secret_name = walkthrough_secret_name(request, account_binding)
    secret = store.find_secret_record(
        scope="context_instance",
        integration=WALKTHROUGH_SECRET_INTEGRATION,
        name=secret_name,
        context=request.context,
        instance=request.instance,
    )
    if request.operation == "run" and (secret is None or secret.status != "configured"):
        raise ValueError("Provision this preview's read-only test identity first.")
    digest = canonical_json_sha256(
        [
            actor,
            request.operation_payload(exclude={"mode", "reviewed_plan_sha256"}),
            connection.selection.identity.model_dump(mode="json", exclude={"deployed_at"}),
            connection.selection.base_url,
            connection.evidence.target_id,
            connection.evidence.container_id,
            connection.server_id,
            connection.app_name,
            database,
        ]
    )
    return WalkthroughPlan(
        digest,
        account_binding,
        database,
        copy_binding,
        "",
        secret_name,
        connection,
    )


class WalkthroughCheck(NamedTuple):
    checkpoint: WalkthroughPrerequisite
    code: WalkthroughPrerequisite | ReadbackPredicate
    state: Literal["clear", "blocked", "unknown"]


def read_walkthrough_prerequisites(
    store: PostgresRecordStore,
    root: Path,
    product: str,
    context: str,
    request: OdooWalkthroughRequest | None = None,
) -> tuple[WalkthroughCheck, ...]:
    checks: list[WalkthroughCheck] = []
    _walkthrough_preflight(store, root, product, context, request=request, checks=checks)
    return tuple(checks)


def _walkthrough_preflight(
    store: PostgresRecordStore,
    root: Path,
    product: str,
    context: str,
    *,
    request: OdooWalkthroughRequest | None = None,
    actor: str = "",
    checks: list[WalkthroughCheck] | None = None,
) -> WalkthroughPlan | None:
    operation = request.operation if request is not None else "provision"
    passed: set[WalkthroughPrerequisite] = set()

    @contextmanager
    def checkpoint(
        code: WalkthroughPrerequisite,
        requires: tuple[WalkthroughPrerequisite, ...] = (),
        *,
        task_required: bool = False,
    ) -> Iterator[bool]:
        if checks is None:
            with _prerequisite(code):
                yield True
            return
        if any(item not in passed for item in requires):
            checks.append(WalkthroughCheck(code, WalkthroughPrerequisite.NOT_REACHED, "unknown"))
            yield False
            return
        if task_required and request is None:
            checks.append(WalkthroughCheck(code, code, "unknown"))
            yield False
            return
        try:
            yield True
        except WalkthroughPrerequisiteError as error:
            checks.append(WalkthroughCheck(code, error.code, "blocked"))
        except NativeProgramFailed as error:
            checks.append(
                WalkthroughCheck(
                    code, error.predicate, "unknown" if error.unavailable else "blocked"
                )
            )
        except NativeSourceUnavailable:
            checks.append(WalkthroughCheck(code, ReadbackPredicate.NATIVE_READ, "unknown"))
        except OdooRuntimeReadError as error:
            checks.append(
                WalkthroughCheck(code, code, "unknown" if error.status_code >= 500 else "blocked")
            )
        except DrillUsageUnreadable:
            checks.append(WalkthroughCheck(code, ReadbackPredicate.USAGE, "unknown"))
        except DrillEvidenceUnavailable:
            checks.append(WalkthroughCheck(code, code, "unknown"))
        except ValueError:
            checks.append(WalkthroughCheck(code, code, "blocked"))
        # noinspection PyBroadExceptionInspection
        except Exception:
            # Unreadable provider/storage evidence is not a known failed predicate.
            checks.append(WalkthroughCheck(code, code, "unknown"))
        else:
            passed.add(code)
            checks.append(WalkthroughCheck(code, code, "clear"))

    selection = None
    generation = None
    connection = None
    parsed = None
    database = ""
    secret_name = ""
    # This check precedes profile, provider and secret resolution. Even trusted
    # direct callers cannot turn this into a production account operation.
    if request is not None and request.instance != "testing":
        raise ValueError("Only testing lanes support task accounts.")
    with checkpoint(WalkthroughPrerequisite.IDENTITY, ()) as run:
        if run:
            profile = store.read_product_profile_record(product)
            if not profile.is_active:
                raise ValueError("The product is not active.")
            selection = select_stable_runtime(store, profile, "testing")
            if selection.identity.context != context:
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
                and (urlsplit(lane.base_url).hostname or "").rstrip(".")
                == (parsed.hostname or "").rstrip(".")
                for other_profile in store.list_product_profile_records()
                for lane in other_profile.lanes
            ):
                raise ValueError("Testing origin aliases another lane.")
    with checkpoint(WalkthroughPrerequisite.GENERATION, ()) as run:
        if run:
            generation = read_current_testing_generation(
                store,
                product=product,
                context=context,
                instance="testing",
                task_account_cleanup=operation == "cleanup",
                resource_qualification=checks is not None and operation != "cleanup",
            )
            if generation is None:
                raise WalkthroughPrerequisiteError(WalkthroughPrerequisite.GENERATION_MISSING)
            if not generation.completed_at:
                raise WalkthroughPrerequisiteError(WalkthroughPrerequisite.GENERATION_INCOMPLETE)
    with checkpoint(WalkthroughPrerequisite.USE, ()) as run:
        if run:
            if operation != "cleanup":
                require_drill_use_unprotected(store, product, context, allow_down_repair=False)
    # Preserve the existing real host storage/network/image/copy inspection.
    # Its aggregate usage boolean includes our own historical task accounts;
    # the native witnessed-account read below distinguishes those from staff.
    with checkpoint(
        WalkthroughPrerequisite.PHYSICAL_COPY, (WalkthroughPrerequisite.GENERATION,)
    ) as run:
        if run:
            assert generation is not None
            if operation != "cleanup":
                read_drill_recent_signins(store, generation, time.time())
    with checkpoint(
        WalkthroughPrerequisite.RUNTIME,
        (WalkthroughPrerequisite.IDENTITY, WalkthroughPrerequisite.GENERATION),
    ) as run:
        if run:
            assert selection is not None
            assert generation is not None
            connection = connect_runtime(
                store=store,
                selection=selection,
                control_plane_root=root,
                database_url=store.database_url,
            )
            if connection.evidence.target_id != generation.target_id:
                raise ValueError("Testing recipient changed.")
    with checkpoint(WalkthroughPrerequisite.ORIGIN, (WalkthroughPrerequisite.RUNTIME,)) as run:
        if run:
            assert connection is not None
            assert parsed is not None
            if not odoo_compose_has_domain(
                host=connection.host,
                token=connection.token,
                compose_id=connection.evidence.target_id,
                domain_host=parsed.hostname or "",
            ):
                raise ValueError("The origin does not belong to this testing target.")
    with checkpoint(WalkthroughPrerequisite.DATABASE, (WalkthroughPrerequisite.RUNTIME,)) as run:
        if run:
            assert connection is not None
            database = testing_drill_environment(store, context).get("ODOO_DB_NAME", "")
            if not database or database != connection.credentials.get("ODOO_DB_NAME"):
                raise ValueError("Testing database is unavailable or changed.")
    copy_binding = generation_binding(generation) if generation is not None else ""
    if operation == "cleanup":
        with checkpoint(
            WalkthroughPrerequisite.PHYSICAL_COPY,
            (
                WalkthroughPrerequisite.GENERATION,
                WalkthroughPrerequisite.RUNTIME,
                WalkthroughPrerequisite.DATABASE,
            ),
        ) as run:
            if run:
                assert generation is not None
                assert connection is not None and selection is not None
                frames: list[dict[str, object]] = []
                inspect_native_testing_receiver(
                    host=connection.host,
                    token=connection.token,
                    server_id=connection.server_id,
                    spec={
                        "generation": generation.model_dump(mode="json"),
                        "compose_project": connection.app_name,
                        "image_reference": selection.identity.image_reference,
                        "database_name": database,
                        "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
                        "workflow_environment": {},
                        "copy_binding_sha256": copy_binding,
                        "usage_only": True,
                        "task_account_cleanup": True,
                    },
                    receive=frames.append,
                )
                if len(frames) != 1 or frames[0].get("cleanup_qualified") is not True:
                    raise ValueError("Testing cleanup isolation is unavailable.")
    binding = canonical_json_sha256(
        [product, context, request.task_id if request is not None else "", copy_binding]
    )
    readback_intent: dict[str, object] = {}
    if connection is not None and selection is not None and generation is not None:
        readback_intent = {
            "project": connection.app_name,
            "identity": selection.identity.model_dump(mode="json"),
            "identity_key": RUNTIME_IDENTITY_ENV_KEY,
            "image": selection.identity.image_reference,
            "database": database,
            "copy_binding": copy_binding,
            "usage_since": generation.prepared_at,
        }
    with checkpoint(
        WalkthroughPrerequisite.ISOLATION_OR_USAGE,
        (
            WalkthroughPrerequisite.RUNTIME,
            WalkthroughPrerequisite.DATABASE,
            WalkthroughPrerequisite.GENERATION,
        ),
    ) as run:
        if run:
            assert connection is not None
            assert selection is not None
            assert generation is not None
            if operation == "cleanup":
                read = read_testing_connection_isolation(
                    host=connection.host,
                    token=connection.token,
                    server_id=connection.server_id,
                    intent=readback_intent,
                )
            else:
                read = read_testing_task_usage(
                    host=connection.host,
                    token=connection.token,
                    server_id=connection.server_id,
                    require_task=False,
                    intent=readback_intent,
                )
            _require_clear_read(read)
    with checkpoint(
        WalkthroughPrerequisite.CREDENTIAL,
        (WalkthroughPrerequisite.GENERATION,),
        task_required=True,
    ) as run:
        if run:
            assert request is not None
            secret_name = walkthrough_secret_name(request, binding)
            secret = store.find_secret_record(
                scope="context_instance",
                integration=WALKTHROUGH_SECRET_INTEGRATION,
                name=secret_name,
                context=context,
                instance="testing",
            )
            if operation == "run" and secret is None:
                raise ValueError("There is no task account for this testing copy.")
            if (
                secret is not None
                and secret.status != "configured"
                and operation != "cleanup"
                and not (request.read_only and operation == "provision")
            ):
                raise ValueError("The task account is closed.")
            if secret is not None and operation != "cleanup" and not request.read_only:
                created = datetime.fromisoformat(secret.created_at.replace("Z", "+00:00"))
                if created.tzinfo is None or datetime.now(timezone.utc) - created > timedelta(
                    seconds=WALKTHROUGH_CREDENTIAL_LIFETIME_SECONDS
                ):
                    raise ValueError("The task account expired; clean it up and use a new task ID.")
    if checks is not None:
        return None
    assert request is not None and selection is not None
    assert generation is not None and connection is not None
    digest = canonical_json_sha256(
        [
            actor,
            request.operation_payload(exclude={"mode", "reviewed_plan_sha256"}),
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
        self.transient_screenshots: tuple[str, ...] = ()
        self.dispatch_deadline = int(time.time()) + WALKTHROUGH_DISPATCH_SECONDS

    def provider_target_origin(self) -> ProviderTargetOrigin | None:
        if self.request.instance != "testing":
            return None
        return ProviderTargetOrigin(
            product=self.request.product,
            context=self.request.context,
            instance=self.request.instance,
            provider_id="dokploy",
            target_type="compose",
            target_id=self.plan.connection.evidence.target_id,
        )

    def target_key(self) -> str:
        if self.request.instance != "testing":
            return odoo_preview_target_key(
                context=self.request.context, compose_name=self.request.instance
            )
        return generic_web_provider_target_key(
            provider_id="dokploy",
            context=self.request.context,
            instance=self.request.instance,
            target_type="compose",
            target_id=self.plan.connection.evidence.target_id,
        )

    def reconciliation_key(self) -> str:
        if self.request.instance != "testing":
            return odoo_preview_reconciliation_key(
                context=self.request.context, compose_name=self.request.instance
            )
        return self.plan.digest

    def original_intent(self) -> dict[str, object]:
        return {
            "host": self.plan.connection.host,
            "server_id": self.plan.connection.server_id,
            "project": self.plan.connection.app_name,
            "identity": self.plan.connection.selection.identity.model_dump(mode="json"),
            "identity_key": RUNTIME_IDENTITY_ENV_KEY,
            "image": self.plan.connection.selection.identity.image_reference,
            "database": self.plan.database,
            "copy_binding": self.plan.copy_binding,
            "binding": self.plan.binding,
            "task_id": self.request.task_id,
            "usage_since": self.plan.usage_since,
            "dispatch_deadline": self.dispatch_deadline,
            "read_only": self.request.read_only,
            "instance": self.request.instance,
        }

    @staticmethod
    def observe(
        provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase, reconciliation_key
        return ProviderObservation("unknown")

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
            product=self.request.product,
            context=self.request.context,
            instance=self.request.instance,
            deployment_record_id=self.plan.connection.selection.identity.deployment_record_id,
            artifact_id=self.plan.connection.selection.identity.artifact_id,
        )
        lease.assert_current()
        try:
            current = plan_walkthrough(self.store, self.root, self.request, self.actor)
            if current.digest != self.plan.digest:
                raise ValueError("Testing task plan changed.")
        except Exception:
            raise ProviderMutationRejectedError(
                ValueError("Testing task plan is no longer eligible.")
            ) from None
        lease.checkpoint_effect("managed_secret")
        try:
            secret = self.store.find_secret_record(
                scope="context_instance",
                integration=WALKTHROUGH_SECRET_INTEGRATION,
                name=self.plan.secret_name,
                context=self.request.context,
                instance=self.request.instance,
            )
            if (
                secret is None or self.request.read_only and secret.status == "disabled"
            ) and self.request.operation != "cleanup":
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
                    instance_name=self.request.instance,
                    actor=self.actor,
                    source_label="service_testing_task",
                    value_origin="generated",
                )
                secret = self.store.find_secret_record(
                    scope="context_instance",
                    integration=WALKTHROUGH_SECRET_INTEGRATION,
                    name=self.plan.secret_name,
                    context=self.request.context,
                    instance=self.request.instance,
                )
            password = ""
            if self.request.operation != "cleanup":
                assert secret is not None
                version = self.store.read_secret_version(secret.current_version_id)
                password = _decrypt_secret_value(version.ciphertext, version.key_id)
            connection = self.plan.connection
            assert_selection_current(self.store, connection.selection)
            lease.checkpoint_effect("account_dispatch")
            account = testing_account_command(
                host=connection.host,
                token=connection.token,
                server_id=connection.server_id,
                payload={
                    **{
                        key: value
                        for key, value in self.original_intent().items()
                        if key not in {"host", "server_id"}
                    },
                    "instance": self.request.instance,
                    "dispatch_id": hashlib.sha256(provider_operation_key.encode()).hexdigest(),
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
                    **({"password": password} if self.request.operation == "provision" else {}),
                    "operation": self.request.operation,
                },
            )
            if account.get("status") == "fail":
                result.status = "fail"
                result.code = "testing_task_refused_cleanup_required"
                return ProviderMutationOutcome(
                    response_status_code=200, response_payload=self.response(result)
                )
            uid = account["account_id"]
            if type(uid) is not int:
                raise ValueError("The account receipt requires an integer identity.")
            if uid <= 2 and not (uid == 0 and self.request.operation == "cleanup"):
                raise ValueError("Invalid testing account ID.")
            result.account_id = uid or None
            if self.request.operation == "cleanup":
                disable_walkthrough_credentials(
                    self.store,
                    self.request,
                    self.actor,
                    secret_id=secret.secret_id if secret is not None else "",
                )
            elif self.request.operation == "provision" and self.request.read_only:
                assert secret is not None
                disable_walkthrough_credentials(
                    self.store,
                    self.request,
                    self.actor,
                    keep_secret_id=secret.secret_id,
                )
            elif self.request.operation == "run":
                lease.checkpoint_effect("browser_dispatch")

                def checkpoint() -> None:
                    lease.assert_current()
                    assert_selection_current(self.store, connection.selection)
                    if self.request.instance != "testing":
                        fixture = PreviewFixtureAdapter(
                            self.store,
                            self.root,
                            self.request.product,
                            PreviewFixtureRequest(
                                action="preview-fixture",
                                intent="preview-fixture",
                                context=self.request.context,
                                instance=self.request.instance,
                                preview_id=self.request.preview_id,
                                generation_id=self.request.generation_id,
                                fixture=WALKTHROUGH_READONLY_FIXTURE,
                                reason="Verify the current read-only preview target.",
                            ),
                            self.trace,
                        )
                        fixture.selection()
                    else:
                        require_drill_use_unprotected(
                            self.store,
                            self.request.product,
                            self.request.context,
                            allow_down_repair=False,
                        )
                    if not testing_task_unused(
                        host=connection.host,
                        token=connection.token,
                        server_id=connection.server_id,
                        intent=self.original_intent(),
                    ):
                        raise ValueError(
                            "Testing task usage or account identity changed or is unreadable."
                        )

                try:
                    passed, screenshots = run_walkthrough_browser(
                        origin=connection.selection.base_url.rstrip("/"),
                        database=self.plan.database,
                        login=WALKTHROUGH_LOGIN_PREFIX + self.request.task_id,
                        password=password,
                        account_id=uid,
                        identity=connection.selection.identity,
                        steps=self.request.steps,
                        checkpoint=checkpoint,
                        read_only=self.request.read_only,
                    )
                except ReadonlyBrowserCheckFailed as error:
                    if not self.request.read_only:
                        raise
                    lease.assert_current()
                    assert_selection_current(self.store, connection.selection)
                    result.status = "fail"
                    result.code = "read_only_browser_check_failed"
                    result.steps_passed = error.steps_passed
                    return ProviderMutationOutcome(200, self.response(result))
                result.signed_in = True
                result.steps_passed = passed
                self.transient_screenshots = screenshots
                result.screenshot_sha256 = tuple(
                    hashlib.sha256(base64.b64decode(png)).hexdigest() for png in screenshots
                )
            result.status, result.code = "pass", ""
        except ProviderMutationRejectedError:
            raise
        except Exception:
            # Provider/browser errors can echo credentials or page content. No
            # exception string, traceback, body or raw output enters a receipt.
            raise ProviderMutationUnknownError(
                "Task outcome is unknown; reconcile the original dispatch before cleanup."
            ) from None
        return ProviderMutationOutcome(
            response_status_code=200, response_payload=self.response(result)
        )


class InterruptedWalkthroughAdapter:
    """Settle an expired dispatch as UNKNOWN without resolving any current lane."""

    def __init__(
        self,
        original: LaunchplaneIdempotencyRecord,
        request: OdooWalkthroughRequest,
        trace: str,
        store: PostgresRecordStore,
        root: Path,
    ) -> None:
        self.original, self.request, self.trace = original, request, trace
        self.store, self.root = store, root

    def target_key(self) -> str:
        return self.original.provider_target_key

    def reconciliation_key(self) -> str:
        return self.original.reconciliation_key

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.original.reconciliation_key:
            return ProviderObservation("unknown")
        if provider_effect_phase not in {"", "managed_secret"}:
            try:
                intent = self.store.read_original_mutation_intent(self.original)
                host, token = source.read_dokploy_config(
                    control_plane_root=self.root, database_url=self.store.database_url
                )
                if host != intent.get("host") or not testing_account_dispatch_finished(
                    host=host,
                    token=token,
                    server_id=str(intent["server_id"]),
                    dispatch_id=hashlib.sha256(provider_operation_key.encode()).hexdigest(),
                    original_intent=intent,
                ):
                    return ProviderObservation("unknown")
            # noinspection PyBroadExceptionInspection
            except Exception:
                return ProviderObservation("unknown")
        return ProviderObservation(
            "present",
            response_status_code=200,
            response_payload={
                "status": "accepted",
                "trace_id": self.trace,
                "result": OdooWalkthroughResult(
                    status="unknown",
                    task_id=self.request.task_id,
                    operation=self.request.operation,
                    plan_sha256=self.request.reviewed_plan_sha256,
                    product=self.request.product,
                    context=self.request.context,
                    instance=self.request.instance,
                    code=(
                        "interrupted_read_only_observation"
                        if self.request.read_only and self.request.operation == "run"
                        else "interrupted_task_requires_cleanup"
                    ),
                ).model_dump(mode="json"),
            },
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        raise RuntimeError("Evidence recovery cannot dispatch task work.")
