"""Reviewed installation/removal of private testing Compose source.

Deployment remains with the existing immutable-artifact driver. This operation
never reads a private key or a managed runtime value, and never deploys.
"""

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Literal, cast

import click
from pydantic import Field, field_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.runtime_identity import COMPOSE_IMAGE_REFERENCE_ENV_KEYS
from control_plane.dokploy import api, source
from control_plane.generic_web_testing_exclusion import _bind, _inventory
from control_plane.dokploy.target_source_setup import repository_source_url
from control_plane.private_testing_transport import (
    PrivateTestingTransportIntent,
    PrivateTestingTransportRefused,
    _object,
    _validate_compose,
    transport_overlay,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.runtime_platform_credentials import looks_like_credential
from control_plane.testing_activation import read_product_compose, verified_product_image
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key

ROUTE = "/v1/products/{product}/private-testing-transport/activation"
_SOURCE_FIELDS = (
    "sourceType",
    "composePath",
    "composeFile",
    "customGitUrl",
    "customGitBranch",
    "autoDeploy",
)
_PLACEMENT_FIELDS = (
    "composeId",
    "serverId",
    "appName",
    "environmentId",
    "name",
    "composeType",
    "isolatedDeployment",
    "randomize",
    "serviceNetworks",
    "mounts",
    "domains",
)


class PrivateTestingActivationRequest(PrivateTestingTransportIntent):
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    expected_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    operation: Literal["install", "refresh", "remove"] = "install"
    original_activation_key: str = ""
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_path: str = Field(pattern=r"^[A-Za-z0-9._/-]+$")
    certificate_root: str = Field(pattern=r"^/[A-Za-z0-9._/-]+$")
    host_review: str = Field(min_length=1, max_length=1000)
    custody_review: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("source_path", "certificate_root")
    @classmethod
    def _paths(cls, value: str) -> str:
        if ".." in value.split("/") or "//" in value or value.endswith("/"):
            raise ValueError("Custody/source path must be canonical and non-traversing.")
        if value in {"/", "."}:
            raise ValueError("Custody/source path must name a dedicated location.")
        return value


def _source_settings(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in _SOURCE_FIELDS}


def _qualify_template(document: dict[str, Any], runtime_service: str) -> None:
    _validate_compose(document, runtime_service)

    def managed_image(image: object) -> bool:
        return isinstance(image, str) and any(
            re.fullmatch(r"\$\{" + re.escape(key) + r"(?::?\?[^}]*)?\}", image)
            for key in COMPOSE_IMAGE_REFERENCE_ENV_KEYS
        )

    if not managed_image(document["services"][runtime_service].get("image")):
        raise PrivateTestingTransportRefused(
            "Runtime must consume the deployment driver's immutable image."
        )
    for service in document["services"].values():
        image = service.get("image", "")
        if not isinstance(image, str) or not (
            managed_image(image)
            or re.fullmatch(r"[a-z0-9][a-z0-9./:_-]+@sha256:[a-f0-9]{64}", image)
        ):
            raise PrivateTestingTransportRefused(
                "Every image must use immutable artifact delivery."
            )
        environment = service.get("environment", {})
        if not isinstance(environment, dict):
            raise PrivateTestingTransportRefused("Managed environment must use explicit mapping.")
        for key, value in environment.items():
            if looks_like_credential(key, str(value)) and not (
                isinstance(value, str)
                and re.fullmatch(r"\$\{[A-Z_][A-Z0-9_]*(?:(?::?\?[^}]*)|:-|-)?\}", value)
            ):
                raise PrivateTestingTransportRefused("Source cannot contain a credential literal.")
    _qualify_structural_interpolation(document)


def _qualify_structural_interpolation(document: dict[str, Any]) -> None:
    # Structural interpolation could redirect storage or alter isolation at deploy time.
    for service in document["services"].values():
        structural = {
            key: value for key, value in service.items() if key not in {"image", "environment"}
        }
        if "$" in json.dumps(structural):
            raise PrivateTestingTransportRefused("Structural Compose interpolation is unsupported.")
    if "$" in json.dumps(document.get("volumes", {})):
        raise PrivateTestingTransportRefused("Storage interpolation is unsupported.")


def record_binding(records: tuple[Any, ...]) -> str:
    """Retain actual isolation/ownership, not unrelated profile or display revisions."""
    profile, target, target_id, provider = records
    return canonical_json_sha256(
        {
            "product": profile.product,
            "repository": profile.repository,
            "driver": profile.driver_id,
            "active": profile.is_active,
            "public_website": profile.public_website,
            "runtime_port": profile.runtime_port,
            "health_path": profile.health_path,
            "image_repository": profile.image.repository,
            "target": target.model_dump(
                mode="json",
                include={
                    "context",
                    "instance",
                    "target_type",
                    "domains",
                    "public_hosts",
                    "pending_public_host_effects",
                },
            ),
            "target_id": target_id.target_id,
            "provider": provider.model_dump(
                mode="json", exclude={"updated_at", "source_label", "display_name"}
            ),
        }
    )


def _placement(payload: dict[str, Any], target_id: str) -> dict[str, Any]:
    if (
        payload.get("composeId") != target_id
        or not all(
            isinstance(payload.get(k), str) and payload[k]
            for k in ("serverId", "appName", "environmentId", "name")
        )
        or payload.get("composeType") != "docker-compose"
        or payload.get("isolatedDeployment") is not False
        or payload.get("randomize") is not False
        or payload.get("serviceNetworks") != []
        or payload.get("mounts") != []
        or payload.get("domains") != []
        or payload.get("autoDeploy") is not False
        or payload.get("composeStatus") not in ("idle", "done", "error")
    ):
        raise PrivateTestingTransportRefused(
            "Provider placement or automatic deployment is unqualified."
        )
    return {key: payload[key] for key in _PLACEMENT_FIELDS}


@dataclass
class PrivateTestingActivationAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: PrivateTestingActivationRequest
    scope: str
    trace_id: str
    intent: dict[str, Any]

    def target_key(self) -> str:
        return str(self.intent["target_key"])

    def reconciliation_key(self) -> str:
        return str(self.intent["plan_sha256"])

    @classmethod
    def prepare(
        cls,
        root: Path,
        store: PostgresRecordStore,
        product: str,
        request: PrivateTestingActivationRequest,
        scope: str,
        trace_id: str,
        idempotency_key: str = "",
    ) -> "PrivateTestingActivationAdapter":
        normalized = request.model_dump(mode="json", exclude={"mode", "expected_plan_sha256"})
        if idempotency_key:
            row = store.read_idempotency_record(
                scope=scope,
                route_path=ROUTE.format(product=product),
                idempotency_key=idempotency_key,
            )
            if row is not None:
                intent = store.read_original_mutation_intent(row)
                if (
                    intent.get("request") != normalized
                    or intent.get("plan_sha256") != request.expected_plan_sha256
                ):
                    raise PrivateTestingTransportRefused("Original operation intent differs.")
                return cls(root, store, product, request, scope, trace_id, intent)
        if request.mode == "readback":
            raise PrivateTestingTransportRefused("Original source operation is unavailable.")
        records = _bind(store, product, request.context, request.instance)
        profile, target, target_id, provider = records
        if (
            not profile.is_active
            or profile.public_website != "none"
            or profile.health_path != "/health"
            or not profile.runtime_port
            or target.domains
            or target.public_hosts
            or target.pending_public_host_effects
        ):
            raise PrivateTestingTransportRefused("Only an exact private testing lane is supported.")
        host, token = source.read_dokploy_config(control_plane_root=root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target_id.target_id
        )
        placement = _placement(payload, target_id.target_id)
        before = _source_settings(payload)
        overlay_namespace = (
            "lp-private-tls-"
            + canonical_json_sha256(
                {"product": product, "context": request.context, "instance": request.instance}
            )[:16]
        )
        certificate_directory = request.certificate_root + "/" + overlay_namespace
        installed: dict[str, Any] = {}
        if request.operation in {"remove", "refresh"}:
            original = store.read_idempotency_record(
                scope=scope,
                route_path=ROUTE.format(product=product),
                idempotency_key=request.original_activation_key,
            )
            if original is None or original.state != "completed":
                raise PrivateTestingTransportRefused(
                    "Removal requires the original completed installation."
                )
            installed = store.read_original_mutation_intent(original)
            if (
                installed.get("request", {}).get("operation") not in {"install", "refresh"}
                or installed.get("records_sha256") != record_binding(records)
                or installed.get("placement") != placement
                or installed.get("after") != before
            ):
                raise PrivateTestingTransportRefused(
                    "Removal cannot overwrite changed or foreign source."
                )
        if request.operation == "remove":
            fields = (
                *PrivateTestingTransportIntent.model_fields,
                "source_commit",
                "source_path",
                "certificate_root",
            )
            if any(normalized[name] != installed["request"][name] for name in fields):
                raise PrivateTestingTransportRefused(
                    "Removal must reference the original transport and immutable source."
                )
            after = installed["before"]
            source_digest = installed["source_sha256"]
            image = installed["image"]
        else:
            repository_url = repository_source_url(profile.repository)
            if request.operation == "install" and (
                target.custom_git_branch != request.source_commit
                or target.compose_path != request.source_path
                or target.custom_git_url != repository_url
                or payload.get("sourceType") != "git"
                or payload.get("customGitBranch") != request.source_commit
                or payload.get("customGitUrl") != repository_url
                or payload.get("composePath") != request.source_path
                or payload.get("composeFile") not in (None, "")
            ):
                raise PrivateTestingTransportRefused(
                    "Provider and tracked source must name the exact immutable tree."
                )
            template = _object(
                json.loads(
                    read_product_compose(store, product, request.source_commit, request.source_path)
                )
            )
            _qualify_template(template, request.runtime_service)
            image = verified_product_image(store, product, request.source_commit)
            if not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
                raise PrivateTestingTransportRefused(
                    "Immutable artifact provenance is unavailable."
                )
            source_digest = canonical_json_sha256(template)
            overlay = transport_overlay(
                request, namespace=overlay_namespace, runtime_port=profile.runtime_port
            )
            for name, filename in (("certificate", "server.crt"), ("key", "server.key")):
                cast(dict[str, Any], overlay["secrets"])[overlay_namespace + "-" + name]["file"] = (
                    certificate_directory + "/" + filename
                )
            desired: dict[str, Any] = deepcopy(template)
            for section, additions in overlay.items():
                if set(_object(additions)) & set(_object(desired.get(section, {}))):
                    raise PrivateTestingTransportRefused("Listener namespace is already in use.")
                desired.setdefault(section, {}).update(_object(additions))
            _qualify_structural_interpolation(desired)
            after = {
                **before,
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": json.dumps(desired, sort_keys=True),
            }
        if _bind(store, product, request.context, request.instance) != records:
            raise PrivateTestingTransportRefused("Target changed during planning.")
        intent = {
            "request": normalized,
            "target_id": target_id.target_id,
            "provider_host": host,
            "target_key": generic_web_provider_target_key(
                provider_id=provider.provider_id,
                context=request.context,
                instance=request.instance,
                target_type="compose",
                target_id=target_id.target_id,
            ),
            "records_sha256": record_binding(records),
            "placement": placement,
            "before": before,
            "after": after,
            "environment_sha256": canonical_json_sha256(payload.get("env")),
            "certificate_directory": certificate_directory,
            "source_sha256": source_digest,
            "image": image,
        }
        intent["plan_sha256"] = canonical_json_sha256(intent)
        if request.expected_plan_sha256 and request.expected_plan_sha256 != intent["plan_sha256"]:
            raise PrivateTestingTransportRefused("Reviewed activation source or intent is stale.")
        return cls(root, store, product, request, scope, trace_id, intent)

    def result(self, *, matches: bool) -> dict[str, object]:
        authority = f"{self.request.private_ip}:{self.request.listener_port}"
        return {
            "product": self.product,
            "context": self.request.context,
            "instance": "testing",
            "operation": self.request.operation,
            "plan_sha256": self.intent["plan_sha256"],
            "reviewed_source_sha256": self.intent["source_sha256"],
            "reviewed_source_commit": self.request.source_commit,
            "configuration_matches": matches,
            "deployment_performed": False,
            "transport_verified": False,
            "certificate_verified": False,
            "host_binding_verified": False,
            "endpoint_proposal": {
                "endpoint_key": self.request.endpoint_key,
                "product": self.product,
                "context": self.request.context,
                "instance": "testing",
                "url": f"https://{authority}/health",
            },
            "server_url": f"wss://{authority}/agent-session/connect",
            "trust": {
                "monitor_trust_scope": "endpoint",
                "reviewed_ca_bundle_sha256": self.request.ca_bundle_sha256,
                "verified": False,
                "required_ip_san": str(self.request.private_ip),
                "receiver_ca_bundle_path": self.request.receiver_ca_bundle_path,
            },
            "custody": {
                "directory": self.intent["certificate_directory"],
                "review": self.request.custody_review,
            },
            "next_action": "Use the existing reviewed immutable-artifact testing deployment after runtime inputs and host/TLS prerequisites qualify.",
        }

    def current(self, *, require_environment: bool = False) -> dict[str, Any]:
        records = _bind(self.store, self.product, self.request.context, self.request.instance)
        if record_binding(records) != self.intent["records_sha256"]:
            raise PrivateTestingTransportRefused(
                "Lane records changed; preserve the original operation."
            )
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        if host != self.intent["provider_host"]:
            raise PrivateTestingTransportRefused("Provider binding changed.")
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=self.intent["target_id"]
        )
        if _placement(payload, self.intent["target_id"]) != self.intent["placement"] or (
            require_environment
            and canonical_json_sha256(payload.get("env")) != self.intent["environment_sha256"]
        ):
            raise PrivateTestingTransportRefused("Provider placement or runtime input changed.")
        if record_binding(
            _bind(self.store, self.product, self.request.context, self.request.instance)
        ) != record_binding(records):
            raise PrivateTestingTransportRefused("Lane binding changed during provider evidence.")
        return payload

    def readback(self) -> dict[str, object]:
        current = self.current()
        if _source_settings(current) != self.intent["after"]:
            raise PrivateTestingTransportRefused("Independent provider source readback differs.")
        return {
            **self.result(matches=True),
            "runtime_inputs_match_review": canonical_json_sha256(current.get("env"))
            == self.intent["environment_sha256"],
        }

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        try:
            result = self.readback()
        except (ValueError, OSError, FileNotFoundError, click.ClickException):
            return ProviderObservation("unknown")
        return ProviderObservation(
            "present",
            202,
            provider_operation_response_payload(trace_id=self.trace_id, records={}, result=result),
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        started = False
        try:
            with self.store.testing_replacement_execution_lock(
                testing_event_execution_lock_name(self.target_key())
            ) as check:
                if check is None:
                    raise PrivateTestingTransportRefused("Testing executor is busy.")
                payload = self.current(require_environment=True)
                host, token = source.read_dokploy_config(control_plane_root=self.root)
                for kind, path, query in (
                    (
                        "deployments",
                        "/api/deployment.allByType",
                        {"id": self.intent["target_id"], "type": "compose"},
                    ),
                    ("queue", "/api/deployment.queueList", {}),
                    (
                        "schedules",
                        "/api/schedule.list",
                        {"id": self.intent["target_id"], "scheduleType": "compose"},
                    ),
                ):
                    facet = _inventory(
                        lambda: api.dokploy_request(host=host, token=token, path=path, query=query),
                        kind=kind,
                        target_id=self.intent["target_id"],
                    )
                    if facet.code != "proved":
                        raise PrivateTestingTransportRefused("Provider work is active or unknown.")
                if self.current(require_environment=True) != payload:
                    raise PrivateTestingTransportRefused("Provider changed during exclusion reads.")
                if _source_settings(payload) != self.intent["before"]:
                    raise PrivateTestingTransportRefused("Source changed before apply.")
                check()
                lease.assert_current()
                # Source installation alone never redeploys or changes managed env.
                lease.checkpoint_effect("private_testing_source_update")
                started = True
                api.dokploy_request(
                    host=host,
                    token=token,
                    path="/api/compose.update",
                    method="POST",
                    payload=cast(
                        api.JsonObject,
                        {
                            "composeId": self.intent["target_id"],
                            "name": payload["name"],
                            "environmentId": payload["environmentId"],
                            **self.intent["after"],
                        },
                    ),
                )
                check()
                result = self.readback()
                return ProviderMutationOutcome(
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace_id, records={}, result=result
                    ),
                )
        except Exception as error:
            if not started:
                raise ProviderMutationRejectedError(
                    PrivateTestingTransportRefused("Private testing source refused before effects.")
                ) from error
            raise ProviderMutationUnknownError(
                "Preserve original source, key and independent readback."
            ) from error
