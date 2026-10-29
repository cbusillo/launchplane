import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import Callable
from typing import cast
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from httpx2 import Response
from control_plane.http_routes.support import ApiRouteRegistrar
from pydantic import BaseModel

from control_plane.contracts.product_retirement import ProductRetirementRecord
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.contracts.retained_volume_sweep import (
    RETAINED_VOLUME_SWEEP_ROUTE,
    RetainedVolumeSweepRequest,
)
from control_plane.http_routes.retained_volume_sweep import (
    RetainedVolumeSweepDependencies,
    register_retained_volume_sweep_route,
)
from control_plane.product_retirement import (
    bind_product_retirement_authority,
    build_product_retirement_plan_record,
)
from control_plane.retained_volume_sweep_execution import (
    build_retained_volume_sweep,
    _inventory_clear,
)
from control_plane.dokploy_inventory import TARGET_COLLECTIONS
from control_plane.service_auth import LocalOperatorIdentity
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplaneRetainedTestingGenerationRow,
)
from tests.support.http import request
from tests.test_product_retirement import _Store, _request, _observation


class RetainedVolumeSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        fixture = _Store()
        fixture.profile = fixture.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"instance": "testing"})
                    for lane in fixture.profile.lanes
                ),
                "production_use": "prelaunch",
            }
        )
        assert fixture.provider_target and fixture.dokploy_target and fixture.target_id
        fixture.provider_target = fixture.provider_target.model_copy(update={"instance": "testing"})
        fixture.dokploy_target = fixture.dokploy_target.model_copy(update={"instance": "testing"})
        fixture.target_id = fixture.target_id.model_copy(update={"instance": "testing"})
        fixture.runtime_records = tuple(
            row.model_copy(update={"instance": "testing"}) for row in fixture.runtime_records
        )
        bound = bind_product_retirement_authority(
            record_store=fixture, request=_request(instance="testing")
        )
        from control_plane.contracts.product_retirement import ProductRetirementIdentity

        self.binding = RetainedVolumeBinding(
            volume_name="retired-data",
            server_id="registered-server",
            application_id="retired-app",
            application_name="retired-app",
            volume_identity_sha256="a" * 64,
        )
        observation = _observation().model_copy(
            update={"retained_volumes": (self.binding,), "retained_volume_inventory_complete": True}
        )
        plan = build_product_retirement_plan_record(
            request=_request(instance="testing"),
            identity=ProductRetirementIdentity(actor="test", identity_kind="test"),
            trace_id="retire-plan",
            idempotency_key="retire-key",
            requested_at="2026-01-01T00:00:00Z",
            bound=bound,
            observation=observation,
        )
        payload = plan.model_dump(mode="json")
        payload.update(
            mode="apply",
            outcome="retired",
            record_id="retirement-complete",
            reviewed_plan_record_id=plan.record_id,
            reviewed_plan_sha256=plan.plan_sha256,
            completed_at="2026-01-01T00:00:00Z",
            mutation_evidence={"provider_absence_verified": True},
        )
        self.record = ProductRetirementRecord.model_validate(payload)
        self.store.write_product_profile_record(
            fixture.profile.model_copy(update={"lifecycle_state": "retired"})
        )
        self.store.write_product_retirement_record(self.record)
        self.apply_calls: list[str] = []
        self.present = True
        self.unknown_apply = False
        self.fail_readback = False
        self.never_started = False
        self.host_keys: list[str] = []
        self.patches = (
            patch(
                "control_plane.retained_volume_sweep_execution.source.read_dokploy_config",
                return_value=("https://provider.invalid", "test-token"),
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                return_value=(),
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.native_copy_stream.run_managed_server_program",
                side_effect=self.host,
            ),
        )
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def host(self, **kwargs: object) -> None:
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        mode, key = payload["mode"], payload["operation"]
        self.host_keys.append(str(key))
        if mode == "apply":
            dispatch_guard = kwargs["dispatch_guard"]
            assert callable(dispatch_guard)
            guard = cast(Callable[[Callable[[], None]], None], dispatch_guard)
            guard(lambda: self.apply_calls.append(str(key)))
            if not self.never_started:
                self.present = False
            status = "unknown" if self.unknown_apply or self.never_started else "removed"
        elif mode == "readback":
            status = "unknown" if self.fail_readback or self.present else "absent"
        else:
            status = "eligible" if self.present else "held"
        receive_callback = kwargs["receive"]
        assert callable(receive_callback)
        receive = cast(Callable[[dict[str, object]], None], receive_callback)
        reason = "test_host_evidence"
        if mode == "readback" and self.never_started:
            status, reason = "held", "never_started"
        receive({"type": "complete", "result": {"status": status, "reason_code": reason}})

    def payload(self, **updates: object) -> dict[str, object]:
        return {"retirement_record_id": self.record.record_id, "minimum_age_seconds": 1, **updates}

    def app(self, *, director: bool = True) -> FastAPI:
        app = FastAPI()

        def error(**kwargs: object) -> HTTPException:
            status_code = kwargs["status_code"]
            if type(status_code) is not int:
                raise AssertionError("HTTP status must be an integer")
            return HTTPException(status_code=status_code, detail=kwargs)

        register_retained_volume_sweep_route(
            cast(ApiRouteRegistrar, cast(object, app)),
            dependencies=RetainedVolumeSweepDependencies(
                read_identity=lambda: LocalOperatorIdentity(
                    "test-agent", "test", director_agent=director
                ),
                get_record_store=lambda: self.store,
                next_trace_id=lambda: "sweep-test",
                authorization_allows=lambda **_: False,
                http_error=error,
                control_plane_root=self.root,
                error_response_model=BaseModel,
            ),
        )
        return app

    @staticmethod
    def post(app: FastAPI, payload: dict[str, object], key: str = "") -> Response:
        return asyncio.run(
            request(
                app,
                "POST",
                RETAINED_VOLUME_SWEEP_ROUTE,
                payload=payload,
                headers={"Idempotency-Key": key},
            )
        )

    def test_reachable_dry_run_apply_readback_and_changed_request(self) -> None:
        app = self.app()
        dry = self.post(app, self.payload())
        self.assertEqual(dry.status_code, 200, dry.text)
        self.assertEqual(dry.json()["items"][0]["status"], "eligible")
        self.assertNotIn(self.binding.volume_name, dry.text)
        self.assertEqual(self.apply_calls, [])
        reviewed = dry.json()["plan_sha256"]
        applied = self.post(
            app, self.payload(mode="apply", reviewed_plan_sha256=reviewed), "sweep-key"
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["items"][0]["status"], "removed")
        self.assertEqual(len(self.apply_calls), 1)
        readback = self.post(
            app, self.payload(mode="readback", reviewed_plan_sha256=reviewed), "sweep-key"
        )
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertTrue(readback.json()["replayed"])
        self.assertEqual(len(self.apply_calls), 1)
        changed = self.post(
            app,
            self.payload(mode="apply", reviewed_plan_sha256=reviewed, minimum_age_seconds=2),
            "sweep-key",
        )
        self.assertEqual(changed.status_code, 409)

    def test_uncertain_apply_reconciles_original_operation_without_redispatch(self) -> None:
        app = self.app()
        reviewed = self.post(app, self.payload()).json()["plan_sha256"]
        self.unknown_apply = True
        applied = self.post(
            app, self.payload(mode="apply", reviewed_plan_sha256=reviewed), "uncertain-key"
        )
        self.assertEqual(applied.status_code, 409, applied.text)
        self.assertEqual(len(self.apply_calls), 1)
        self.fail_readback = True
        retry = self.post(
            app, self.payload(mode="apply", reviewed_plan_sha256=reviewed), "uncertain-key"
        )
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(len(self.apply_calls), 1)
        self.fail_readback = False
        readback = self.post(
            app, self.payload(mode="readback", reviewed_plan_sha256=reviewed), "uncertain-key"
        )
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json()["items"][0]["status"], "absent")
        self.assertEqual(len(self.apply_calls), 1)
        self.assertEqual(len(set(key for key in self.host_keys if key != "inspection")), 1)

    def test_non_director_and_missing_readback_receipt_are_refused(self) -> None:
        self.assertEqual(self.post(self.app(director=False), self.payload()).status_code, 403)
        missing = self.post(
            self.app(), self.payload(mode="readback", reviewed_plan_sha256="a" * 64), "missing-key"
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.apply_calls, [])

    def test_production_unknown_inventory_age_and_recovery_custody_hold(self) -> None:
        request_model = RetainedVolumeSweepRequest.model_validate(self.payload())
        cases: tuple[dict[str, object], ...] = (
            {"instance": "prod"},
            {
                "provider_observation": self.record.provider_observation.model_copy(
                    update={"retained_volume_inventory_complete": False}
                )
            },
            {"completed_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
            {"completed_at": "invalid"},
        )
        for index, update in enumerate(cases):
            record = self.record.model_copy(update={"record_id": f"held-{index}", **update})
            self.store.write_product_retirement_record(record)
            plan, _ = build_retained_volume_sweep(
                self.store,
                self.root,
                request_model.model_copy(update={"retirement_record_id": record.record_id}),
            )
            self.assertEqual(plan.items[0].status, "held")
        with patch.object(
            self.store,
            "retained_volume_recovery_payloads",
            return_value=({"retained_volume": self.binding.volume_name},),
        ):
            plan, _ = build_retained_volume_sweep(self.store, self.root, request_model)
        self.assertEqual(plan.items[0].reason, "recovery_custody_unreleased")
        self.assertEqual(self.apply_calls, [])

    def test_persisted_recovery_namespace_and_provider_references_remain_held(self) -> None:
        with self.store._session_factory() as session:
            session.add(
                LaunchplaneRetainedTestingGenerationRow(
                    operation_id="retained-operation",
                    generation_binding_sha256="b" * 64,
                    payload={"generation": {"resource_prefix": "retired"}},
                )
            )
            session.commit()
        plan, _ = build_retained_volume_sweep(
            self.store, self.root, RetainedVolumeSweepRequest.model_validate(self.payload())
        )
        self.assertEqual(plan.items[0].reason, "recovery_custody_unreleased")
        with (
            patch.object(self.store, "retained_volume_recovery_payloads", return_value=()),
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                return_value=(
                    {
                        "environments": [
                            {
                                **{kind: [] for kind in TARGET_COLLECTIONS},
                                "applications": [{"applicationId": "another-app"}],
                            }
                        ]
                    },
                ),
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_target_payload",
                return_value={"mounts": [{"volumeName": self.binding.volume_name}]},
            ),
        ):
            plan, _ = build_retained_volume_sweep(
                self.store, self.root, RetainedVolumeSweepRequest.model_validate(self.payload())
            )
        self.assertEqual(plan.items[0].reason, "provider_reference_unavailable")
        self.assertEqual(self.apply_calls, [])

    def test_never_started_original_settles_then_allows_a_new_reviewed_sweep(self) -> None:
        app = self.app()
        reviewed = self.post(app, self.payload()).json()["plan_sha256"]
        self.never_started = True
        first = self.post(
            app, self.payload(mode="apply", reviewed_plan_sha256=reviewed), "no-effect-key"
        )
        self.assertEqual(first.status_code, 409)
        settled = self.post(
            app, self.payload(mode="readback", reviewed_plan_sha256=reviewed), "no-effect-key"
        )
        self.assertEqual(settled.status_code, 200, settled.text)
        self.assertEqual(settled.json()["items"][0]["status"], "held")
        self.never_started = False
        reviewed = self.post(app, self.payload()).json()["plan_sha256"]
        completed = self.post(
            app, self.payload(mode="apply", reviewed_plan_sha256=reviewed), "new-reviewed-key"
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["items"][0]["status"], "removed")

    def test_database_and_converted_compose_references_are_checked(self) -> None:
        environment: dict[str, object] = {kind: [] for kind in TARGET_COLLECTIONS}
        environment["postgres"] = [{"postgresId": "other-database"}]
        with (
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                return_value=({"environments": [environment]},),
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.api.dokploy_request",
                return_value={
                    "postgresId": "other-database",
                    "mounts": [{"volumeName": self.binding.volume_name}],
                },
            ),
        ):
            self.assertFalse(_inventory_clear(self.root, self.binding))
        environment["postgres"] = []
        environment["compose"] = [{"composeId": "other-compose"}]
        with (
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                return_value=({"environments": [environment]},),
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_target_payload",
                return_value={"mounts": [], "appName": "other-stack"},
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.compose.fetch_dokploy_converted_compose_file",
                return_value='{"services":{"web":{"volumes":["retired-data:/data"]}},"volumes":{"data":{"external":true,"name":"retired-data"}}}',
            ),
        ):
            self.assertFalse(_inventory_clear(self.root, self.binding))

    def test_other_retirement_cannot_bypass_an_uncertain_volume_reservation(self) -> None:
        app = self.app()
        reviewed = self.post(app, self.payload()).json()["plan_sha256"]
        self.unknown_apply = True
        self.assertEqual(
            self.post(
                app, self.payload(mode="apply", reviewed_plan_sha256=reviewed), "held-effect-key"
            ).status_code,
            409,
        )
        other = self.record.model_copy(update={"record_id": "other-retirement"})
        self.store.write_product_retirement_record(other)
        plan, _ = build_retained_volume_sweep(
            self.store,
            self.root,
            RetainedVolumeSweepRequest(retirement_record_id=other.record_id, minimum_age_seconds=1),
        )
        self.assertEqual(plan.items[0].reason, "recovery_custody_unreleased")
        self.assertEqual(len(self.apply_calls), 1)
