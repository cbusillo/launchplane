"""Fresh application activation retains the unknown predecessor's physical custody."""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import json
import re
from typing import Any, Iterator, cast

import yaml

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.testing_activation import (
    TestingActivationRecord,
    TestingApplicationPredecessor,
    TestingActivationRefusal,
    TestingActivationRefusalCode,
    TestingActivationFailure,
    TestingActivationRefusedResult,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.testing_activation import TestingApplicationReadiness
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    runtime_identity_env,
    parse_runtime_identity_payload,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.dokploy import api, source
from control_plane.dokploy.fresh_application import require_fresh_application_runtime
from control_plane.dokploy_inventory import MAX_PROJECTS, read_dokploy_inventory
from control_plane.generic_web_testing_exclusion import (
    bind_testing_recipient,
    require_retained_application_custody,
)
from control_plane.integration_allowances import target_record_sha256
from control_plane.odoo_testing_resources import ApplicationTestingResources
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderOperationLease,
    provider_operation_title,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    provider_operation_response_payload,
)
from control_plane.testing_activation import (
    TestingActivationAdapter,
    activation_record,
    configuration_digest,
    preparation_resources,
    verified_product_image,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.testing_separation_resources import preparation_binding

APPLICATION_CAPABILITY_PHASE = "application_capability_inspection"


class ApplicationRouteAdmissionContradiction(TestingActivationRefusal):
    """A complete route read positively contradicts the admitted predecessor."""


def undispatched_application_recipient(
    record: TestingActivationRecord, reservation: LaunchplaneIdempotencyRecord
) -> bool:
    """Only the fixed read-only capability program can precede first placement."""
    return (
        record.request.application is not None
        and not any((record.project_id, record.environment_id, record.target_id, record.app_name))
        and not record.state_recipient
        and record.application_readiness is None
        and record.phase == "prepared"
    )


def unstarted_application_recipient(
    record: TestingActivationRecord, reservation: LaunchplaneIdempotencyRecord
) -> bool:
    """Exact answered metadata markers exclude any original image deployment."""
    specification = record.request.application
    readiness = record.application_readiness
    if (
        specification is None
        or specification.state.kind not in {"none", "volume"}
        or record.published
        or not all((record.project_id, record.environment_id, record.target_id, record.app_name))
        or any(record.state_recipient.get(key) for key in ("application_deployment_id",))
        or (
            readiness is not None
            and (readiness.route is not None or readiness.private_health is not None)
        )
    ):
        return False
    if (
        record.phase == "create_application_mount"
        and record.state_recipient.get("mount_verified") != "true"
    ):
        return False
    return record.phase in {
        "recipient_ready",
        "create_application_mount",
        "configure_application",
        "refusing_private_candidate",
        "refused",
    }


def require_retained_route_authority(store: Any, record: TestingActivationRecord) -> None:
    """Corrections may refuse forward work, but cannot redirect this inverse."""
    from control_plane.testing_candidate_runtime import testing_serving_target_authority
    from control_plane.testing_publication_storage import testing_binding_authority

    target = store.read_dokploy_target_record(context_name=record.context, instance_name="testing")
    target_id = store.read_dokploy_target_id_record(
        context_name=record.context, instance_name="testing"
    )
    provider = store.read_provider_target_record(
        context_name=record.context, instance_name="testing"
    )
    try:
        binding = store.read_route_binding_record(
            product=record.product, context_name=record.context, instance_name="testing"
        )
    except FileNotFoundError:
        binding = None
    if (
        testing_serving_target_authority(target)
        != testing_serving_target_authority(record.retained_target)
        or target_id.target_id != record.retained_target_id.target_id
        or provider.model_dump(exclude={"updated_at", "source_label"})
        != record.retained_provider.model_dump(exclude={"updated_at", "source_label"})
        or testing_binding_authority(binding)
        != testing_binding_authority(record.retained_route_binding)
    ):
        raise TestingActivationRefusal("The original serving recipient or route authority changed.")


def completed_application_forward_exclusion(store: Any, record: TestingActivationRecord) -> bool:
    """Only the exact original completed logical child proves terminal forward exclusion."""
    from control_plane.dokploy.testing_route_execution import TestingRouteExecutionIntent

    readiness = record.application_readiness
    if readiness is None or readiness.route is None or readiness.route_execution is None:
        return False
    path = f"/v1/products/{record.product}/testing-activation:route"
    key = record.plan_sha256 + ":route"
    original = store.read_idempotency_record(
        scope=record.scope, route_path=path, idempotency_key=key
    )
    if (
        original is None
        or (
            original.scope,
            original.route_path,
            original.idempotency_key,
            original.state,
            original.request_fingerprint,
            original.reconciliation_key,
            original.provider_target_key,
        )
        != (
            record.scope,
            path,
            key,
            "completed",
            readiness.route.digest,
            readiness.route.digest,
            "testing-activation-route:" + record.product + ":" + record.context,
        )
        or original.response_payload.get("route_write_not_started") is not True
    ):
        return False
    try:
        intent = store.read_original_mutation_intent(original)
        if not isinstance(intent, dict):
            return False
        execution = TestingRouteExecutionIntent.model_validate(intent.get("route_execution"))
    except (FileNotFoundError, ValueError, TypeError):
        return False
    return execution == readiness.route_execution


def completed_application_inverse(store: Any, record: TestingActivationRecord) -> bool:
    """The original inverse child binds the captured route, document and physical custody."""
    from control_plane.dokploy.testing_route_execution import TestingRouteExecutionIntent
    from control_plane.dokploy.testing_route import route_document

    readiness = record.application_readiness
    if readiness is None or readiness.route is None or readiness.route_execution is None:
        return False
    inverse = store.read_idempotency_record(
        scope=record.scope,
        route_path=f"/v1/products/{record.product}/testing-activation:route-inverse",
        idempotency_key=record.plan_sha256 + ":route-inverse",
    )
    if (
        inverse is None
        or inverse.state != "completed"
        or inverse.reconciliation_key != readiness.route.digest
    ):
        return False
    restored = TestingRouteExecutionIntent.model_validate(
        store.read_original_mutation_intent(inverse).get("route_execution")
    )
    expected = readiness.route_execution.model_copy(
        update={
            "backend": readiness.route.old_backend,
            "document_sha256": canonical_json_sha256(
                route_document(readiness.route, readiness.route.old_backend)
            ),
        }
    )
    return restored.model_dump(exclude={"custody"}) == expected.model_dump(exclude={"custody"})


def restored_application_route(store: Any, record: TestingActivationRecord) -> Any:
    """A refused activation may own the restored file without qualifying a product."""
    from control_plane.dokploy.testing_route import route_document

    equivalents = {}
    for value in store.list_testing_application_activations(record.product, record.context):
        previous = TestingActivationRecord.model_validate(value)
        if (
            previous.published
            or previous.failure is None
            or previous.failure.kind != "publication_refused"
        ):
            continue
        parent = store.read_testing_application_activation_operation(
            previous.product, previous.context, previous.scope, previous.request.preparation_key
        )
        result = parent.response_payload.get("result") if parent is not None else None
        if (
            parent is None
            or parent.state != "completed"
            or parent.reconciliation_key != previous.plan_sha256
            or not isinstance(result, dict)
            or result.get("plan_sha256") != previous.plan_sha256
            or result.get("reason") != "publication_refused"
            or result.get("separation_verified") is not False
            or not completed_application_inverse(store, previous)
        ):
            continue
        try:
            require_retained_route_authority(store, previous)
        except TestingActivationRefusal:
            continue
        readiness = previous.application_readiness
        assert (
            readiness is not None
            and readiness.route is not None
            and readiness.old_runtime_identity is not None
        )
        route = readiness.route
        effective = canonical_json_sha256(
            {
                "document": route_document(route, route.old_backend),
                "legacy": route.legacy_routers,
                "legacy_backend": route.legacy_backend,
                "entrypoints": route.public_entrypoints,
                "application_file": (route.application_file_name, route.application_file_sha256),
                "old_identity": readiness.old_runtime_identity.model_dump(exclude={"deployed_at"}),
            }
        )
        equivalents[effective] = route
    if len(equivalents) > 1:
        raise TestingActivationRefusal("The restored original route custody is ambiguous.")
    return next(iter(equivalents.values()), None)


@dataclass
class ApplicationActivationAdapter(TestingActivationAdapter):
    def bindings(self) -> tuple[Any, Any, Any, Any]:
        return bind_testing_recipient(self.store, self.product, self.request.context, "testing")

    def current(self) -> TestingActivationRecord:
        existing = activation_record(
            self.store,
            self.product,
            self.request.context,
            scope=self.scope,
            preparation_key=self.request.preparation_key,
        )
        if existing is not None:
            if existing.actor != self.actor or existing.request.model_dump(
                exclude={"mode", "reviewed_plan_sha256"}
            ) != self.request.model_dump(exclude={"mode", "reviewed_plan_sha256"}):
                raise TestingActivationRefusal("Retain the original request and caller.")
            return existing
        specification = self.request.application
        assert specification is not None
        profile, target, target_id, provider = self.bindings()
        if (
            not profile.is_active
            or target.target_type != "application"
            or (profile.runtime_port, profile.health_path)
            != (specification.runtime_port, specification.health_path)
            or target_record_sha256(target) != self.request.interlock_record_sha256
        ):
            raise TestingActivationRefusal("The application/runtime/interlock authority changed.")
        hold = target.policies.staff_testing_hold
        if hold is None or hold.recorded_by != self.actor:
            raise TestingActivationRefusal("Retain this caller's explicit staging interlock.")
        protection = self.store.read_disposable_lane_safety(
            self.product, self.request.context, "testing"
        )
        if protection is not None and protection.usage == "in_use":
            raise TestingActivationRefusal("Known staff use protects the retained recipient.")
        if self.request.volume_keys or self.request.port_keys:
            raise TestingActivationRefusal(
                "Application state is mounted from its typed specification."
            )
        resources, receipt = preparation_resources(
            self.store, self.product, self.request, self.scope
        )
        if not isinstance(resources, ApplicationTestingResources):
            raise TestingActivationRefusal(
                "Application activation requires its original resource preparation."
            )
        binding, _old_key = preparation_binding(
            self.store, self.product, self.request.context, self.actor, self.root, specification
        )
        from control_plane.testing_separation_resources import ROUTE as PREPARATION_ROUTE

        preparation = self.store.read_idempotency_record(
            scope=self.scope,
            route_path=PREPARATION_ROUTE.format(product=self.product),
            idempotency_key=self.request.preparation_key,
        )
        if preparation is None or preparation.reconciliation_key != binding:
            raise TestingActivationRefusal("Retained allocation authority changed.")
        self.require_independent_credential_plan()
        record = TestingActivationRecord(
            product=self.product,
            context=self.request.context,
            actor=self.actor,
            scope=self.scope,
            request=self.request.model_copy(update={"mode": "dry-run", "reviewed_plan_sha256": ""}),
            plan_sha256="",
            namespace=resources.namespace + "-recipient",
            preparation_record_id=receipt,
            retained_target=target,
            retained_target_id=target_id,
            retained_provider=provider,
            retained_reservations=(),
            configuration_sha256=configuration_digest(
                self.store, self.product, self.request.context
            ),
            configuration_version=3,
            compose_source="",
            image_reference=verified_product_image(
                self.store, self.product, self.request.source_commit
            ),
        )
        from control_plane.generic_web_testing_exclusion import _target_reservations

        record.retained_reservations = _target_reservations(
            self.store,
            product=self.product,
            context=self.request.context,
            target_key=_old_key,
            target_id=target_id.target_id,
        )
        record.retained_reservations = tuple(
            row for row in record.retained_reservations if row.provider_target_key == _old_key
        )
        try:
            record.retained_inventory = self.store.read_environment_inventory(
                context_name=record.context, instance_name="testing"
            )
        except FileNotFoundError:
            pass
        else:
            record.retained_deployment = self.store.read_deployment_record(
                record.retained_inventory.deployment_record_id
            )
        try:
            record.retained_route_binding = self.store.read_route_binding_record(
                product=record.product, context_name=record.context, instance_name="testing"
            )
        except FileNotFoundError:
            pass
        lane = next(
            item
            for item in profile.lanes
            if (item.context, item.instance) == (record.context, "testing")
        )
        audits = self.store.list_ingress_route_audit_records(
            product=record.product, context_name=record.context
        )
        if not lane.base_url and any(
            audit.record_id.startswith("testing-private-ingress-") and audit.status == "applied"
            for audit in audits
        ):
            from control_plane.testing_application_private_route import private_ingress_snapshot

            _, _, record.retained_private_ingress = private_ingress_snapshot(
                self.root, self.store, record.product, record.context
            )
        record.plan_sha256 = canonical_json_sha256(record.model_dump(exclude={"plan_sha256"}))
        return record

    def require_independent_credential_plan(self) -> None:
        specification = self.request.application
        assert specification is not None
        from control_plane.testing_application_credentials import credential_binding_names

        credential_binding_names(self.store, self.product, self.request.context, specification)

    def require_unchanged(self, record: TestingActivationRecord) -> None:
        super().require_unchanged(record)
        specification = record.request.application
        profile = self.store.read_product_profile_record(record.product)
        assert specification is not None
        if (profile.runtime_port, profile.health_path) != (
            specification.runtime_port,
            specification.health_path,
        ):
            raise TestingActivationRefusal(
                "The application runtime contract changed.",
                code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
            )
        if not record.published:
            try:
                inventory = self.store.read_environment_inventory(
                    context_name=record.context, instance_name="testing"
                )
            except FileNotFoundError:
                inventory = None
            if inventory != record.retained_inventory:
                raise TestingActivationRefusal(
                    "The exact predecessor inventory changed.",
                    code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
                )
            if (
                record.retained_deployment is not None
                and self.store.read_deployment_record(record.retained_deployment.record_id)
                != record.retained_deployment
            ):
                raise TestingActivationRefusal(
                    "The exact predecessor deployment changed.",
                    code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
                )
            from control_plane.testing_publication_storage import testing_binding_authority

            try:
                binding = self.store.read_route_binding_record(
                    product=record.product, context_name=record.context, instance_name="testing"
                )
            except FileNotFoundError:
                binding = None
            if testing_binding_authority(binding) != testing_binding_authority(
                record.retained_route_binding
            ):
                raise TestingActivationRefusal(
                    "The original serving route authority changed.",
                    code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
                )
        if record.state_recipient:
            from control_plane.testing_application_credentials import state_password

            for key in record.state_recipient:
                if key.endswith("_version"):
                    state_password(self.store, record, key.removesuffix("_version"))

    @contextmanager
    def evidence(self, lease: ProviderOperationLease) -> Iterator[None]:
        if not isinstance(lease, ProviderEvidenceLease):
            raise TestingActivationRefusal(
                "Application progress requires the original evidence lease."
            )
        with self.store.provider_evidence_guard(lease.evidence_reservation):
            yield

    def live(self, record: TestingActivationRecord) -> api.JsonObject:
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="application", target_id=record.target_id
        )
        if (
            payload.get("applicationId") != record.target_id
            or payload.get("environmentId") != record.environment_id
            or not isinstance(payload.get("appName"), str)
            or not re.fullmatch(
                re.escape(record.namespace) + r"(?:-[a-z0-9]{6})?", str(payload["appName"])
            )
            or (record.app_name and payload["appName"] != record.app_name)
        ):
            raise TestingActivationRefusal("The new application physical identity changed.")
        return payload

    def unused_recipient(self, record: TestingActivationRecord) -> api.JsonObject:
        live = self.live(record)
        old_host, token = source.read_dokploy_config(control_plane_root=self.root)
        old = api.fetch_dokploy_target_payload(
            host=old_host,
            token=token,
            target_type="application",
            target_id=record.retained_target_id.target_id,
        )
        if (
            live.get("serverId") != old.get("serverId")
            or live.get("domains") != []
            or live.get("mounts") != []
        ):
            raise TestingActivationRefusal("The unused application placement or bindings changed.")
        require_fresh_application_runtime(
            live, environment_id=record.environment_id, project_id=record.project_id
        )
        return live

    def recover_placement(self, record: TestingActivationRecord) -> TestingActivationRecord:
        """Only positive original namespace evidence can fill an uncertain create's ID."""
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        inventory = read_dokploy_inventory(host=host, token=token, offset=0, limit=MAX_PROJECTS)
        if inventory.next_offset is not None or inventory.total_projects != len(inventory.projects):
            raise TestingActivationRefusal("Original placement inventory is incomplete.")
        updates: dict[str, str] = {}
        projects = [item for item in inventory.projects if item.name == record.namespace]
        if not record.project_id and record.phase == "prepared":
            if projects:
                raise TestingActivationRefusal(
                    "An unrequested activation namespace already exists."
                )
            return record
        if len(projects) != 1 or (
            record.project_id and projects[0].project_id != record.project_id
        ):
            raise TestingActivationRefusal("Original project is absent, changed or ambiguous.")
        project = projects[0]
        if not record.project_id:
            if record.phase != "create_project":
                raise TestingActivationRefusal(
                    "The original operation did not request this project."
                )
            updates.update(project_id=project.project_id, phase="project_ready")
        environments = [item for item in project.environments if item.name == record.namespace]
        if not record.environment_id and record.phase not in {
            "create_environment",
            "create_application",
        }:
            if environments and record.phase == "project_ready":
                raise TestingActivationRefusal(
                    "An unrequested activation environment already exists."
                )
            return record.model_copy(update=updates)
        if len(environments) != 1 or (
            record.environment_id and environments[0].environment_id != record.environment_id
        ):
            raise TestingActivationRefusal("Original environment is absent, changed or ambiguous.")
        environment = environments[0]
        if not record.environment_id:
            if record.phase != "create_environment":
                raise TestingActivationRefusal(
                    "The original operation did not request this environment."
                )
            updates.update(environment_id=environment.environment_id, phase="environment_ready")
        applications = [
            item
            for item in environment.targets
            if item.category == "application" and item.name == record.namespace
        ]
        if not record.target_id and record.phase != "create_application":
            if applications:
                raise TestingActivationRefusal(
                    "An unrequested activation application already exists."
                )
            return record.model_copy(update=updates)
        if len(applications) != 1 or (
            record.target_id and applications[0].target_id != record.target_id
        ):
            raise TestingActivationRefusal("Original application is absent, changed or ambiguous.")
        if not record.target_id:
            updates.update(target_id=applications[0].target_id, phase="recipient_ready")
        return record.model_copy(update=updates)

    def create_placement(
        self, record: TestingActivationRecord, checkpoint: Callable[[str], None]
    ) -> TestingActivationRecord:
        recovered = self.recover_placement(record)
        if recovered != record:
            record = self.save(
                record,
                **recovered.model_dump(
                    include={"project_id", "environment_id", "target_id", "phase"}
                ),
            )
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        old = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="application",
            target_id=record.retained_target_id.target_id,
        )
        for phase, path, field, body in (
            ("create_project", "/api/project.create", "project_id", {"name": record.namespace}),
            ("create_environment", "/api/environment.create", "environment_id", {}),
            ("create_application", "/api/application.create", "target_id", {}),
        ):
            if getattr(record, field):
                continue
            if phase == "create_environment":
                body = {"name": record.namespace, "projectId": record.project_id}
            if phase == "create_application":
                body = {
                    "name": record.namespace,
                    "appName": record.namespace,
                    "environmentId": record.environment_id,
                }
                if old.get("serverId"):
                    body["serverId"] = str(old["serverId"])
            checkpoint(phase)
            record = self.save(record, phase=phase)
            result = api.dokploy_request(
                host=host, token=token, path=path, method="POST", payload=cast(api.JsonObject, body)
            )
            if isinstance(result, dict) and isinstance(result.get("project"), dict):
                result = result["project"]
            identity = {
                "project_id": "projectId",
                "environment_id": "environmentId",
                "target_id": "applicationId",
            }[field]
            if (
                not isinstance(result, dict)
                or not isinstance(result.get(identity), str)
                or not result[identity]
            ):
                raise TestingActivationRefusal(
                    "Creation outcome is uncertain; preserve its original key."
                )
            record = self.save(
                record,
                **{
                    field: result[identity],
                    "phase": {
                        "project_id": "project_ready",
                        "environment_id": "environment_ready",
                        "target_id": "recipient_ready",
                    }[field],
                },
            )
        if not record.app_name:
            fresh = self.unused_recipient(record)
            record = self.save(record, app_name=fresh["appName"])
        return record

    def save_state(self, record: TestingActivationRecord, **values: str) -> TestingActivationRecord:
        return self.save(record, state_recipient={**record.state_recipient, **values})

    def deploy_owned_target(
        self,
        record: TestingActivationRecord,
        *,
        target_type: str,
        target_id: str,
        title: str,
        state_key: str,
        checkpoint: Callable[[str], None],
    ) -> TestingActivationRecord:
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        if state_key in record.state_recipient:
            return record
        phase = "deploy_" + state_key
        if record.phase == phase:
            observed = api.deployment_for_target_by_title(
                host=host, token=token, target_type=target_type, target_id=target_id, title=title
            )
            if (
                observed is None
                or api.deployment_status(observed) != "done"
                or not api.deployment_key(observed)
            ):
                raise TestingActivationRefusal(
                    "The original deployment remains unobserved; do not repeat it."
                )
            return self.save_state(record, **{state_key: api.deployment_key(observed)})
        history = api.list_deployments_for_target(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
            require_complete=True,
        )
        if history:
            raise TestingActivationRefusal(
                "The fresh recipient has an unrequested deployment history."
            )
        checkpoint(phase)
        record = self.save(record, phase=phase)
        api.trigger_deployment(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
            no_cache=False,
            title=title,
        )
        deployment = api.wait_for_target_deployment(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
            before_key="",
            timeout_seconds=300,
            deployment_title=title,
        )
        if not deployment:
            raise TestingActivationRefusal("Original deployment completion is unavailable.")
        return self.save_state(record, **{state_key: deployment})

    def application_values(self, record: TestingActivationRecord) -> dict[str, str]:
        from control_plane.runtime_environments import (
            resolve_site_runtime_environment,
            site_delivered_secret_bindings_from_store,
        )
        from control_plane.testing_application_credentials import staged_runtime_values

        staged = staged_runtime_values(self.store, record)
        bindings = site_delivered_secret_bindings_from_store(
            record_store=self.store, context_name=record.context, instance_name="testing"
        )
        environment = resolve_site_runtime_environment(
            control_plane_root=self.root,
            context_name=record.context,
            instance_name="testing",
            database_url=self.store.database_url,
            target_environment_override={},
            secret_binding_keys=frozenset(set(bindings) - set(staged)),
        )
        values = {**environment.values, **staged}
        from control_plane.runtime_platform_credentials import looks_like_credential

        if any(
            looks_like_credential(key, value)
            and key not in environment.secret_keys
            and key not in staged
            for key, value in values.items()
        ):
            raise TestingActivationRefusal(
                "Application credentials require purpose-scoped managed bindings."
            )
        if record.application_readiness is None:
            raise TestingActivationRefusal(
                "The original application runtime identity is unavailable."
            )
        values.update(runtime_identity_env(record.application_readiness.runtime_identity))
        return values

    def prepare_credentials(self, record: TestingActivationRecord) -> TestingActivationRecord:
        """Preparation is purpose-scoped; canonical site delivery is unchanged."""
        from control_plane.testing_application_credentials import (
            generation_bundle,
            STATE_SECRET_INTEGRATION,
        )

        if record.state_recipient.get("credentials_prepared") == "true":
            return record
        bundle = generation_bundle(self.store, record)
        state = {**record.state_recipient, "credentials_prepared": "true"}
        for secret in bundle.secret_records:
            if secret.integration != STATE_SECRET_INTEGRATION:
                raise TestingActivationRefusal(
                    "Preparation cannot activate canonical runtime delivery."
                )
            purpose = secret.name.removeprefix(record.namespace + "-")
            state[purpose + "_version"] = secret.current_version_id
        replacement = record.model_copy(update={"state_recipient": state})
        self.store.write_testing_activation(
            expected=record.model_dump(mode="json"),
            replacement=replacement.model_dump(mode="json"),
            bundle=bundle.model_copy(
                update={
                    "required_context_owner": (record.product, record.context),
                    "expected_product_profiles": (
                        self.store.read_product_profile_record(record.product),
                    ),
                }
            )
            if bundle.requires_write()
            else None,
        )
        self.require_unchanged(replacement)
        return replacement

    def registry_values(self, record: TestingActivationRecord) -> api.JsonObject:
        """Provider-side pull credentials never enter the application's environment."""
        from control_plane.workflows.dokploy_deploy import (
            _canonical_registry_host,
            _docker_image_registry_host,
        )

        host, token = source.read_dokploy_config(control_plane_root=self.root)
        old = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="application",
            target_id=record.retained_target_id.target_id,
        )
        result: api.JsonObject = {
            key: old.get(key) for key in ("username", "password", "registryUrl", "registryId")
        }
        if result["registryId"]:
            registry = api.dokploy_request(
                host=host,
                token=token,
                path="/api/registry.one",
                query={"registryId": str(result["registryId"])},
            )
            if not isinstance(registry, dict) or registry.get("registryId") != result["registryId"]:
                raise TestingActivationRefusal(
                    "The existing image registry identity is unavailable."
                )
            result.update(
                {key: registry.get(key) for key in ("username", "password", "registryUrl")}
            )
        if any(result.get(key) for key in ("username", "password", "registryUrl")) and (
            not result.get("username")
            or not result.get("password")
            or _canonical_registry_host(str(result.get("registryUrl") or ""))
            != _docker_image_registry_host(record.image_reference)
        ):
            raise TestingActivationRefusal(
                "The provider pull credentials do not identify this image registry."
            )
        return result

    def verify_provider(self, record: TestingActivationRecord) -> None:
        """Accept only the original Docker source, exact managed env and supported mount."""
        specification = record.request.application
        assert specification is not None
        live = self.live(record)
        registry = self.registry_values(record)
        if (
            live.get("sourceType") != "docker"
            or live.get("dockerImage") != record.image_reference
            or live.get("autoDeploy") is not False
            or api.parse_dokploy_env_text(str(live.get("env") or ""))
            != self.application_values(record)
            or any(
                live.get(key) not in (value, "" if value is None else value)
                for key, value in registry.items()
                if key != "registryId"
            )
            or canonical_json_sha256(registry) != record.state_recipient.get("registry_sha256")
        ):
            raise TestingActivationRefusal(
                "The fresh application source, credentials or managed inputs changed."
            )
        mounts = live.get("mounts")
        if specification.state.kind == "volume":
            resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
            assert isinstance(resources, ApplicationTestingResources)
            if not self.mount_matches(record, live, resources):
                raise TestingActivationRefusal("The independent application mount changed.")
        elif mounts != []:
            raise TestingActivationRefusal("The application has an unrequested state mount.")
        self.verify_provider_options(record, live)

    def verify_provider_options(
        self, record: TestingActivationRecord, live: api.JsonObject
    ) -> None:
        from copy import deepcopy

        # Reapply the existing vanilla-runtime checker to every uncontrolled field.
        # Its initial-only fields are replaced only after separately proving their
        # exact original activation values above and the actual runtime below.
        vanilla = deepcopy(live)
        for key in ("dockerImage", "username", "password", "registryUrl", "registryId"):
            vanilla[key] = None
        vanilla.update(
            sourceType=None,
            env="",
            autoDeploy=False,
            mounts=[],
            deployments=[],
            applicationStatus="idle",
        )
        vanilla["registry"] = None
        require_fresh_application_runtime(
            vanilla, environment_id=record.environment_id, project_id=record.project_id
        )

    @staticmethod
    def mount_matches(
        record: TestingActivationRecord,
        live: api.JsonObject,
        resources: ApplicationTestingResources,
    ) -> bool:
        specification = record.request.application
        assert specification is not None
        rows = live.get("mounts")
        mount = api.as_json_object(rows[0]) if isinstance(rows, list) and len(rows) == 1 else None
        return mount is not None and all(
            mount.get(key) == value
            for key, value in {
                "type": "volume",
                "volumeName": resources.volumes["state"],
                "mountPath": specification.state.container_path,
                "serviceType": "application",
                "serviceId": record.target_id,
            }.items()
        )

    def configure_application(
        self,
        record: TestingActivationRecord,
        resources: ApplicationTestingResources,
        checkpoint: Callable[[str], None],
    ) -> TestingActivationRecord:
        specification = record.request.application
        assert specification is not None
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        if record.application_readiness is None:
            identity = RuntimeIdentity(
                product=record.product,
                context=record.context,
                instance="testing",
                deployment_record_id="deployment-testing-activation-" + record.plan_sha256,
                artifact_id=record.image_reference,
                source_git_ref=record.request.source_commit,
                image_reference=record.image_reference,
            )
            record = self.save(
                record, application_readiness=TestingApplicationReadiness(runtime_identity=identity)
            )
        if "registry_sha256" not in record.state_recipient:
            record = self.save_state(
                record, registry_sha256=canonical_json_sha256(self.registry_values(record))
            )
        if specification.state.kind == "volume" and "mount_verified" not in record.state_recipient:
            live = self.live(record)
            mounts = live.get("mounts")
            matches = self.mount_matches(record, live, resources)
            if not matches:
                if (
                    record.phase == "create_application_mount"
                    or mounts != []
                    or live.get("deployments") != []
                ):
                    raise TestingActivationRefusal(
                        "The original mount outcome is unknown; do not repeat it."
                    )
                checkpoint("create_application_mount")
                record = self.save(record, phase="create_application_mount")
                api.dokploy_request(
                    host=host,
                    token=token,
                    path="/api/mounts.create",
                    method="POST",
                    payload={
                        "type": "volume",
                        "volumeName": resources.volumes["state"],
                        "mountPath": specification.state.container_path,
                        "serviceType": "application",
                        "serviceId": record.target_id,
                    },
                )
                live = self.live(record)
                mounts = live.get("mounts")
                if not self.mount_matches(record, live, resources):
                    raise TestingActivationRefusal("The original mount remains unobserved.")
            record = self.save_state(record, mount_verified="true")
        if "application_configured" not in record.state_recipient:
            live = self.live(record)
            registry = self.registry_values(record)
            if live.get("deployments") != []:
                raise TestingActivationRefusal(
                    "The application deployed before configuration was qualified."
                )
            checkpoint("configure_application")
            record = self.save(record, phase="configure_application")
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/application.update",
                method="POST",
                payload={
                    "applicationId": record.target_id,
                    "sourceType": "docker",
                    "autoDeploy": False,
                },
            )
            checkpoint(record.phase)
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/application.saveDockerProvider",
                method="POST",
                payload={
                    "applicationId": record.target_id,
                    "dockerImage": record.image_reference,
                    **{key: registry[key] for key in ("username", "password", "registryUrl")},
                },
            )
            checkpoint(record.phase)
            api.update_dokploy_target_env(
                host=host,
                token=token,
                target_type="application",
                target_id=record.target_id,
                target_payload=self.live(record),
                env_text=api.serialize_dokploy_env_text(self.application_values(record)),
            )
            self.verify_provider(record)
            record = self.save_state(record, application_configured="true")
        self.verify_provider(record)
        return record

    def qualify_application(self, record: TestingActivationRecord) -> TestingActivationRecord:
        from control_plane.testing_application_runtime import (
            inspect_application_runtime,
            VerifiedApplicationRuntimeFailure,
        )

        specification = record.request.application
        readiness = record.application_readiness
        assert specification is not None and readiness is not None
        if readiness.route is not None:
            from control_plane.contracts.runtime_identity import compare_runtime_identity

            health = readiness.private_health
            if (
                health is None
                or not health.verified
                or health.status != "pass"
                or health.runtime_identity_status != "match"
                or compare_runtime_identity(
                    expected=readiness.runtime_identity, observed=health.observed_runtime_identity
                )[0]
                != "match"
            ):
                raise TestingActivationRefusal(
                    "The original private route admission is unavailable."
                )
            return record
        self.verify_provider(record)
        resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        failure = None
        try:
            frame = inspect_application_runtime(
                host=host,
                token=token,
                server_id=str(self.live(record).get("serverId") or ""),
                application=record.app_name,
                runtime_port=specification.runtime_port,
                network=specification.network_name,
                image=record.image_reference,
                identity=readiness.runtime_identity,
                values=self.application_values(record),
                volume=resources.volumes.get("state", ""),
                volume_binding=resources.binding_sha256,
                mount_path=specification.state.container_path,
                health_path=specification.health_path,
            )
        except VerifiedApplicationRuntimeFailure as error:
            frame, failure = error.evidence, error
        health_identity = parse_runtime_identity_payload(frame.get("health_identity"))
        identity_status, identity_detail = frame["identity_status"], frame["identity_detail"]
        replacement = readiness.model_copy(
            update={
                "service_id": frame["service_id"],
                "task_id": frame["task_id"],
                "container_id": frame["container_id"],
                "backend": frame["backend"],
                "private_health": HealthcheckEvidence(
                    status="fail" if failure is not None else "pass",
                    verified=True,
                    urls=(frame["health_url"],),
                    timeout_seconds=10,
                    runtime_identity_status=identity_status,
                    runtime_identity_detail=identity_detail,
                    observed_runtime_identity=health_identity,
                ),
            }
        )
        if replacement != readiness:
            record = self.save(record, application_readiness=replacement)
        if failure is not None:
            raise TestingActivationRefusal(
                "The verified private candidate failed.",
                code=TestingActivationRefusalCode.RECIPIENT_UNQUALIFIED,
            ) from failure
        return record

    def route_authority(
        self, record: TestingActivationRecord, *, check_private: bool = True
    ) -> tuple[str, str]:
        from urllib.parse import urlsplit

        profile = self.store.read_product_profile_record(record.product)
        lane = next(
            item
            for item in profile.lanes
            if (item.context, item.instance) == (record.context, "testing")
        )
        if lane.base_url:
            hostname = urlsplit(lane.base_url).hostname
            if not hostname:
                raise ApplicationRouteAdmissionContradiction(
                    "The stable public address is unavailable."
                )
            return hostname, lane.base_url
        if record.retained_private_ingress:
            if check_private:
                self.require_private_ingress(record)
            endpoint = self.store.read_private_health_endpoint_record(
                record.retained_private_ingress["endpoint_key"]
            )
            return urlsplit(endpoint.url).hostname or "", endpoint.url
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        old = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="application",
            target_id=record.retained_target_id.target_id,
        )
        if (
            record.retained_target.domains
            or record.retained_target.public_hosts
            or record.retained_target.pending_public_host_effects
            or old.get("domains") != []
        ):
            raise ApplicationRouteAdmissionContradiction(
                "The addressed lane has no admitted stable route authority."
            )
        return "", ""

    def require_private_ingress(self, record: TestingActivationRecord) -> None:
        if not record.retained_private_ingress:
            return
        from control_plane.testing_application_private_route import (
            private_ingress_snapshot,
            require_private_ingress_records,
            PrivateIngressAuthorityChanged,
        )

        try:
            require_private_ingress_records(
                self.store, record.product, record.context, record.retained_private_ingress
            )
        except PrivateIngressAuthorityChanged as error:
            raise TestingActivationRefusal(
                str(error), code=TestingActivationRefusalCode.AUTHORITY_CHANGED
            ) from error
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        actual = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="application",
            target_id=record.retained_target_id.target_id,
        )
        if (
            actual.get("applicationId") == record.retained_target_id.target_id
            and isinstance(actual.get("serverId"), str)
            and actual["serverId"] != record.retained_private_ingress["server_id"]
        ):
            raise TestingActivationRefusal(
                "The retained private application server changed.",
                code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
            )
        selected = None
        readiness = record.application_readiness
        if readiness and readiness.route:
            child = self.store.read_idempotency_record(
                scope=record.scope,
                route_path=f"/v1/products/{record.product}/testing-activation:route",
                idempotency_key=record.plan_sha256 + ":route",
            )
            if child:
                selected = record
        try:
            _, _, binding = private_ingress_snapshot(
                self.root,
                self.store,
                record.product,
                record.context,
                endpoint_key=record.retained_private_ingress["endpoint_key"],
                route_activation=selected,
            )
        except PrivateIngressAuthorityChanged as error:
            raise TestingActivationRefusal(
                str(error), code=TestingActivationRefusalCode.AUTHORITY_CHANGED
            ) from error
        if binding != record.retained_private_ingress:
            raise TestingActivationRefusal(
                "The original private ingress authority changed.",
                code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
            )

    def route_admission(self, record: TestingActivationRecord) -> tuple[Any, dict[str, Any]] | None:
        from control_plane.testing_application_capabilities import (
            retained_application_capability_spec,
        )
        from control_plane.testing_activation import completed_application_activation
        from control_plane.dokploy.testing_route import (
            admit_testing_route_predecessor,
            testing_ingress_entrypoints,
        )
        from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport

        hostname, _ = self.route_authority(record)
        if not hostname:
            return None
        specification, readiness = record.request.application, record.application_readiness
        assert specification is not None
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        old_payload = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="application",
            target_id=record.retained_target_id.target_id,
        )
        old_spec = retained_application_capability_spec(
            self.store, self.root, record.product, record.context
        )
        old_spec.update(name=old_spec["application"], port=specification.runtime_port)
        predecessor = record.predecessor
        if predecessor is not None:
            identity = predecessor.runtime_identity
            old_spec.update(
                service_id=predecessor.service_id,
                task_template_sha256=predecessor.task_template_sha256,
                deployments={
                    identity.deployment_record_id: {
                        "artifact": identity.artifact_id,
                        "source": identity.source_git_ref,
                    }
                },
            )
        previous_route = None
        previous_key = (
            record.retained_route_binding.source.source_versions.get("testing_activation")
            if record.retained_route_binding
            else None
        )
        previous = [
            TestingActivationRecord.model_validate(value)
            for value in self.store.list_testing_application_activations(
                record.product, record.context
            )
        ]
        matches = [
            value
            for value in previous
            if (previous_key is None or value.plan_sha256 == previous_key)
            and value.target_id == record.retained_target_id.target_id
            and completed_application_activation(self.store, value)
        ]
        if len(matches) > 1:
            raise TestingActivationRefusal("The original owned route custody is ambiguous.")
        if matches:
            previous_readiness = matches[0].application_readiness
            if previous_readiness is None or previous_readiness.route is None:
                raise TestingActivationRefusal("The original owned route template is unavailable.")
            previous_route = previous_readiness.route
        restored_history = False
        if previous_route is None:
            previous_route = restored_application_route(self.store, record)
            restored_history = previous_route is not None
        fresh_restored_history = restored_history and not (readiness and readiness.route)
        original_route = (readiness.route if readiness else None) or previous_route
        if original_route is not None and original_route.application_file_name:
            old_spec["route_file_name"] = original_route.application_file_name
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        transport = DokployTestingRouteTransport(
            host=host,
            token=token,
            server_id=str(old_payload.get("serverId") or ""),
            old_project="",
            product=record.product,
            context=record.context,
            public_host=hostname,
            entrypoints=readiness.route.public_entrypoints if readiness and readiness.route else (),
            old_runtime_identity=predecessor.runtime_identity if predecessor else None,
            old_application=old_spec,
            new_application={
                "name": record.app_name,
                "port": specification.runtime_port,
                "service_id": readiness.service_id if readiness else "",
                "task_id": readiness.task_id if readiness else "",
            },
        )
        if (
            original_route is not None
            and original_route.application_file_sha256
            and not fresh_restored_history
        ):
            transport.payload["expected_application_file_sha256"] = (
                original_route.application_file_sha256
            )
        frame = transport.read()
        if fresh_restored_history and frame["owned_file"] is None:
            # A verified inverse can be a no-op. Its historical owned template
            # cannot replace today's exact initial domain/file/backend admission.
            previous_route = None
        if previous_route is None:
            old = api.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type="application",
                target_id=record.retained_target_id.target_id,
            )
            domains = old.get("domains")
            if not isinstance(domains, list) or not any(
                isinstance(domain, dict)
                and domain.get("host") == hostname
                and domain.get("applicationId") == record.retained_target_id.target_id
                and domain.get("port") == specification.runtime_port
                and domain.get("path") in {None, "", "/"}
                and domain.get("domainId")
                for domain in domains
            ):
                raise ApplicationRouteAdmissionContradiction(
                    "The retained application does not own this exact stable host/port."
                )
        try:
            document = (
                yaml.safe_load(frame["application_file"])
                if frame["application_file"] is not None
                else None
            )
            if not isinstance(document, dict):
                raise ValueError("The actual retained application route file is unavailable.")
            points = testing_ingress_entrypoints(frame["raw"], hostname)
            arguments = dict(
                product=record.product,
                context=record.context,
                host=hostname,
                old_backend=f"http://{old_spec['name']}:{specification.runtime_port}",
                old_backend_addresses=tuple(frame["old_endpoints"]),
                raw=frame["raw"],
                public_entrypoints=points,
                owned_document=json.loads(frame["owned_file"])
                if frame["owned_file"] is not None
                else None,
                recorded_template=previous_route.template if previous_route else None,
                application_document=document,
                application_file_name=frame["application_file_name"],
                application_file_sha256=frame["application_file_sha256"],
                recorded_application_route=previous_route,
            )
            admit_testing_route_predecessor(**arguments)
        except ValueError as error:
            raise ApplicationRouteAdmissionContradiction(str(error)) from error
        transport.payload["entrypoints"] = points
        return transport, arguments

    def read_edge_health(
        self,
        record: TestingActivationRecord,
        *,
        prospective: bool = False,
        expected: RuntimeIdentity | None = None,
    ) -> Any:
        from control_plane.contracts.promotion_record import HealthcheckEvidence
        from control_plane.contracts.runtime_identity import health_payload_runtime_identity_status
        from control_plane.testing_rebuild_service import read_public_testing_health

        _, address = self.route_authority(record, check_private=False)
        if not record.retained_private_ingress:
            assert record.request.application is not None
            return read_public_testing_health(
                address, record.request.application.health_path, expected
            )
        from control_plane.testing_private_health import fetch_testing_private_health
        from control_plane.contracts.private_health_endpoint_record import (
            private_health_endpoint_record_sha256,
        )

        endpoint = self.store.read_private_health_endpoint_record(
            record.retained_private_ingress["endpoint_key"]
        )
        try:
            response = fetch_testing_private_health(
                root=self.root,
                store=self.store,
                product=record.product,
                context=record.context,
                instance="testing",
                endpoint_key=endpoint.endpoint_key,
                endpoint_sha256=private_health_endpoint_record_sha256(endpoint),
                url=endpoint.url,
                timeout_seconds=5,
                route_activation=record if prospective else None,
            )
            if 300 <= response.status_code < 400:
                return HealthcheckEvidence(status="pending", urls=(address,))
            payload = json.loads(response.body) if 200 <= response.status_code < 300 else None
            if 200 <= response.status_code < 300 and not isinstance(payload, dict):
                raise ValueError("Private health document unavailable.")
            status, detail, identity = health_payload_runtime_identity_status(
                expected=expected, payload=payload
            )
            return HealthcheckEvidence(
                status="pass" if 200 <= response.status_code < 300 else "fail",
                verified=True,
                timeout_seconds=5,
                urls=(address,),
                observed_runtime_identity=identity,
                runtime_identity_status=status,
                runtime_identity_detail=detail,
            )
        except (ValueError, OSError, RuntimeError):
            return HealthcheckEvidence(status="pending", urls=(address,))

    def preflight_application_route(
        self, record: TestingActivationRecord
    ) -> TestingApplicationPredecessor | None:
        from control_plane.testing_application_private_route import application_route_transport

        if record.application_readiness and record.application_readiness.route:
            if record.predecessor is None:
                raise ValueError("The captured predecessor service witness is unavailable.")
            application_route_transport(self.root, record).read()
            return record.predecessor
        admission = self.route_admission(record)
        if admission is None:
            return None
        transport, _ = admission
        if transport.old_runtime_identity is None:
            raise ApplicationRouteAdmissionContradiction(
                "The retained runtime identity is unavailable."
            )
        binding = transport.payload["old_application"]
        assert isinstance(binding, dict)
        observed = TestingApplicationPredecessor(
            runtime_identity=transport.old_runtime_identity,
            service_id=binding["service_id"],
            task_template_sha256=binding["task_template_sha256"],
        )
        if record.predecessor is not None and record.predecessor != observed:
            raise ValueError("The original retained service witness changed.")
        return observed

    def switch_application_route(
        self,
        record: TestingActivationRecord,
        lease: ProviderOperationLease,
        checkpoint: Callable[[str], None],
    ) -> TestingActivationRecord:
        from control_plane.dokploy.testing_route import compile_testing_route
        from control_plane.testing_application_private_route import application_route_transport
        from control_plane.testing_application_runtime import qualify_edge_health

        readiness = record.application_readiness
        assert readiness is not None
        if readiness.route is None:
            admission = self.route_admission(record)
            if admission is None:
                return record
            transport, arguments = admission
            # The retained copy can be unhealthy: independent recovery must not
            # require repairing or redeploying that unknown original first.
            baseline = self.read_edge_health(record, expected=transport.old_runtime_identity)
            if baseline.runtime_identity_status == "unchecked":
                from control_plane.contracts.runtime_identity import compare_runtime_identity

                status, detail = compare_runtime_identity(
                    expected=transport.old_runtime_identity,
                    observed=baseline.observed_runtime_identity,
                )
                baseline = baseline.model_copy(
                    update={"runtime_identity_status": status, "runtime_identity_detail": detail}
                )
            route = compile_testing_route(new_backend=readiness.backend, **arguments)
            transport.payload["expected_application_file_sha256"] = route.application_file_sha256
            readiness = readiness.model_copy(
                update={
                    "route": route,
                    "route_execution": transport.execution_intent(
                        route, route.new_backend, operation_id=record.plan_sha256
                    ),
                    "old_runtime_identity": record.predecessor.runtime_identity
                    if record.predecessor
                    else None,
                    "predecessor_health": baseline,
                }
            )
            record = self.save(record, application_readiness=readiness)
        else:
            transport = application_route_transport(self.root, record)
        self.route_effect(record, lease, checkpoint, transport, inverse=False)
        health = qualify_edge_health(
            lambda: self.read_edge_health(
                record, prospective=True, expected=readiness.runtime_identity
            ),
            readiness.runtime_identity,
        )
        predecessor = readiness.predecessor_health
        predecessor_qualified = (
            predecessor is not None
            and predecessor.verified
            and predecessor.status == "pass"
            and predecessor.runtime_identity_status == "match"
        )
        if health.status != "pass" and not predecessor_qualified:
            # An unhealthy original or an unqualified access gate cannot
            # attribute a nonqualifying edge reply to this independent copy.
            raise TestingActivationRefusal(
                "The candidate edge remains unqualified; continue the original key."
            )
        if not health.verified or health.status == "pending":
            raise TestingActivationRefusal("The original edge health response remains unknown.")
        record = self.save(
            record,
            application_readiness=readiness.model_copy(
                update={
                    "public_health": health,
                    "observed_at": self.store.mutation_observation_timestamp(),
                }
            ),
        )
        if health.status != "pass":
            raise TestingActivationRefusal(
                "The observed edge candidate failed qualification.",
                code=TestingActivationRefusalCode.RECIPIENT_UNQUALIFIED,
            )
        return record

    def route_effect(
        self,
        record: TestingActivationRecord,
        lease: ProviderOperationLease,
        checkpoint: Callable[[str], None],
        transport: Any,
        *,
        inverse: bool,
        observe_only: bool = False,
    ) -> None:
        """Use one original child and physical writer per route direction."""
        from control_plane.dokploy.testing_route import observe_testing_route, route_document
        from control_plane.testing_route_effects import (
            TestingRouteEffects,
            TestingRouteFenceController,
        )
        from control_plane.provider_operations import run_durable_provider_operation

        assert isinstance(lease, ProviderEvidenceLease)
        readiness = record.application_readiness
        assert readiness is not None and readiness.route is not None
        route = readiness.route
        assert readiness.route_execution is not None
        desired = route.old_backend if inverse else route.new_backend
        suffix = ":route-inverse" if inverse else ":route"
        path = f"/v1/products/{record.product}/testing-activation" + suffix
        key = record.plan_sha256 + suffix
        prior = self.store.read_idempotency_record(
            scope=record.scope, route_path=path, idempotency_key=key
        )
        if inverse and prior is not None:
            from control_plane.dokploy.testing_route_execution import TestingRouteExecutionIntent

            execution = TestingRouteExecutionIntent.model_validate(
                self.store.read_original_mutation_intent(prior).get("route_execution")
            )
            expected = readiness.route_execution.model_copy(
                update={
                    "backend": route.old_backend,
                    "document_sha256": canonical_json_sha256(
                        route_document(route, route.old_backend)
                    ),
                }
            )
            if execution.model_dump(exclude={"custody"}) != expected.model_dump(
                exclude={"custody"}
            ):
                raise TestingActivationRefusal("The original inverse intent changed.")
        else:
            execution = (
                transport.execution_intent(route, desired, operation_id=record.plan_sha256)
                if inverse
                else readiness.route_execution
            )
        if observe_only and prior is None:
            return

        def present() -> bool:
            frame = transport.read()
            owned = json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None
            return observe_testing_route(route, frame["raw"], owned) == desired

        def recovery_guard() -> None:
            lease.assert_current()
            self.require_recovery_unused(record)
            require_retained_route_authority(self.store, record)

        def guard() -> None:
            recovery_guard()
            if not inverse:
                self.require_unchanged(record)
                self.require_private_ingress(record)
                self.verify_provider(record)

        effects = TestingRouteEffects(
            controller=TestingRouteFenceController(
                store=self.store,
                transport=transport,
                execution=execution,
                guard=guard,
                checkpoint=checkpoint,
                lease_owner=lambda: lease.evidence_reservation().lease_owner,
            ),
            phase="application-route-inverse" if inverse else "application-route",
            present=present,
            publish=lambda attempt, dispatch: transport.publish(
                route,
                desired,
                expected_backend=route.new_backend if inverse else route.old_backend,
                dispatch_guard=dispatch,
                desired_runtime_identity=readiness.old_runtime_identity
                if inverse
                else readiness.runtime_identity,
                execution=attempt.execution,
                execution_key=attempt.key,
                writer_attempt=attempt,
            ),
            recovery_guard=recovery_guard,
            inverse=inverse,
        )

        class Route:
            def target_key(inner) -> str:
                return "testing-activation-route:" + record.product + ":" + record.context

            def reconciliation_key(inner) -> str:
                return route.digest

            def apply(
                inner, provider_operation_key: str, child: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                return effects.apply(child)

            def observe(
                inner,
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> ProviderObservation:
                return (
                    effects.observe(provider_effect_phase, prior.attempt)
                    if prior is not None
                    else ProviderObservation("unknown")
                )

        recovery_guard() if observe_only else guard()
        outcome = run_durable_provider_operation(
            store=self.store,
            scope=record.scope,
            route_path=path,
            idempotency_key=key,
            request_fingerprint=route.digest,
            lease_owner=lease.evidence_reservation().lease_owner,
            response_trace_id=self.trace_id,
            adapter=Route(),
            allow_mutation=not observe_only,
            original_intent={"route_execution": execution.model_dump(mode="json")},
        )
        if outcome.status not in {"completed", "adopted", "replayed"}:
            raise TestingActivationRefusal(
                "The original addressed route writer remains unobserved."
            )
        if not observe_only and not present():
            raise TestingActivationRefusal("The original addressed route remains unobserved.")

    def restore_application_route(
        self,
        record: TestingActivationRecord,
        lease: ProviderOperationLease,
        check: Callable[[], None],
    ) -> None:
        """Settle the original forward writer before restoring its captured predecessor."""
        from control_plane.testing_application_private_route import application_route_transport

        assert isinstance(lease, ProviderEvidenceLease)
        readiness = record.application_readiness
        assert readiness is not None and readiness.route is not None
        transport = application_route_transport(self.root, record)

        def checkpoint(phase: str) -> None:
            check()
            lease.assert_current()
            self.require_recovery_unused(record)
            require_retained_route_authority(self.store, record)
            lease.checkpoint_effect(phase)

        # Observation settles the exact original writer; it cannot replay forward
        # work under corrected authority. A still-running writer blocks inverse.
        self.route_effect(record, lease, checkpoint, transport, inverse=False, observe_only=True)
        self.route_effect(record, lease, checkpoint, transport, inverse=True)

    def publish_application(
        self,
        record: TestingActivationRecord,
        checkpoint: Callable[[str], None],
    ) -> TestingActivationRecord:
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
        from control_plane.contracts.route_binding_record import (
            EnvironmentRouteBindingRecord,
            RouteBindingDomain,
            RouteBindingIngress,
            RouteBindingProviderTarget,
            RouteBindingSource,
            RouteBindingTls,
        )
        from control_plane.storage.product_authority_bundle import (
            ProviderTargetWrite,
        )

        readiness = record.application_readiness
        if (
            readiness is None
            or readiness.private_health is None
            or readiness.private_health.status != "pass"
        ):
            raise TestingActivationRefusal("The actual fresh runtime is not ready.")
        self.require_private_ingress(record)
        checkpoint("publish_application_recipient")
        stamp = utc_now_timestamp()
        target = record.retained_target.model_copy(
            update={
                "project_name": record.namespace,
                "target_name": record.namespace,
                "source_type": "docker",
                "compose_path": "",
                "custom_git_url": "",
                "custom_git_branch": "",
                "env": {},
                "updated_at": stamp,
                "source_label": "service:testing-activation",
                "pending_public_host_effects": (),
                **({"domains": (), "public_hosts": ()} if record.retained_private_ingress else {}),
            }
        )
        target_id = DokployTargetIdRecord(
            context=record.context,
            instance="testing",
            target_id=record.target_id,
            updated_at=stamp,
            source_label="service:testing-activation",
        )
        provider = ProviderTargetRecord.from_dokploy_records(
            target_record=target, target_id_record=target_id
        )
        binding = None
        if readiness.route is not None and not record.retained_private_ingress:
            old = record.retained_route_binding
            binding = EnvironmentRouteBindingRecord(
                product=record.product,
                context=record.context,
                instance="testing",
                provider_target=RouteBindingProviderTarget(
                    provider_id="dokploy",
                    target_category="application",
                    provider_target_type="application",
                    target_name=record.namespace,
                ),
                domains=old.domains
                if old
                else (RouteBindingDomain(domain_name=readiness.route.host),),
                ingress=old.ingress
                if old
                else RouteBindingIngress(provider="traefik", termination_kind="direct"),
                tls=old.tls if old else RouteBindingTls(owner="provider"),
                source=RouteBindingSource(
                    source_kind="service",
                    source_label="service:testing-activation",
                    source_record_ids=(record.plan_sha256,),
                    source_versions={
                        "testing_activation": record.plan_sha256,
                        "testing_route": readiness.route.digest,
                    },
                    refreshed_at=stamp,
                ),
                updated_at=stamp,
            )
        from control_plane.testing_application_credentials import runtime_activation_bundle

        credential_bundle = runtime_activation_bundle(self.store, record)
        published = record.model_copy(
            update={
                "published": True,
                "qualified_at": stamp,
                "phase": "qualified",
                "published_target": target,
                "published_route_binding": binding,
                "generated_configuration_sha256": configuration_digest(
                    self.store, record.product, record.context, generated=credential_bundle
                )
                if credential_bundle.secret_records
                else "",
            }
        )
        self.store.write_testing_activation(
            expected=record.model_dump(mode="json"),
            replacement=published.model_dump(mode="json"),
            bundle=credential_bundle.model_copy(
                update={
                    "required_context_owner": (record.product, record.context),
                    "expected_product_profiles": (
                        self.store.read_product_profile_record(record.product),
                    ),
                    "dokploy_targets": (target,),
                    "dokploy_target_ids": (target_id,),
                    "provider_target_writes": (
                        ProviderTargetWrite(
                            record=provider, expected_record=record.retained_provider
                        ),
                    ),
                }
            ),
        )
        return published

    def result(self, record: TestingActivationRecord) -> dict[str, Any]:
        if record.failure is not None:
            return TestingActivationRefusedResult(
                product=record.product,
                context=record.context,
                plan_sha256=record.plan_sha256,
                source_commit=record.request.source_commit,
                resource_namespace=record.namespace,
                image_reference=record.image_reference,
                host_ports=record.host_ports,
                reason=record.failure.kind,
                deployment_requested=bool(
                    record.state_recipient.get("application_deployment_id")
                    or record.failure.kind == "candidate_deployment_failed"
                ),
            ).model_dump(mode="json")
        return {**super().result(record), "deployment_requested": True}

    def failed_candidate(
        self, record: TestingActivationRecord, provider_operation_key: str
    ) -> TestingActivationFailure | None:
        """Observe only this original's terminal private job, never generic health errors."""
        if (
            record.published
            or record.application_readiness is not None
            and record.application_readiness.route is not None
        ):
            return None
        if record.phase == "deploy_application_deployment_id":
            target_type, target_id, suffix = "application", record.target_id, " application"
            live = self.live(record)
        else:
            return None
        if not target_id or live.get("domains") != []:
            return None
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        history = api.list_deployments_for_target(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
            require_complete=True,
        )
        if len(history) != 1:
            return None
        observed = history[0]
        if (
            observed.get("title") != provider_operation_title(provider_operation_key) + suffix
            or not api.deployment_key(observed)
            or api.deployment_status(observed) not in api._DEPLOYMENT_FAILURE_STATUSES
        ):
            return None
        return TestingActivationFailure(
            kind="candidate_deployment_failed",
            target_type=cast(Any, target_type),
            target_id=target_id,
            deployment_id=api.deployment_key(observed),
            deployment_status=api.deployment_status(observed),
        )

    def require_unstarted_candidate(self, record: TestingActivationRecord) -> None:
        """Read back the original private placement without replaying any effect."""
        live = self.live(record)
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        old = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="application",
            target_id=record.retained_target_id.target_id,
        )
        if (
            live.get("serverId") != old.get("serverId")
            or live.get("domains") != []
            or live.get("deployments") != []
            or live.get("applicationStatus") != "idle"
            or api.list_deployments_for_target(
                host=host,
                token=token,
                target_type="application",
                target_id=record.target_id,
                require_complete=True,
            )
        ):
            raise TestingActivationRefusal(
                "The original unused candidate is not proven private and idle."
            )
        if record.state_recipient.get("mount_verified") == "true":
            resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
            assert isinstance(resources, ApplicationTestingResources)
            if not self.mount_matches(record, live, resources):
                raise TestingActivationRefusal("The answered original mount changed.")
        elif live.get("mounts") != []:
            raise TestingActivationRefusal("An unrequested candidate mount appeared.")
        if (
            record.state_recipient.get("application_configured") == "true"
            or live.get("sourceType") == "docker"
        ):
            if (
                live.get("sourceType") != "docker"
                or live.get("dockerImage") not in {None, "", record.image_reference}
                or live.get("autoDeploy") is not False
            ):
                raise TestingActivationRefusal("The answered original candidate source changed.")
            self.verify_provider_options(record, live)
        else:
            require_fresh_application_runtime(
                live, environment_id=record.environment_id, project_id=record.project_id
            )

    def require_terminal_candidate(
        self, record: TestingActivationRecord, operation_key: str
    ) -> None:
        """Retain running private recipients; only their original deployment jobs must be closed."""
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        targets = [
            (
                "application",
                record.target_id,
                " application",
                "application_deployment_id",
                self.live(record),
            )
        ]
        for kind, target_id, suffix, key, live in targets:
            history = api.list_deployments_for_target(
                host=host, token=token, target_type=kind, target_id=target_id, require_complete=True
            )
            if (
                live.get("domains") != []
                or len(history) != 1
                or history[0].get("title") != provider_operation_title(operation_key) + suffix
                or api.deployment_key(history[0]) != record.state_recipient.get(key)
                or api.deployment_status(history[0])
                not in api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES
            ):
                raise TestingActivationRefusal(
                    "The original private deployment custody remains unobserved."
                )

    def refuse(
        self, record: TestingActivationRecord, failure: TestingActivationFailure
    ) -> ProviderMutationOutcome:
        replacement = self.save(record, phase="refused", failure=failure)
        return ProviderMutationOutcome(
            202,
            provider_operation_response_payload(
                trace_id=self.trace_id, records={}, result=self.result(replacement)
            ),
        )

    def require_recovery_unused(self, record: TestingActivationRecord) -> None:
        protection = self.store.read_disposable_lane_safety(
            record.product, record.context, "testing"
        )
        if protection is not None and protection.usage == "in_use":
            raise TestingActivationRefusal("Staff use pauses original route recovery.")

    def candidate_refusal_ready(self, record: TestingActivationRecord) -> bool:
        """Only a deliberate positive refusal may abandon an original candidate."""
        if record.published:
            return False
        self.require_recovery_unused(record)
        if record.phase in {"restoring_application_route", "refusing_private_candidate"}:
            return True
        if completed_application_forward_exclusion(self.store, record):
            return True
        readiness = record.application_readiness
        if readiness is not None and any(
            health is not None and health.verified and health.status == "fail"
            for health in (readiness.private_health, readiness.public_health)
        ):
            return True
        try:
            self.require_unchanged(record)
            self.require_private_ingress(record)
        except TestingActivationRefusal as refusal:
            return refusal.code == TestingActivationRefusalCode.AUTHORITY_CHANGED
        if (
            readiness is not None
            and readiness.route is None
            and self.store.read_idempotency_record(
                scope=record.scope,
                route_path=f"/v1/products/{record.product}/testing-activation:route",
                idempotency_key=record.plan_sha256 + ":route",
            )
            is None
        ):
            try:
                self.preflight_application_route(record)
            except ApplicationRouteAdmissionContradiction:
                return True
        return False

    def refuse_after_failure(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome | None:
        current = activation_record(
            self.store,
            self.product,
            self.request.context,
            scope=self.scope,
            preparation_key=self.request.preparation_key,
        )
        if current is None or current.failure is not None:
            return None
        assert isinstance(lease, ProviderEvidenceLease)
        if (
            undispatched_application_recipient(current, lease.evidence_reservation())
            and lease.evidence_reservation().provider_effect_phase
        ):
            return self.refuse(
                current, TestingActivationFailure(kind="authority_refused_before_recipient")
            )
        failure = self.failed_candidate(current, provider_operation_key)
        if failure is not None:
            return self.refuse(current, failure)
        if self.candidate_refusal_ready(current):
            from control_plane.workflows.generic_web_deploy_provider import (
                generic_web_provider_target_key,
            )
            from control_plane.testing_event_deploy_recovery import (
                testing_event_execution_lock_name,
            )

            key = generic_web_provider_target_key(
                provider_id="dokploy",
                context=current.context,
                instance="testing",
                target_type="application",
                target_id=current.retained_target_id.target_id,
            )
            with self.store.testing_replacement_execution_lock(
                testing_event_execution_lock_name(key)
            ) as check:
                if check is None:
                    return None
                require_retained_route_authority(self.store, current)
                lease.assert_current()
                check()
                self.require_recovery_unused(current)
                readiness = current.application_readiness
                unstarted = unstarted_application_recipient(current, lease.evidence_reservation())
                if readiness is None and not unstarted:
                    return None
                if readiness is not None and readiness.route is not None:
                    if current.phase != "restoring_application_route":
                        current = self.save(current, phase="restoring_application_route")
                    self.restore_application_route(current, lease, check)
                else:
                    # Before a private-health result, require the answered original
                    # application job; unresolved create/mount/bootstrap phases stay held.
                    if unstarted:
                        self.require_unstarted_candidate(current)
                    else:
                        self.require_terminal_candidate(current, provider_operation_key)
                        if not current.state_recipient.get("application_deployment_id"):
                            return None
                    if current.phase != "refusing_private_candidate":
                        current = self.save(current, phase="refusing_private_candidate")
                # A foreign or unknown candidate job may hold this receipt, but
                # cannot prevent restoring the proven original serving route.
                if unstarted:
                    self.require_unstarted_candidate(current)
                else:
                    self.require_terminal_candidate(current, provider_operation_key)
                return self.refuse(current, TestingActivationFailure(kind="publication_refused"))
        return None

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        from control_plane.workflows.generic_web_deploy_provider import (
            generic_web_provider_target_key,
        )
        from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
        from control_plane.testing_application_capabilities import (
            inspect_retained_application_capabilities,
        )

        started = False
        with self.evidence(lease):
            record = self.current()
            if (
                record.phase in {"restoring_application_route", "refusing_private_candidate"}
                or completed_application_forward_exclusion(self.store, record)
            ) or (
                record.application_readiness is not None
                and any(
                    health is not None and health.verified and health.status == "fail"
                    for health in (
                        record.application_readiness.private_health,
                        record.application_readiness.public_health,
                    )
                )
            ):
                try:
                    outcome = self.refuse_after_failure(provider_operation_key, lease)
                    if outcome is None:
                        raise TestingActivationRefusal("Original recovery remains unobserved.")
                    return outcome
                except Exception as recovery_error:
                    raise ProviderMutationUnknownError(
                        "Continue the original recovery only."
                    ) from recovery_error
            try:
                if record.plan_sha256 != self.request.reviewed_plan_sha256:
                    raise TestingActivationRefusal("Review this exact original activation plan.")
                if record.failure is not None:
                    return ProviderMutationOutcome(
                        202,
                        provider_operation_response_payload(
                            trace_id=self.trace_id, records={}, result=self.result(record)
                        ),
                    )
                key = generic_web_provider_target_key(
                    provider_id="dokploy",
                    context=record.context,
                    instance="testing",
                    target_type="application",
                    target_id=record.retained_target_id.target_id,
                )
                with self.store.testing_replacement_execution_lock(
                    testing_event_execution_lock_name(key)
                ) as old_check:
                    if old_check is None:
                        raise TestingActivationRefusal("The retained original executor is active.")

                    def checkpoint(phase: str) -> None:
                        nonlocal started
                        old_check()
                        lease.assert_current()
                        self.require_unchanged(record)
                        lease.checkpoint_effect(phase)
                        started = True

                    self.require_unchanged(record)
                    if record.published:
                        self.verify_provider(record)
                    else:
                        require_retained_application_custody(
                            self.store,
                            product=record.product,
                            context=record.context,
                            check=old_check,
                        )
                        if record.phase in {
                            "create_project",
                            "create_environment",
                            "create_application",
                        }:
                            recovered = self.recover_placement(record)
                            if recovered != record:
                                record = self.save(
                                    record,
                                    **recovered.model_dump(
                                        include={
                                            "project_id",
                                            "environment_id",
                                            "target_id",
                                            "phase",
                                        }
                                    ),
                                )
                        predecessor = self.preflight_application_route(record)
                        if record.predecessor is None and predecessor is not None:
                            record = self.save(record, predecessor=predecessor)
                        inspect_retained_application_capabilities(
                            store=self.store,
                            root=self.root,
                            product=record.product,
                            context=record.context,
                            namespace=record.namespace,
                            before_effect=lambda _phase: checkpoint(APPLICATION_CAPABILITY_PHASE),
                        )
                        resources, _ = preparation_resources(
                            self.store, self.product, self.request, self.scope
                        )
                        assert isinstance(resources, ApplicationTestingResources)
                        record = self.create_placement(record, checkpoint)
                        record = self.prepare_credentials(record)
                        record = self.configure_application(record, resources, checkpoint)
                        record = self.deploy_owned_target(
                            record,
                            target_type="application",
                            target_id=record.target_id,
                            title=provider_operation_title(provider_operation_key) + " application",
                            state_key="application_deployment_id",
                            checkpoint=checkpoint,
                        )
                        record = self.qualify_application(record)
                        record = self.switch_application_route(record, lease, checkpoint)
                        record = self.publish_application(record, checkpoint)
                    return ProviderMutationOutcome(
                        202,
                        provider_operation_response_payload(
                            trace_id=self.trace_id, records={}, result=self.result(record)
                        ),
                    )
            except Exception as error:
                try:
                    refused = self.refuse_after_failure(provider_operation_key, lease)
                except Exception as recovery_error:
                    raise ProviderMutationUnknownError(
                        "Original recovery requires observation."
                    ) from recovery_error
                if refused is not None:
                    return refused
                if not started:
                    raise ProviderMutationRejectedError(
                        error
                        if isinstance(error, TestingActivationRefusal)
                        else ValueError("Activation refused before provider effects.")
                    ) from error
                raise ProviderMutationUnknownError(
                    "Retain the original application activation key, recipients and unknown predecessor custody."
                ) from error

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        # Observation cannot create/configure/deploy or mint a physical writer.
        # A positive completed publication is adopted; all interrupted work uses
        # the same original apply after its phase-specific positive readbacks.
        try:
            record = self.current()
            if record.plan_sha256 != reconciliation_key:
                return ProviderObservation("unknown")
            if record.failure is not None:
                return ProviderObservation(
                    "present",
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace_id, records={}, result=self.result(record)
                    ),
                )
            parent = self.store.read_testing_application_activation_operation(
                self.product, self.request.context, self.scope, self.request.preparation_key
            )
            if parent is not None and (
                undispatched_application_recipient(record, parent)
                or self.failed_candidate(record, provider_operation_key) is not None
            ):
                # Apply owns a fresh lease for the receipt-only refusal, even
                # when current authority permanently refuses forward effects.
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
            if self.candidate_refusal_ready(record):
                require_retained_route_authority(self.store, record)
                if parent is not None and unstarted_application_recipient(record, parent):
                    self.require_unstarted_candidate(record)
                elif record.application_readiness is None:
                    return ProviderObservation("unknown")
                elif record.application_readiness.route is None:
                    self.require_terminal_candidate(record, provider_operation_key)
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
            self.require_unchanged(record)
            if record.published:
                self.verify_provider(record)
                return ProviderObservation(
                    "present",
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace_id, records={}, result=self.result(record)
                    ),
                )
            if not provider_effect_phase:
                return ProviderObservation("absent", retry_safe=True)
            # Each uncertain create needs positive original namespace evidence.
            # Subsequent fixed configuration/readback steps keep their original
            # identities; their apply paths refuse ambiguous jobs or schedules.
            if record.phase in {"create_project", "create_environment", "create_application"}:
                self.recover_placement(record)
            if record.phase == "create_application_mount":
                live = self.live(record)
                specification = record.request.application
                assert specification is not None
                resources, _ = preparation_resources(
                    self.store, self.product, self.request, self.scope
                )
                assert isinstance(resources, ApplicationTestingResources)
                if not self.mount_matches(record, live, resources):
                    return ProviderObservation("unknown")
            return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)
        except Exception:
            return ProviderObservation("unknown")


