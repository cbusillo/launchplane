"""The native testing rebuild inside the existing durable replacement action."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.contracts.testing_publication import (
    TestingPublicationPlan,
    TestingServingSnapshot,
)
from control_plane.contracts.testing_rebuild import (
    TestingCopy,
    TestingCopyReadiness,
    TestingCopyReceipt,
    TestingRebuildPlan,
    TestingRebuildRecord,
    TestingRouteObservation,
)
from control_plane.dokploy import api, post_deploy, source
from control_plane.dokploy.native_copy_stream import (
    require_registered_remote_server,
    NativeSourceAuthorizationDenied,
)
from control_plane.dokploy.testing_retained_copy import retained_copy_operation
from control_plane.dokploy.testing_route import (
    TestingLaneRoute,
    compile_testing_route,
    observe_testing_route,
    testing_route_id,
)
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.lane_usage import evaluate_lane_usage
from control_plane.odoo_testing_usage import read_odoo_testing_usage
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_allocation import (
    observe_candidate_creation,
    prepare_candidate_storage,
    require_candidate_target_custody,
)
from control_plane.testing_candidate_application import (
    prepare_candidate_application,
    candidate_private_readiness,
    abandon_candidate_application,
    candidate_restore_terminal,
)
from control_plane.testing_candidate_runtime import (
    CandidateSource,
    TestingCandidateRuntimeInputs,
    candidate_application_intent,
    candidate_runtime_inputs,
)
from control_plane.testing_publication import plan_testing_publication
from control_plane.testing_publication_storage import (
    effective_after,
    prepare_publication,
    publish,
    read_publication,
    read_snapshot,
)
from control_plane.testing_rebuild_storage import RecordTestingRebuildStore
from control_plane.testing_serving_copy import published_testing_copy
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.workflows.testing_rebuild import (
    TestingRebuildPreparationFailure,
    execute_testing_rebuild,
)

REBUILD_SCOPE = "launchplane:testing-native-rebuild:v1"
REBUILD_ROUTE = "worker:testing-native-rebuild"


class NativeTestingRebuildPending(ValueError):
    """The original effects need another observation, never an alternate dispatch."""


class NativeTestingRebuildIntent(FrozenRecord):
    operation_id: str
    profile: LaunchplaneProductProfileRecord
    before: TestingServingSnapshot
    server_id: str
    project: str
    old: TestingCopy
    retained: dict[str, Any]
    public_entrypoints: tuple[str, ...]
    credential_peer_projects: tuple[str, ...] = ()
    recorded_template: dict[str, Any] | None = None

    @property
    def digest(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


def native_testing_rebuild(operation: OdooStableTargetReplacementOperationRecord) -> bool:
    return operation.is_native_testing_rebuild


def rebuild_source(operation: OdooStableTargetReplacementOperationRecord) -> CandidateSource:
    modes: dict[str, CandidateSource] = {
        "empty": "empty",
        "production_restore": "production",
        "upstream_restore": "upstream",
    }
    if not native_testing_rebuild(operation) or operation.request.data_source_mode not in modes:
        raise ValueError("A native rebuild requires its explicit testing source.")
    return modes[operation.request.data_source_mode]


def retained_testing_projects(
    store: PostgresRecordStore,
    *,
    before: TestingServingSnapshot,
    product: str,
    context: str,
    host: str,
    token: str,
    server_id: str,
) -> tuple[str, ...]:
    """Only this lane's published predecessor chain can share its local passwords.

    Storage sharing is never excused. Retained copies preserve data and the same
    generated credentials deliberately; a second rebuild must recognize their
    recorded physical custody rather than require password rotation or deletion.
    """
    binding = before.binding
    key = binding.source.source_versions.get("testing_publication", "") if binding else ""
    seen: set[str] = set()
    projects: set[str] = set()
    while key:
        if key in seen:
            raise ValueError("The retained testing publication chain contains a cycle.")
        seen.add(key)
        receipt = read_publication(store, key)
        if (
            receipt is None
            or receipt.state != "applied"
            or (receipt.plan.candidate.product, receipt.plan.candidate.context)
            != (product, context)
        ):
            raise ValueError("Retained testing publication custody is unavailable.")
        prior = receipt.plan.before
        target_id = prior.target_id.target_id
        if any(item.target_id == target_id for item in store.list_dokploy_target_id_records()):
            raise ValueError("A retained testing credential peer aliases a serving lane.")
        aliases = [
            item
            for item in store.list_provider_target_records()
            if (item.provider_id, item.target_category, item.target_id)
            == ("dokploy", "compose", target_id)
        ]
        retained_context = (
            "lp-testing-retained-"
            + canonical_json_sha256({"product": product, "context": context, "target": target_id})[
                :32
            ]
        )
        for item in aliases:
            if (item.context, item.instance) == (
                retained_context,
                "retained",
            ) and item == prior.provider.model_copy(
                update={
                    "context": retained_context,
                    "instance": "retained",
                    "source_label": "service:testing-rebuild-retained",
                }
            ):
                continue
            candidate = store.find_testing_candidate_by_target(target_id)
            if candidate is None or (
                candidate.product,
                candidate.context,
                candidate.namespace,
                "testing-candidate",
            ) != (product, context, item.context, item.instance):
                raise ValueError("A retained testing credential peer has foreign physical custody.")
        if not aliases:
            raise ValueError("A retained testing credential peer lost physical custody.")
        actual = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target_id
        )
        if (actual.get("name"), actual.get("serverId")) != (
            prior.target.target_name,
            server_id,
        ) or not actual.get("appName"):
            raise ValueError("A retained testing credential peer changed placement.")
        projects.add(str(actual["appName"]))
        binding = prior.binding
        key = binding.source.source_versions.get("testing_publication", "") if binding else ""
    return tuple(sorted(projects))


def capture_rebuild_intent(
    root: Path, store: PostgresRecordStore, operation: OdooStableTargetReplacementOperationRecord
) -> NativeTestingRebuildIntent:
    rebuild_source(operation)
    profile = store.read_product_profile_record(operation.product)
    if (
        not profile.is_active
        or profile.driver_id != "odoo"
        or not is_exclusive_product_context(
            context=operation.context,
            product=operation.product,
            owners=product_context_owner_map(store.list_product_profile_records()),
        )
    ):
        raise ValueError("Rebuild requires an exclusive active Odoo testing context.")
    lane = next(
        item
        for item in profile.lanes
        if (item.context, item.instance) == (operation.context, "testing")
    )
    before = read_snapshot(store, product=operation.product, context=operation.context)
    if (before.target.context, before.target.instance, before.target.target_type) != (
        operation.context,
        "testing",
        "compose",
    ) or before.provider.target_id != before.target_id.target_id:
        raise ValueError("The testing target records disagree.")
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    provider = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=before.target_id.target_id
    )
    if (provider.get("composeId"), provider.get("name")) != (
        before.target_id.target_id,
        before.target.target_name,
    ) or not provider.get("appName"):
        raise ValueError("The actual testing target differs from its record.")
    server = require_registered_remote_server(provider.get("serverId"))
    project = str(provider["appName"])
    peers = retained_testing_projects(
        store,
        before=before,
        product=operation.product,
        context=operation.context,
        host=host,
        token=token,
        server_id=server,
    )
    retained = retained_copy_operation(
        host=host,
        token=token,
        server_id=server,
        payload={
            "mode": "read",
            "project": project,
            "context": operation.context,
            "independent_candidate_projects": peers,
        },
    )
    identity = RuntimeIdentity.model_validate(retained["identity"])
    transport = DokployTestingRouteTransport(
        host=host,
        token=token,
        server_id=server,
        old_project=project,
        product=operation.product,
        context=operation.context,
        public_host=str(urlsplit(lane.base_url).hostname),
        entrypoints=(),
        old_runtime_identity=identity,
    )
    frame = transport.read()
    previous = published_testing_copy(store, product=operation.product, context=operation.context)
    template = previous.plan.route.template if previous else None
    endpoints = frame["old_endpoints"]
    if not endpoints:
        raise ValueError("The original testing private backend is unavailable.")
    raw = frame["raw"]
    points = tuple(
        sorted({point for router in raw["routers"].values() for point in router["entryPoints"]})
    )
    if not points:
        raise ValueError("The original testing entrypoints are unavailable.")
    # Bind the actual selected backend, not an arbitrary address on the old process.
    owned = json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None
    namespace = testing_candidate_namespace(
        operation.product, operation.context, operation.operation_id
    )
    possible = []
    for endpoint in endpoints:
        try:
            compile_testing_route(
                product=operation.product,
                context=operation.context,
                host=str(urlsplit(lane.base_url).hostname),
                old_backend=endpoint,
                new_backend=f"http://{namespace}:{profile.runtime_port}",
                raw=raw,
                public_entrypoints=points,
                owned_document=owned,
                recorded_template=template,
            )
        except ValueError:
            continue
        possible.append(endpoint)
    if len(possible) != 1:
        raise ValueError("Exactly one actual retained testing backend is required.")
    return NativeTestingRebuildIntent(
        operation_id=operation.operation_id,
        profile=profile,
        before=before,
        server_id=server,
        project=project,
        old=TestingCopy(
            copy_id=before.target_id.target_id,
            target_id=before.target_id.target_id,
            private_url=possible[0],
            resource_binding=retained["resource_binding"],
            runtime_identity=identity,
        ),
        retained=retained,
        credential_peer_projects=peers,
        public_entrypoints=points,
        recorded_template=template,
    )


class NativeTestingRebuildAdapter:
    def __init__(
        self,
        root: Path,
        store: PostgresRecordStore,
        operation: OdooStableTargetReplacementOperationRecord,
        intent: NativeTestingRebuildIntent,
        authorize: Callable[[str], None],
    ) -> None:
        self.root, self.store, self.operation, self.intent, self.authorize = (
            root,
            store,
            operation,
            intent,
            authorize,
        )
        self.namespace = testing_candidate_namespace(
            operation.product, operation.context, operation.operation_id
        )
        self.checkpoints = RecordTestingRebuildStore(store)

    def target_key(self) -> str:
        return generic_web_provider_target_key(
            provider_id="dokploy",
            context=self.operation.context,
            instance="testing",
            target_type="compose",
            target_id=self.intent.old.target_id,
        )

    def reconciliation_key(self) -> str:
        return self.intent.digest

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.intent.digest:
            return ProviderObservation("unknown")
        # Each component persists its original dispatch before sending it. The
        # exclusive executor can safely continue those observations; none treats
        # a missing/old outcome as permission to repeat its provider command.
        with self.store.testing_replacement_execution_lock(self.operation.operation_id) as check:
            if check is None:
                return ProviderObservation("unknown")
            return ProviderObservation("absent", retry_safe=True)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        with self.store.testing_replacement_execution_lock(self.operation.operation_id) as check:
            if check is None:
                raise NativeTestingRebuildPending("The original rebuild still has an executor.")
            lease.checkpoint_effect("testing_rebuild_execution")
            executor = NativeTestingRebuildExecution(self, lease, check)
            record = executor.run()
            return ProviderMutationOutcome(
                200,
                {
                    "result": rebuild_result(record, executor.allocation).model_dump(mode="json"),
                },
            )


class NativeTestingRebuildExecution:
    def __init__(
        self,
        adapter: NativeTestingRebuildAdapter,
        lease: ProviderOperationLease,
        check_lock: Callable[[], None],
    ) -> None:
        self.adapter, self.lease, self.check_lock = adapter, lease, check_lock
        self.root, self.store, self.operation, self.intent = (
            adapter.root,
            adapter.store,
            adapter.operation,
            adapter.intent,
        )
        self.host, self.token = source.read_dokploy_config(
            control_plane_root=self.root, database_url=self.store.database_url
        )
        self.inputs: TestingCandidateRuntimeInputs | None = None
        self.allocation: TestingCandidateAllocation
        self.plan: TestingRebuildPlan
        self.route: TestingLaneRoute
        self.transport = DokployTestingRouteTransport(
            host=self.host,
            token=self.token,
            server_id=self.intent.server_id,
            old_project=self.intent.project,
            product=self.operation.product,
            context=self.operation.context,
            public_host=str(urlsplit(self.base_url).hostname),
            entrypoints=self.intent.public_entrypoints,
            old_runtime_identity=self.intent.old.runtime_identity,
        )
        self.publication: TestingPublicationPlan | None = None

    @property
    def base_url(self) -> str:
        return next(
            item.base_url
            for item in self.intent.profile.lanes
            if (item.context, item.instance) == (self.operation.context, "testing")
        ).rstrip("/")

    def guard(self, plan: TestingRebuildPlan | None = None) -> None:
        self.check_lock()
        self.lease.assert_current()
        self.adapter.authorize("testing_rebuild_checkpoint")
        if self.store.read_product_profile_record(self.operation.product) != self.intent.profile:
            raise ValueError("The original testing profile changed.")
        target = self.store.read_dokploy_target_record(
            context_name=self.operation.context, instance_name="testing"
        )
        if target.policies.staff_testing_hold is not None:
            raise ValueError("Staff testing holds this lane.")
        self.old_provider()
        current_target = self.store.read_dokploy_target_id_record(
            context_name=self.operation.context, instance_name="testing"
        ).target_id
        if any(
            item.target_id in {self.intent.old.target_id, current_target}
            and (item.context, item.instance) != (self.operation.context, "testing")
            for item in self.store.list_dokploy_target_id_records()
        ):
            raise ValueError("A rebuild copy aliases another serving lane.")

        safety = self.store.read_disposable_lane_safety(
            self.operation.product, self.operation.context, "testing"
        )
        if safety is not None and (
            safety.usage == "in_use"
            or safety.data == "only_copy"
            or safety.production_resources in {"shared", "separation_ready"}
        ):
            raise ValueError(
                "The lane has a recorded protected use, copy or shared production resource."
            )
        if any(
            item.target_id == self.intent.old.target_id
            and (item.context, item.instance) != (self.operation.context, "testing")
            for item in self.store.list_dokploy_target_id_records()
        ):
            raise ValueError("The retained target aliases another lane.")
        usage = read_odoo_testing_usage(
            target=self.intent.before.target,
            target_id=self.intent.before.target_id,
            control_plane_root=self.root,
            database_url=self.store.database_url,
        )
        decision = evaluate_lane_usage(
            self.store,
            profile=self.intent.profile,
            target=target,
            target_id=self.intent.before.target_id,
            instance="testing",
            usage=usage.usage,
        )
        retained = retained_copy_operation(
            host=self.host,
            token=self.token,
            server_id=self.intent.server_id,
            payload=self.retained_payload("read"),
        )
        retiring_original = False
        if retained["stopped"] and plan is not None:
            checkpoint = self.adapter.checkpoints.load(plan)
            stop = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE + ":effect",
                idempotency_key=self.operation.operation_id + ":retire-old",
            )
            retiring_original = (
                checkpoint.retirement_started
                and checkpoint.step == "retiring"
                and stop is not None
                and stop.provider_effect_phase == "retire-old"
                and stop.provider_target_key == self.adapter.target_key() + ":retire-old"
            )
        if decision.protected and not retiring_original:
            raise ValueError("The retained testing copy's occupancy is protected or unverified.")
        current = read_snapshot(
            self.store, product=self.operation.product, context=self.operation.context
        )
        publication = read_publication(self.store, self.publication_key)
        expected = (
            effective_after(publication)
            if publication is not None and publication.state == "applied"
            else self.intent.before
        )
        if current != expected:
            raise ValueError("Testing serving authority changed outside the original rebuild.")
        if not post_deploy.compose_data_workflow_is_quiescent(
            host=self.host,
            token=self.token,
            target_definition=source.DokployTargetDefinition(
                context=self.operation.context,
                instance="testing",
                target_type="compose",
                target_id=self.intent.old.target_id,
            ),
        ):
            raise ValueError("The retained testing copy still has a data workflow.")

    @property
    def publication_key(self) -> str:
        return canonical_json_sha256(
            {
                "product": self.operation.product,
                "context": self.operation.context,
                "operation": self.operation.operation_id,
            }
        )

    def old_provider(self) -> dict[str, Any]:
        provider = api.fetch_dokploy_target_payload(
            host=self.host,
            token=self.token,
            target_type="compose",
            target_id=self.intent.old.target_id,
        )
        if (
            provider.get("composeId"),
            provider.get("name"),
            provider.get("appName"),
            provider.get("serverId"),
        ) != (
            self.intent.old.target_id,
            self.intent.before.target.target_name,
            self.intent.project,
            self.intent.server_id,
        ):
            raise ValueError("The retained testing provider placement changed.")
        return provider

    def retained_payload(self, mode: str) -> dict[str, Any]:
        peers = retained_testing_projects(
            self.store,
            before=self.intent.before,
            product=self.operation.product,
            context=self.operation.context,
            host=self.host,
            token=self.token,
            server_id=self.intent.server_id,
        )
        if peers != self.intent.credential_peer_projects:
            raise ValueError("The original retained testing credential peers changed custody.")
        projects = list(peers)
        candidate = self.store.read_testing_candidate(self.adapter.namespace)
        if candidate is not None and candidate.compose_id:
            provider = api.fetch_dokploy_target_payload(
                host=self.host,
                token=self.token,
                target_type="compose",
                target_id=candidate.compose_id,
            )
            if (provider.get("name"), provider.get("environmentId"), provider.get("serverId")) != (
                candidate.namespace,
                candidate.environment_id,
                candidate.server_id,
            ) or not provider.get("appName"):
                raise ValueError("The admitted independent candidate changed placement.")
            projects.append(str(provider["appName"]))
        return {
            "mode": mode,
            "project": self.intent.project,
            "context": self.operation.context,
            "identity": self.intent.retained["identity"],
            "web_id": self.intent.retained["web_id"],
            "resource_binding": self.intent.old.resource_binding,
            "independent_candidate_projects": projects,
        }

    def run(self) -> TestingRebuildRecord:
        existing_publication = read_publication(self.store, self.publication_key)
        allocation = self.store.read_testing_candidate(self.adapter.namespace)
        if existing_publication is None:
            self.guard()
            if allocation is None:
                allocation = TestingCandidateAllocation(
                    namespace=self.adapter.namespace,
                    product=self.operation.product,
                    context=self.operation.context,
                    operation_id=self.operation.operation_id,
                    intent_sha256=candidate_application_intent(
                        product=self.operation.product,
                        context=self.operation.context,
                        artifact_id=self.operation.request.artifact_id,
                        source=rebuild_source(self.operation),
                    ),
                    serving_target_id=self.intent.old.target_id,
                    server_id=self.intent.server_id,
                    created_at=datetime.now(UTC),
                )
            if allocation.effect in {"project_id", "environment_id", "compose_id"} and not getattr(
                allocation, allocation.effect
            ):
                observed = observe_candidate_creation(
                    root=self.root,
                    store=self.store,
                    namespace=allocation.namespace,
                    guard=self.guard,
                )
                if observed is None:
                    raise NativeTestingRebuildPending(
                        "The original candidate allocation is still unknown."
                    )
                allocation = observed
            if allocation.application is None:
                allocation = prepare_candidate_storage(
                    root=self.root,
                    store=self.store,
                    allocation=allocation,
                    lease=self.lease,
                    guard=self.guard,
                )
            else:
                # Empty-storage qualification precedes application execution.
                # A resumed populated copy is proved by its original custody
                # and private readiness, never by another empty-volume gate.
                require_candidate_target_custody(self.store, allocation)
            self.inputs = candidate_runtime_inputs(
                root=self.root,
                store=self.store,
                allocation=allocation,
                artifact_id=self.operation.request.artifact_id,
                source=rebuild_source(self.operation),
            )
            app_plan = self.inputs.plan
        else:
            self.publication = existing_publication.plan
            allocation = self.store.read_testing_candidate(self.adapter.namespace)
            if allocation != self.publication.candidate:
                raise ValueError("The original publication candidate custody changed.")
            assert allocation is not None and allocation.application is not None
            app_plan = allocation.application.plan
            if existing_publication.state == "planned":
                self.inputs = candidate_runtime_inputs(
                    root=self.root,
                    store=self.store,
                    allocation=allocation,
                    artifact_id=self.operation.request.artifact_id,
                    source=rebuild_source(self.operation),
                )
        self.allocation = allocation
        new = TestingCopy(
            copy_id=allocation.namespace,
            target_id=allocation.compose_id,
            private_url=f"http://{allocation.namespace}:{self.intent.profile.runtime_port}",
            resource_binding=allocation.resources_binding_sha256,
            runtime_identity=app_plan.runtime_identity,
        )
        self.plan = TestingRebuildPlan(
            operation_id=self.operation.operation_id,
            product=self.operation.product,
            context=self.operation.context,
            base_url=self.base_url,
            route_id=testing_route_id(self.operation.product, self.operation.context),
            authority_digest=self.intent.digest,
            source=app_plan.source,
            source_reference=""
            if app_plan.source == "empty"
            else app_plan.native_source.binding_sha256
            if app_plan.native_source is not None
            else canonical_json_sha256(app_plan.upstream_source),
            old=self.intent.old,
            new=new,
        )
        if self.publication is not None:
            self.route = self.publication.route
        else:
            frame = self.transport.read()
            self.route = compile_testing_route(
                product=self.operation.product,
                context=self.operation.context,
                host=self.plan.host,
                old_backend=self.plan.old.private_url,
                new_backend=new.private_url,
                raw=frame["raw"],
                public_entrypoints=self.intent.public_entrypoints,
                owned_document=json.loads(frame["owned_file"])
                if frame["owned_file"] is not None
                else None,
                recorded_template=self.intent.recorded_template,
            )
        return execute_testing_rebuild(self.plan, self.adapter.checkpoints, self)

    @contextmanager
    def exclusive(self, plan: TestingRebuildPlan) -> Iterator[None]:
        self.check_lock()
        yield

    def prepare(self, plan: TestingRebuildPlan) -> TestingCopyReceipt:
        assert self.inputs is not None
        try:
            self.allocation = prepare_candidate_application(
                root=self.root,
                store=self.store,
                allocation=self.allocation,
                inputs=self.inputs,
                lease=self.lease,
                guard=self.guard,
            )
        except ValueError:
            current = self.store.read_testing_candidate(self.allocation.namespace)
            if (
                current is not None
                and current.application is not None
                and current.application.phase == "restoring"
            ):
                evidence = candidate_restore_terminal(
                    host=self.host, token=self.token, record=current, require_success=False
                )
                if evidence.rsplit(":", 1)[-1] in api._DEPLOYMENT_FAILURE_STATUSES:
                    self.allocation = abandon_candidate_application(
                        root=self.root,
                        store=self.store,
                        namespace=current.namespace,
                        lease=self.lease,
                        guard=self.guard,
                    )
                    raise TestingRebuildPreparationFailure(
                        copy_id=plan.new.copy_id, evidence_id=evidence
                    ) from None
            raise
        app = self.allocation.application
        assert app is not None and app.prepared is not None
        prepared = app.prepared
        return TestingCopyReceipt(
            plan_digest=plan.digest,
            copy_id=plan.new.copy_id,
            evidence_id=prepared.source_witness,
            independent_resources_verified=True,
            local_credentials_verified=True,
            live_connections_removed_before_start=True,
        )

    def readiness(self, plan: TestingRebuildPlan) -> TestingCopyReadiness:
        assert self.inputs is not None and self.allocation.application is not None
        prepared = candidate_private_readiness(
            host=self.host,
            token=self.token,
            allocation=self.allocation,
            inputs=self.inputs,
            deployment_id=self.allocation.application.deployment_id,
        )
        assert isinstance(self.lease, ProviderEvidenceLease)
        self.publication = self.publication or plan_testing_publication(
            self.store,
            candidate=self.allocation,
            before=self.intent.before,
            route=self.route,
            reservation=self.lease.evidence_reservation(),
            now=datetime.now(UTC),
        )
        prepare_publication(
            self.store, plan=self.publication, reservation=self.lease.evidence_reservation()
        )
        return TestingCopyReadiness(
            copy_id=plan.new.copy_id,
            host=plan.host,
            observed_at=prepared.observed_at,
            runtime_identity=prepared.runtime_identity,
            healthy=True,
            evidence_id=prepared.source_witness,
        )

    def current_route(self, plan: TestingRebuildPlan) -> TestingRouteObservation:
        frame = self.transport.read()
        backend = observe_testing_route(
            self.route,
            frame["raw"],
            json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None,
        )
        copy = plan.new if backend == plan.new.private_url else plan.old
        return TestingRouteObservation(
            route_id=plan.route_id,
            host=plan.host,
            target_url=backend,
            slot_id=copy.copy_id,
            applied=True,
            authoritative_router_count=1,
            observed_at=datetime.now(UTC),
            public_health=read_public_testing_health(
                self.base_url, self.intent.profile.health_path
            ),
        )

    def switch(
        self,
        plan: TestingRebuildPlan,
        previous: TestingCopy,
        desired: TestingCopy,
        *,
        reconcile_only: bool,
    ) -> None:
        self.run_effect(
            "route:" + desired.copy_id,
            lambda: self.route_backend() == desired.private_url,
            lambda dispatch: self.transport.publish(
                self.route,
                desired.private_url,
                expected_backend=previous.private_url,
                dispatch_guard=dispatch,
            ),
            reconcile_only=reconcile_only,
        )

    def route_backend(self) -> str:
        frame = self.transport.read()
        return observe_testing_route(
            self.route,
            frame["raw"],
            json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None,
        )

    def publish(
        self, plan: TestingRebuildPlan, desired: TestingCopy, *, forward_started: bool
    ) -> None:
        assert self.publication is not None and isinstance(self.lease, ProviderEvidenceLease)
        self.guard()
        observation = self.current_route(plan) if desired == plan.new else None
        publish(
            self.store,
            plan=self.publication,
            reservation=self.lease.evidence_reservation(),
            now=datetime.now(UTC),
            observation=observation,
            inverse=desired == plan.old,
        )

    def retire_old(self, plan: TestingRebuildPlan) -> None:
        def disable_auto_deploy(dispatch: Callable[[Callable[[], None]], None]) -> None:
            def send() -> None:
                api.dokploy_request(
                    host=self.host,
                    token=self.token,
                    path="/api/compose.update",
                    method="POST",
                    payload={"composeId": self.intent.old.target_id, "autoDeploy": False},
                )

            dispatch(send)

        self.run_effect(
            "disable-old-auto-deploy",
            lambda: self.old_provider().get("autoDeploy") is False,
            disable_auto_deploy,
        )
        self.run_effect(
            "retire-old",
            lambda: bool(
                retained_copy_operation(
                    host=self.host,
                    token=self.token,
                    server_id=self.intent.server_id,
                    payload=self.retained_payload("read"),
                )["stopped"]
            ),
            lambda dispatch: retained_copy_operation(
                host=self.host,
                token=self.token,
                server_id=self.intent.server_id,
                payload=self.retained_payload("stop"),
                dispatch_guard=dispatch,
            ),
        )

    def unstarted_retirement(self, plan: TestingRebuildPlan) -> TestingCopyReadiness | None:
        # An unknown or denied original stop is never retried or cleared merely
        # because the old process still appears running.
        return None

    def run_effect(
        self,
        phase: str,
        present: Callable[[], bool],
        execute: Callable[[Callable[[Callable[[], None]], None]], object],
        *,
        reconcile_only: bool = False,
    ) -> None:
        parent = self

        class Effect:
            def target_key(self) -> str:
                return parent.adapter.target_key() + ":" + phase

            def reconciliation_key(self) -> str:
                return canonical_json_sha256({"intent": parent.intent.digest, "phase": phase})

            def observe(
                self,
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> ProviderObservation:
                if present():
                    return ProviderObservation("present", 200)
                return ProviderObservation("unknown" if provider_effect_phase else "absent")

            def apply(
                self, provider_operation_key: str, lease: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                if present():
                    return ProviderMutationOutcome(200, provider_effect_performed=False)

                def dispatch(send: Callable[[], None]) -> None:
                    parent.guard()
                    lease.assert_current()
                    lease.checkpoint_effect(phase)
                    send()

                execute(dispatch)
                if not present():
                    raise NativeTestingRebuildPending("The original effect has not converged.")
                return ProviderMutationOutcome(200)

        prior = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":" + phase,
        )
        assert isinstance(self.lease, ProviderEvidenceLease)
        result = run_durable_provider_operation(
            store=self.store,
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":" + phase,
            request_fingerprint=canonical_json_sha256(
                {"intent": self.intent.digest, "phase": phase}
            ),
            lease_owner=self.lease.evidence_reservation().lease_owner,
            response_trace_id=self.operation.operation_id,
            adapter=Effect(),
            # The child fence, including a missing reservation before any send,
            # proves whether this original effect may dispatch.
            allow_mutation=not (
                reconcile_only and prior is not None and prior.provider_effect_started_at
            ),
        )
        if result.status not in {"completed", "adopted", "replayed"}:
            raise NativeTestingRebuildPending("The original rebuild effect requires observation.")


def rebuild_result(
    record: TestingRebuildRecord, allocation: TestingCandidateAllocation
) -> OdooStableTargetReplacementApplyResult:
    app = allocation.application
    complete = record.step == "complete"
    return OdooStableTargetReplacementApplyResult(
        product=record.plan.product,
        context=record.plan.context,
        instance="testing",
        strategy="replace-and-cutover",
        deployment_record_id=record.plan.new.runtime_identity.deployment_record_id
        if complete
        else "",
        deploy_status="pass" if complete else "fail",
        post_deploy_status="pass" if complete else "fail",
        health_status="pass" if complete else "fail",
        health_url=record.plan.base_url,
        runtime_identity_injected=complete,
        target_id=record.plan.new.target_id if complete else record.plan.old.target_id,
        target_name=allocation.namespace if complete else "",
        artifact_id=record.plan.new.runtime_identity.artifact_id,
        image_reference=record.plan.new.runtime_identity.image_reference,
        runtime_source={
            "testing_rebuild": record.plan.operation_id,
            "testing_candidate": allocation.namespace,
            "copy_source": app.plan.source if app else record.plan.source,
        },
        error_code=record.error,
        error_message="The candidate failed; the original testing copy remains selected."
        if not complete
        else "",
    )


def read_public_testing_health(base_url: str, path: str) -> HealthcheckEvidence:
    """A redirect, timeout or missing identity never proves a healthy origin."""
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPRedirectHandler, Request, build_opener
    from control_plane.contracts.runtime_identity import runtime_identity_from_health_payload

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
        ) -> None:
            return None

    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    try:
        with build_opener(NoRedirect()).open(Request(url, method="GET"), timeout=5) as response:
            if response.geturl() != url:
                return HealthcheckEvidence(status="pending", urls=(url,))
            payload = json.loads(response.read(262144).decode())
            identity = runtime_identity_from_health_payload(payload)
            return HealthcheckEvidence(
                verified=True,
                urls=(url,),
                timeout_seconds=5,
                status="pass",
                observed_runtime_identity=identity,
            )
    except HTTPError as error:
        error.close()
        return HealthcheckEvidence(verified=True, urls=(url,), timeout_seconds=5, status="fail")
    except (URLError, TimeoutError, OSError, ValueError):
        return HealthcheckEvidence(status="pending", urls=(url,))


def execute_native_testing_rebuild(
    *,
    root: Path,
    store: PostgresRecordStore,
    operation: OdooStableTargetReplacementOperationRecord,
    authorize: Callable[[str], None],
    lease_owner: str,
) -> OdooStableTargetReplacementApplyResult:
    try:
        return _execute_native_testing_rebuild(
            root=root,
            store=store,
            operation=operation,
            authorize=authorize,
            lease_owner=lease_owner,
        )
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied(str(error)) from error
        if error.retryable:
            raise NativeTestingRebuildPending(
                "The original provider outcome needs observation."
            ) from error
        raise


def _execute_native_testing_rebuild(
    *,
    root: Path,
    store: PostgresRecordStore,
    operation: OdooStableTargetReplacementOperationRecord,
    authorize: Callable[[str], None],
    lease_owner: str,
) -> OdooStableTargetReplacementApplyResult:
    authorize("testing_rebuild_admission")
    found = store.lookup_existing_mutation_reservation(
        route_path=REBUILD_ROUTE,
        idempotency_key=operation.operation_id,
        request_fingerprint=operation.request_fingerprint,
    )
    if found.status == "missing":
        intent = capture_rebuild_intent(root, store, operation)
    elif found.record is not None and found.status == "found":
        intent = NativeTestingRebuildIntent.model_validate(
            store.read_original_mutation_intent(found.record)
        )
    else:
        raise ValueError("The original testing rebuild reservation is unavailable or conflicts.")
    if intent.operation_id != operation.operation_id or intent.profile.product != operation.product:
        raise ValueError("The original testing rebuild intent belongs to another operation.")
    adapter = NativeTestingRebuildAdapter(root, store, operation, intent, authorize)
    result = run_durable_provider_operation(
        store=store,
        scope=REBUILD_SCOPE,
        route_path=REBUILD_ROUTE,
        idempotency_key=operation.operation_id,
        request_fingerprint=operation.request_fingerprint,
        lease_owner=lease_owner,
        response_trace_id=operation.operation_id,
        adapter=adapter,
        original_intent=intent.model_dump(mode="json"),
    )
    if result.status not in {"completed", "adopted", "replayed"}:
        raise NativeTestingRebuildPending("Continue observing the original testing rebuild.")
    return OdooStableTargetReplacementApplyResult.model_validate(result.response_payload["result"])
