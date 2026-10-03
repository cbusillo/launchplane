"""Retire an unprovisioned generic-web product without any provider mutation."""

from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

import click

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_retirement import (
    ProductRetirementProviderObservation,
    ProductRetirementRecord,
    ProductRetirementRequest,
    canonical_sha256,
)
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.product_retirement import (
    BoundProductRetirement,
    DokployProductRetirementAdapter,
    ProductRetirementBlockedError,
    ProductRetirementStore,
    authority_snapshot,
    redacted_product_retirement_response,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderObservation,
    ProviderOperationLease,
)
from control_plane.workflows.generic_web_preview import effective_preview_app_name_prefix


class NoTargetRetirementStore(ProductRetirementStore, Protocol):
    def list_provider_target_records(
        self, *, provider_id: str = ""
    ) -> tuple[ProviderTargetRecord, ...]: ...

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]: ...

    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]: ...

    def list_preview_generation_records(
        self, *, preview_id: str = "", limit: int | None = None
    ) -> tuple[PreviewGenerationRecord, ...]: ...

    def commit_no_target_retirement(
        self,
        *,
        bound: BoundProductRetirement,
        terminal: ProductRetirementRecord,
    ) -> None: ...


def bind_no_target_retirement(
    *, record_store: ProductRetirementStore, request: ProductRetirementRequest
) -> BoundProductRetirement:
    store = cast(NoTargetRetirementStore, record_store)
    profile = store.read_product_profile_record(request.product)
    if (
        not request.no_target
        or profile.driver_id != "generic-web"
        or profile.lifecycle_state != "active"
        or len(profile.lanes) != 1
        or profile.lanes[0].instance != request.instance
    ):
        raise ProductRetirementBlockedError(
            "No-target retirement requires one active generic-web lane."
        )
    context = profile.lanes[0].context
    contexts = {context, profile.preview.context, *profile.historical_contexts} - {""}
    for other in store.list_product_profile_records():
        if other.product != profile.product and contexts.intersection(
            {lane.context for lane in other.lanes}
            | {other.preview.context, *other.historical_contexts}
        ):
            raise ProductRetirementBlockedError("No-target retirement context is shared.")
    if any(target.context in contexts for target in store.list_provider_target_records()) or any(
        target.context in contexts for target in store.list_dokploy_target_id_records()
    ):
        raise ProductRetirementBlockedError("No-target retirement found tracked target authority.")
    target = store.read_dokploy_target_record(context_name=context, instance_name=request.instance)
    if target.target_type != "application" or not target.target_name.strip():
        raise ProductRetirementBlockedError("No-target retirement requires an application name.")
    if any(
        candidate.context in contexts and candidate != target
        for candidate in store.list_dokploy_target_records()
    ):
        raise ProductRetirementBlockedError("No-target retirement found additional target config.")
    previews = tuple(
        sorted(
            (
                PreviewRecord.model_validate(preview)
                for preview in store.list_preview_records(anchor_repo=profile.repository)
            ),
            key=lambda preview: preview.preview_id,
        )
    )
    if any(
        preview.context != profile.preview.context
        or preview.active_generation_id
        or preview.serving_generation_id
        or preview.latest_generation_id
        or store.list_preview_generation_records(preview_id=preview.preview_id)
        for preview in previews
    ):
        raise ProductRetirementBlockedError(
            "No-target retirement found preview generation evidence."
        )
    # This path only ends failed provisioning; it does not delete runtime data or secrets.
    if any(
        store.list_runtime_environment_records(context_name=name)
        or store.list_secret_records(context_name=name)
        for name in contexts - {context}
    ):
        raise ProductRetirementBlockedError(
            "No-target retirement found historical runtime authority."
        )
    return BoundProductRetirement(
        profile=profile,
        context=context,
        provider_target=None,
        dokploy_target=target,
        dokploy_target_id=None,
        runtime_records=store.list_runtime_environment_records(context_name=context),
        secret_records=store.list_secret_records(context_name=context),
        previews=previews,
    )


def _absence_scope(bound: BoundProductRetirement) -> dict[str, object]:
    return {
        "repository": bound.profile.repository,
        "image_repository": bound.profile.image.repository,
        "application_name": bound.dokploy_target.target_name,
        "preview_prefix": effective_preview_app_name_prefix(profile=bound.profile) + "-",
        "domains": sorted(
            {
                *bound.dokploy_target.domains,
                *(
                    urlsplit(url).hostname or ""
                    for url in (
                        bound.profile.lanes[0].base_url,
                        *(preview.canonical_url for preview in bound.previews),
                    )
                    if url
                ),
            }
        ),
    }


def absence_scope_sha256(bound: BoundProductRetirement) -> str:
    return canonical_sha256(_absence_scope(bound))


