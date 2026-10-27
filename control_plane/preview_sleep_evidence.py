from pathlib import Path
import logging

import yaml

from control_plane.contracts.preview_inventory_scan_record import PreviewSleepObservation
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy import api
from control_plane.preview_gateway import PREVIEW_GATEWAY_SERVICE
from control_plane.preview_idle import PreviewCannotSleep, PreviewIdleAdapter, PreviewIdleScheduler
from control_plane.contracts.preview_execution import odoo_preview_target_key
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import preview_slug_from_application_name

_LOGGER = logging.getLogger(__name__)


def collect_preview_sleep_observations(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    root: Path,
    host: str,
    token: str,
    inventory: tuple[tuple[str, str, str], ...],
    prefix: str,
) -> tuple[PreviewSleepObservation, ...]:
    scheduler = PreviewIdleScheduler(store, control_plane_root=root)
    observations: list[PreviewSleepObservation] = []
    for _, compose_id, name in inventory:
        slug = preview_slug_from_application_name(
            app_name_suffix=profile.preview.app_name_suffix,
            app_name_prefix=prefix,
            application_name=name,
        )
        if not slug:
            continue
        state = PreviewSleepObservation(
            slug=slug,
            state="cannot_sleep",
            reason="Sleep eligibility could not be verified; inspect preview diagnostics.",
        )
        try:
            previews = store.list_preview_records(context_name=profile.preview.context, limit=None)
            payload = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=compose_id
            )
            declaration = yaml.safe_load(str(payload.get("composeFile") or ""))
            if not isinstance(declaration, dict) or PREVIEW_GATEWAY_SERVICE not in declaration.get(
                "services", {}
            ):
                raise PreviewCannotSleep(
                    "The idle gateway is missing; an ordinary rebuild installs it."
                )
            matching = [
                preview
                for preview in previews
                if preview.anchor_repo in {profile.repository, profile.repository.partition("/")[2]}
                and profile.preview.slug_template.format(number=preview.anchor_pr_number) == slug
                and preview.state != "destroyed"
            ]
            if len(matching) != 1:
                raise PreviewCannotSleep(
                    "No unique serving preview record is available; inspect deployment recovery."
                )
            target = scheduler._require_binding(profile, matching[0].preview_id, host, token)
            observation = PreviewIdleAdapter(
                host=host, token=token, target=target, intent={}, record_id="", check=lambda: None
            ).read("observe")
            services = observation["services"]
            if set(services) != {"web", "database", "script-runner"}:
                raise ValueError("Incomplete application state")
            if observation["gateway"]["mode"] == "asleep" and all(
                not item["running"] for item in services.values()
            ):
                state = PreviewSleepObservation(slug=slug, state="asleep")
            elif store.list_held_provider_target_reservations(
                provider_target_key=odoo_preview_target_key(
                    context=target["context"], compose_name=target["compose_name"]
                )
            ):
                raise PreviewCannotSleep(
                    "A deployment or recovery operation is holding this preview."
                )
            elif observation["gateway"]["mode"] == "awake" and all(
                item["running"] and item["healthy"] for item in services.values()
            ):
                state = PreviewSleepObservation(slug=slug, state="awake")
            else:
                raise PreviewCannotSleep(
                    "Application services are stopped, unhealthy or changing sleep state."
                )
        except PreviewCannotSleep as error:
            state = state.model_copy(update={"reason": str(error)})
        except Exception:
            _LOGGER.warning("Preview sleep observation is unavailable for %s", slug)
        observations.append(state)
    return tuple(observations)
