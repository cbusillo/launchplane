import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
    OdooWebsiteBootstrapPayload,
)
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.dokploy.source import (
    DokploySourceOfTruth,
    DokployTargetDefinition,
    resolve_ship_healthcheck_urls,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy
from control_plane.workflows.odoo_stable_target_replacement import _target_base_url
from tests.test_odoo_post_deploy import _module_update_evidence


class OdooPublicBaseUrlTests(unittest.TestCase):
    def test_cm_deploy_and_promotion_derive_both_urls_without_persisting_override(self) -> None:
        origin = "cm-website-prod.shinycomputers.com"
        for phase in ("deploy", "promotion"):
            for hosts in (
                ("cellmechanic.com", "www.cellmechanic.com"),
                ("www.cellmechanic.com", "cellmechanic.com"),
                (),
            ):
                for has_record in (False, True):
                    with (
                        self.subTest(phase=phase, hosts=hosts, has_record=has_record),
                        TemporaryDirectory() as directory,
                    ):
                        store = FilesystemRecordStore(state_dir=Path(directory) / "state")
                        original_url = "https://old.example.test"
                        if has_record:
                            store.write_odoo_instance_override_record(
                                OdooInstanceOverrideRecord(
                                    context="cm_website",
                                    instance="prod",
                                    config_parameters=(
                                        OdooConfigParameterOverride(
                                            key="web.base.url",
                                            value=OdooOverrideValue(
                                                source="literal", value=original_url
                                            ),
                                        ),
                                    ),
                                    website_bootstrap=OdooWebsiteBootstrapPayload(
                                        name="Cell Mechanic", canonical_url=original_url
                                    ),
                                    updated_at="2026-10-08T00:00:00Z",
                                )
                            )
                        target = DokployTargetDefinition(
                            context="cm_website",
                            instance="prod",
                            target_id="cm-compose",
                            domains=(origin,),
                            public_hosts=hosts,
                        )
                        captured = []

                        def run(**kwargs: object) -> dict[str, str]:
                            captured.append(kwargs)
                            return _module_update_evidence()

                        with (
                            patch(
                                "control_plane.workflows.odoo_post_deploy.dokploy_source.read_control_plane_dokploy_source_of_truth",
                                return_value=DokploySourceOfTruth(
                                    schema_version=1, targets=(target,)
                                ),
                            ),
                            patch(
                                "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                                return_value=("host", "token"),
                            ),
                            patch(
                                "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                                side_effect=run,
                            ),
                        ):
                            result = execute_odoo_post_deploy(
                                control_plane_root=Path(directory),
                                record_store=store,
                                request=OdooPostDeployRequest(
                                    context="cm_website", instance="prod", phase=phase
                                ),
                            )
                        self.assertEqual(result.post_deploy_status, "pass")
                        env = cast(dict[str, str], captured[0]["workflow_environment_overrides"])
                        if has_record or hosts:
                            payload = json.loads(
                                base64.b64decode(env["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"])
                            )
                            expected = f"https://{hosts[0]}" if hosts else original_url
                            parameters = {
                                item["key"]: item["value"]["value"]
                                for item in payload["config_parameters"]
                            }
                            self.assertEqual(parameters["web.base.url"], expected)
                            if has_record:
                                self.assertEqual(
                                    payload["website_bootstrap"]["canonical_url"], expected
                                )
                            if hosts:
                                self.assertEqual(
                                    result.override_evidence["resolved_base_url"], expected
                                )
                        else:
                            self.assertEqual(env, {})
                        if has_record:
                            persisted = store.read_odoo_instance_override_record(
                                context_name="cm_website", instance_name="prod"
                            )
                            self.assertEqual(
                                persisted.config_parameters[0].value.value, original_url
                            )
                            assert persisted.website_bootstrap is not None
                            self.assertEqual(
                                persisted.website_bootstrap.canonical_url, original_url
                            )
                        else:
                            with self.assertRaises(FileNotFoundError):
                                store.read_odoo_instance_override_record(
                                    context_name="cm_website", instance_name="prod"
                                )
                        self.assertEqual(
                            resolve_ship_healthcheck_urls(
                                target_definition=target, environment_values={}
                            ),
                            (f"https://{origin}/web/health",),
                        )

    def test_public_url_precedes_lane_url_but_testing_and_no_public_hosts_keep_existing_url(
        self,
    ) -> None:
        for instance, hosts, expected in (
            ("prod", ("cellmechanic.com", "www.cellmechanic.com"), "https://cellmechanic.com"),
            ("testing", ("cellmechanic.com",), "https://origin.example.test"),
            ("prod", (), "https://origin.example.test"),
        ):
            lane = ProductLaneProfile(
                context="cm_website", instance=instance, base_url="https://origin.example.test/"
            )
            self.assertEqual(
                _target_base_url(lane=lane, domains=("other.example.test",), public_hosts=hosts),
                expected,
            )

    def test_cm_stable_bootstrap_renders_public_url_and_verifies_internal_health(self) -> None:
        from tests.test_odoo_stable_bootstrap import _Store
        from control_plane.workflows.odoo_stable_bootstrap import (
            OdooStableBootstrapRequest,
            execute_odoo_stable_bootstrap,
        )
        from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult
        from control_plane.workflows.odoo_verification import (
            OdooVerificationResult,
            OdooVerificationEvidence,
        )

        fixture = _Store()
        origin = "cm-website-prod.shinycomputers.com"
        hosts = ("cellmechanic.com", "www.cellmechanic.com")
        lane = fixture.profile.lanes[0].model_copy(
            update={
                "context": "cm_website",
                "instance": "prod",
                "base_url": "",
                "health_url": "",
                "odoo_stable_bootstrap": fixture.profile.lanes[0].odoo_stable_bootstrap.model_copy(
                    update={
                        "expected_domains": (origin,),
                        "expected_target_name": "cm-website-prod",
                    }
                ),
            }
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory) / "state")
            store.write_product_profile_record(
                fixture.profile.model_copy(update={"lanes": (lane,)})
            )
            store.write_dokploy_target_record(
                fixture.target_record.model_copy(
                    update={
                        "context": lane.context,
                        "instance": "prod",
                        "target_name": "cm-website-prod",
                        "domains": (origin,),
                        "public_hosts": hosts,
                    }
                )
            )
            store.write_dokploy_target_id_record(
                fixture.target_id_record.model_copy(
                    update={"context": lane.context, "instance": "prod"}
                )
            )
            store.write_environment_inventory(
                fixture.inventory.model_copy(update={"context": lane.context, "instance": "prod"})
            )
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context=lane.context,
                    instance="prod",
                    website_bootstrap=OdooWebsiteBootstrapPayload(
                        name="Cell Mechanic", canonical_url="https://local.example.test"
                    ),
                    updated_at="2026-10-08T00:00:00Z",
                )
            )
            with (
                patch(
                    "control_plane.workflows.odoo_stable_bootstrap.dokploy_source.read_dokploy_config",
                    return_value=("host", "token"),
                ),
                patch(
                    "control_plane.workflows.odoo_stable_bootstrap.dokploy_post_deploy.run_compose_odoo_stable_bootstrap"
                ) as bootstrap,
                patch(
                    "control_plane.workflows.odoo_stable_bootstrap.execute_odoo_post_deploy",
                    return_value=OdooPostDeployResult(
                        context=lane.context,
                        instance="prod",
                        phase="deploy",
                        post_deploy_status="pass",
                    ),
                ),
                patch(
                    "control_plane.workflows.odoo_stable_bootstrap.verify_odoo_stable_readiness",
                    return_value=OdooVerificationResult(
                        health_status="pass",
                        canonical_status="pass",
                        logo_status="pass",
                        evidence=OdooVerificationEvidence(
                            base_url=f"https://{hosts[0]}", canonical_url=f"https://{hosts[0]}"
                        ),
                    ),
                ) as verify,
            ):
                result = execute_odoo_stable_bootstrap(
                    control_plane_root=Path(directory),
                    record_store=store,
                    request=OdooStableBootstrapRequest(
                        product=fixture.profile.product,
                        context=lane.context,
                        instance="prod",
                        confirmation=lane.odoo_stable_bootstrap.confirmation,
                    ),
                    dokploy_request=lambda *args, **kwargs: [
                        {"host": host} for host in (*hosts, origin)
                    ],
                )
            self.assertEqual(result.bootstrap_status, "pass")
            payload = json.loads(
                base64.b64decode(
                    bootstrap.call_args.kwargs["workflow_environment_overrides"][
                        "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"
                    ]
                )
            )
            self.assertEqual(payload["website_bootstrap"]["canonical_url"], f"https://{hosts[0]}")
            self.assertEqual(
                payload["config_parameters"][0]["value"]["value"], f"https://{hosts[0]}"
            )
            self.assertEqual(verify.call_args.kwargs["base_url"], f"https://{hosts[0]}")
            self.assertEqual(
                verify.call_args.kwargs["health_url"], f"https://{origin}/launchplane/health"
            )
            self.assertEqual(result.canonical_url, f"https://{hosts[0]}")

    def test_replacement_dry_run_reports_public_url_from_record(self) -> None:
        from tests.test_odoo_stable_target_replacement import _Store, _profile, _target_record
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementRequest,
        )
        from control_plane.workflows.odoo_stable_target_replacement import (
            build_odoo_stable_target_replacement_plan,
        )

        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"instance": "prod"})
        target = _target_record().model_copy(
            update={
                "instance": "prod",
                "public_hosts": ("cellmechanic.com", "www.cellmechanic.com"),
            }
        )
        plan = build_odoo_stable_target_replacement_plan(
            control_plane_root=Path("."),
            record_store=_Store(
                profile=profile.model_copy(update={"lanes": (lane,)}), target_record=target
            ),
            request=OdooStableTargetReplacementRequest(product=profile.product, instance="prod"),
        )
        self.assertEqual(plan.model_dump()["base_url"], "https://cellmechanic.com")