def activation_image_record(record: TestingActivationRecord) -> Any:
    """Record the actual image/readiness operation without a product qualification pass."""
    from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
    from control_plane.contracts.promotion_record import (
        ArtifactIdentityReference,
        DeploymentEvidence,
        PostDeployUpdateEvidence,
    )

    readiness = record.application_readiness
    if readiness is None or not record.state_recipient.get("application_deployment_id"):
        raise TestingActivationRefusal("The original image deployment is unavailable.")
    return DeploymentRecord(
        record_id=readiness.runtime_identity.deployment_record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=record.image_reference),
        context=record.context,
        instance="testing",
        source_git_ref=record.request.source_commit,
        runtime_identity=readiness.runtime_identity,
        runtime_source={"testing_activation": record.plan_sha256},
        resolved_target=ResolvedTargetEvidence(
            target_type="application", target_id=record.target_id, target_name=record.namespace
        ),
        deploy=DeploymentEvidence(
            target_type="application",
            target_name=record.namespace,
            deploy_mode="docker",
            deployment_id=record.state_recipient["application_deployment_id"],
            status="pass",
            finished_at=record.qualified_at,
        ),
        post_deploy_update=PostDeployUpdateEvidence(
            detail="Activation prepares an independent recipient. Product verification remains ordinary deployment work."
        ),
        destination_health=HealthcheckEvidence(status="pending"),
    )