def observe_no_target_absence(
    *, control_plane_root: Path, bound: BoundProductRetirement, observed_at: str
) -> ProductRetirementProviderObservation:
    host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
    applications = dokploy_api.search_dokploy_applications(host=host, token=token)
    scope = _absence_scope(bound)
    domains = cast(list[str], scope["domains"])
    for application in applications:
        target_id = str(application.get("applicationId") or application.get("id") or "").strip()
        if not target_id:
            raise ProductRetirementBlockedError("Provider inventory contains no application id.")
        # Search is only enumeration; inspect each application and its domains for absence proof.
        payload = dokploy_api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="application", target_id=target_id
        )
        name = str(payload.get("name") or "").strip()
        if not name or str(payload.get("applicationId") or payload.get("id") or "") != target_id:
            raise ProductRetirementBlockedError(
                "Provider inventory application evidence is incomplete."
            )
        repository_values = (
            str(payload.get("customGitUrl") or ""),
            str(payload.get("repository") or ""),
        )
        repository = bound.profile.repository.lower()
        image = str(payload.get("dockerImage") or "").strip()
        image_repository = bound.profile.image.repository
        if (
            name == bound.dokploy_target.target_name
            or name.startswith(str(scope["preview_prefix"]))
            or any(repository in value.lower() for value in repository_values)
            or image == image_repository
            or image.startswith((image_repository + ":", image_repository + "@"))
        ):
            raise ProductRetirementBlockedError(
                "Provider still holds an application for the product."
            )
        for domain in dokploy_api.fetch_dokploy_application_domains(
            host=host, token=token, application_id=target_id
        ):
            domain_host = str(domain.get("host") or domain.get("domain") or "").strip().lower()
            if not domain_host or domain_host in domains:
                raise ProductRetirementBlockedError("Provider domain evidence blocks retirement.")
    inventory_ids = sorted(str(app.get("applicationId") or app.get("id")) for app in applications)
    verified_inventory_ids = sorted(
        str(app.get("applicationId") or app.get("id"))
        for app in dokploy_api.search_dokploy_applications(host=host, token=token)
    )
    if inventory_ids != verified_inventory_ids:
        raise ProductRetirementBlockedError("Provider inventory changed during absence proof.")
    return ProductRetirementProviderObservation(
        observed_at=observed_at,
        target_id="",
        target_id_sha256="",
        state="absent",
        retirable=False,
        no_target=True,
        absence_scope_sha256=absence_scope_sha256(bound),
        inventory_sha256=canonical_sha256(inventory_ids),
    )


class NoTargetProductRetirementAdapter(DokployProductRetirementAdapter):
    def target_key(self) -> str:
        return f"product-retirement:no-target:{self._plan.product}"

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase, reconciliation_key
        # A committed terminal record is atomic with both the profile and preview writes.
        records = self._record_store.list_product_retirement_records(product=self._plan.product)
        for record in records:
            if record.plan_sha256 == self._plan.plan_sha256 and record.outcome == "retired":
                return ProviderObservation(
                    outcome="present",
                    response_payload=redacted_product_retirement_response(record),
                )
        return ProviderObservation(outcome="absent", retry_safe=True)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        self._write_started_record(provider_operation_key)
        try:
            bound = bind_no_target_retirement(
                record_store=self._record_store, request=self._request
            )
            if authority_snapshot(bound) != self._plan.authority_snapshot:
                raise ProductRetirementBlockedError("No-target authority changed after planning.")

            observation = observe_no_target_absence(
                control_plane_root=self._control_plane_root,
                bound=bound,
                observed_at=self._requested_at,
            )
            if (
                observation.absence_scope_sha256
                != self._plan.provider_observation.absence_scope_sha256
            ):
                raise ProductRetirementBlockedError("No-target absence scope changed.")

            terminal = self._build_terminal_record(
                outcome="retired",
                provider_operation_key=provider_operation_key,
                provider_absence_verified=True,
            )
            terminal = terminal.model_copy(
                update={
                    "provider_observation": observation,
                    "mutation_evidence": terminal.mutation_evidence.model_copy(
                        update={
                            "lifecycle_after": "retired",
                            "closed_preview_ids": tuple(
                                preview.preview_id
                                for preview in bound.previews
                                if preview.state != "destroyed"
                            ),
                        }
                    ),
                }
            )
            lease.assert_current()
            cast(NoTargetRetirementStore, self._record_store).commit_no_target_retirement(
                bound=bound, terminal=terminal
            )
        except (ValueError, OSError, click.ClickException, TimeoutError) as error:
            raise ProviderMutationRejectedError(error) from error
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=redacted_product_retirement_response(terminal),
            provider_effect_performed=False,
        )
