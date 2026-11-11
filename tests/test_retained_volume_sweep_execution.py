import asyncio
import json
from contextlib import closing
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
    compose_volume_names,
)
from control_plane.dokploy_inventory import TARGET_COLLECTIONS
from control_plane.dokploy import api, native_copy_stream
from control_plane.service_auth import LocalOperatorIdentity, TerminalAgentIdentity
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

    def app(self, *, director: bool = True, terminal: bool = False) -> FastAPI:
        app = FastAPI()
        identity_type = TerminalAgentIdentity if terminal else LocalOperatorIdentity

        def error(**kwargs: object) -> HTTPException:
            status_code = kwargs["status_code"]
            if type(status_code) is not int:
                raise AssertionError("HTTP status must be an integer")
            return HTTPException(status_code=status_code, detail=kwargs)

        register_retained_volume_sweep_route(
            cast(ApiRouteRegistrar, cast(object, app)),
            dependencies=RetainedVolumeSweepDependencies(
                read_identity=lambda: identity_type("test-agent", "test", director_agent=director),
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

    def test_director_terminal_sweeps_but_ordinary_terminal_is_refused(self) -> None:
        for mode in ("dry-run", "apply", "readback"):
            with self.subTest(mode=mode):
                body = self.payload(mode=mode)
                if mode != "dry-run":
                    body["reviewed_plan_sha256"] = "f" * 64
                denied = self.post(
                    self.app(director=False, terminal=True), body, "ordinary-terminal"
                )
                self.assertEqual(denied.status_code, 403, denied.text)
        app = self.app(terminal=True)
        dry = self.post(app, self.payload())
        self.assertEqual(dry.status_code, 200, dry.text)
        body = self.payload(mode="apply", reviewed_plan_sha256=dry.json()["plan_sha256"])
        applied = self.post(app, body, "director-terminal")
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["items"][0]["status"], "removed")
        body["mode"] = "readback"
        observed = self.post(app, body, "director-terminal")
        self.assertEqual(observed.status_code, 200, observed.text)
        self.assertEqual(len(self.apply_calls), 1)

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
            return_value=(payload for payload in ({"retained_volume": self.binding.volume_name},)),
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
            patch.object(
                self.store,
                "retained_volume_recovery_payloads",
                return_value=(payload for payload in cast(tuple[dict[str, object], ...], ())),
            ),
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

    def test_unrelated_recovery_history_does_not_prevent_sweep_or_hide_late_custody(self) -> None:
        unrelated_rows = [
            LaunchplaneRetainedTestingGenerationRow(
                operation_id=f"unrelated-operation-{index}",
                generation_binding_sha256=f"{index:064x}",
                payload={"generation": {"resource_prefix": f"unrelated-{index}"}},
            )
            for index in range(10_001)
        ]
        with self.store._session_factory() as session:
            session.add_all(unrelated_rows)
            session.commit()

        app = self.app()
        eligible = self.post(app, self.payload())
        self.assertEqual(eligible.status_code, 200, eligible.text)
        self.assertEqual(eligible.json()["items"][0]["status"], "eligible")

        with self.store._session_factory() as session:
            session.add(
                LaunchplaneRetainedTestingGenerationRow(
                    operation_id="late-custody-operation",
                    generation_binding_sha256="f" * 64,
                    payload={"generation": {"resource_prefix": self.binding.volume_name}},
                )
            )
            session.commit()
        held = self.post(app, self.payload())
        self.assertEqual(held.status_code, 200, held.text)
        self.assertEqual(held.json()["items"][0]["status"], "held")
        self.assertEqual(held.json()["items"][0]["reason"], "recovery_custody_unreleased")
        with closing(self.store.retained_volume_recovery_payloads()) as payloads:
            self.assertEqual(sum(1 for _ in payloads), len(unrelated_rows) + 1)

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

    def test_implicit_volume_names_follow_the_provider_project_command(self) -> None:
        selected_project = "selected-project"
        payload = {
            "appName": selected_project,
            "composeType": "docker-compose",
            "createEnvFile": True,
            "command": None,
            "randomize": False,
        }
        for project in ("different-project", "${PROJECT_NAME}"):
            document: dict[str, object] = {"name": project, "volumes": {"data": {}}}
            with self.subTest(project=project):
                self.assertEqual(
                    compose_volume_names(document, payload), (selected_project + "_data",)
                )
                for changes in ({"command": "compose -p another up"}, {"command": ""}):
                    if changes["command"]:
                        with self.assertRaises(ValueError):
                            compose_volume_names(document, {**payload, **changes})
                    else:
                        self.assertEqual(
                            compose_volume_names(document, {**payload, **changes}),
                            (selected_project + "_data",),
                        )
        self.assertEqual(
            compose_volume_names({"volumes": {"data": {"external": True}}}, {}), ("data",)
        )
        self.assertEqual(
            compose_volume_names({"volumes": {"data": {"name": "literal-volume"}}}, {}),
            ("literal-volume",),
        )

    def test_exact_inventory_resolves_qualified_default_syntax_without_guessing_inputs(
        self,
    ) -> None:
        environment: dict[str, object] = {**{kind: [] for kind in TARGET_COLLECTIONS}, "env": ""}
        environment["compose"] = [{"composeId": "other-compose"}]
        project = {"env": "", "environments": [environment]}
        payload = {
            "composeId": "other-compose",
            "mounts": [],
            "appName": "other-stack",
            "env": "STATE_VOLUME=unrelated-state",
            "composeType": "docker-compose",
            "sourceType": "raw",
            "composePath": "./docker-compose.yml",
            "createEnvFile": True,
            "command": None,
            "randomize": False,
        }
        document = {
            "services": {"web": {"volumes": ["data:/data"]}},
            "volumes": {"data": {"external": True, "name": "${STATE_VOLUME:-unused}-data"}},
        }
        for changes, exact, clear in (
            ({}, True, True),
            (
                {"command": "compose -p other-stack -f docker-compose.yml up -d --remove-orphans"},
                True,
                True,
            ),
            (
                {
                    "command": "compose -p another-stack -f docker-compose.yml up -d --remove-orphans"
                },
                True,
                False,
            ),
            (
                {"env": "STATE_VOLUME=" + self.binding.volume_name.removesuffix("-data")},
                True,
                False,
            ),
            ({"env": ""}, True, False),
            ({"env": "STATE_VOLUME="}, True, False),
            ({"env": "STATE_VOLUME=one\nSTATE_VOLUME=two"}, True, False),
            ({"env": "STATE_VOLUME=$UNRESOLVED"}, True, False),
            ({"createEnvFile": False}, True, False),
            ({"command": "compose --env-file another.env up"}, True, False),
            ({"composeType": "stack"}, True, False),
            ({"randomize": True}, True, False),
            ({}, False, False),
        ):
            with self.subTest(changes=changes, exact=exact):
                with (
                    patch(
                        "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                        return_value=(project,),
                    ),
                    patch(
                        "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_target_payload",
                        return_value={**payload, **changes},
                    ),
                    patch(
                        "control_plane.retained_volume_sweep_execution.compose.fetch_dokploy_converted_compose_file",
                        return_value=json.dumps(document),
                    ),
                ):
                    self.assertEqual(_inventory_clear(self.root, self.binding, exact=exact), clear)
        with (
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                return_value=(project,),
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_target_payload",
                return_value=payload,
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.compose.fetch_dokploy_converted_compose_file",
                return_value="null",
            ),
        ):
            self.assertFalse(_inventory_clear(self.root, self.binding, exact=True))

    def test_inventory_reports_unavailable_projects_and_later_consumers_together(self) -> None:
        environment: dict[str, object] = {kind: [] for kind in TARGET_COLLECTIONS}
        environment.update(environmentId="environment", env="STATE=" + self.binding.volume_name)
        projects = (
            {"projectId": "unreadable", "environments": []},
            {"projectId": "consumer", "env": "", "environments": [environment]},
        )
        references: list[dict[str, str]] = []
        with patch(
            "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
            return_value=projects,
        ):
            self.assertFalse(
                _inventory_clear(self.root, self.binding, exact=True, references=references)
            )
        self.assertEqual(
            {(row["reason"], row["record_id"]) for row in references},
            {
                ("provider_inventory_unavailable", "unreadable"),
                ("foreign_provider_reference", "environment"),
            },
        )

    def test_unreadable_target_does_not_hide_a_later_target_consumer(self) -> None:
        environment: dict[str, object] = {kind: [] for kind in TARGET_COLLECTIONS}
        environment.update(
            environmentId="environment",
            env="",
            applications=[{"applicationId": "unreadable"}, {"applicationId": "consumer"}],
        )
        references: list[dict[str, str]] = []
        with (
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                return_value=({"env": "", "environments": [environment]},),
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_target_payload",
                side_effect=[
                    ValueError("malformed detail"),
                    {
                        "applicationId": "consumer",
                        "env": "",
                        "mounts": [{"volumeName": self.binding.volume_name}],
                    },
                ],
            ),
        ):
            self.assertFalse(
                _inventory_clear(self.root, self.binding, exact=True, references=references)
            )
        self.assertEqual(
            {(item["reason"], item["record_id"]) for item in references},
            {
                ("provider_inventory_unavailable", "unreadable"),
                ("foreign_provider_reference", "consumer"),
                ("inventory_target_acquisition_failed", "unreadable"),
                ("inventory_target_checks_skipped", "unreadable"),
            },
        )

    def test_nondiagnostic_inventory_preserves_read_exceptions(self) -> None:
        for detail_kind in ("environment", "target"):
            environment: dict[str, object] = {kind: [] for kind in TARGET_COLLECTIONS}
            environment["environmentId"] = "environment"
            if detail_kind == "target":
                environment.update(env="", applications=[{"applicationId": "target"}])
            with (
                self.subTest(detail_kind=detail_kind),
                patch(
                    "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                    return_value=(
                        {"projectId": "project", "env": "", "environments": [environment]},
                    ),
                ),
                patch(
                    "control_plane.retained_volume_sweep_execution.api.dokploy_request",
                    side_effect=ValueError("malformed detail"),
                ),
                patch(
                    "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_target_payload",
                    side_effect=ValueError("malformed detail"),
                ),
                self.assertRaises(ValueError),
            ):
                _inventory_clear(self.root, self.binding, exact=True)

    def test_inventory_attributes_simultaneous_failures_without_losing_consumer(self) -> None:
        environment = {
            **{kind: [] for kind in TARGET_COLLECTIONS},
            "env": "",
            "compose": [{"composeId": "broken"}, {"composeId": "consumer"}],
        }
        references: list[dict[str, str]] = []
        with (
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                return_value=({"env": "", "environments": [environment]},),
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_target_payload",
                side_effect=[
                    {"composeId": "broken", "env": [], "mounts": None},
                    {"composeId": "consumer", "env": "", "mounts": [], "private": "do-not-publish"},
                ],
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.compose.fetch_dokploy_converted_compose_file",
                side_effect=[
                    "[malformed",
                    json.dumps(
                        {
                            "services": {"web": {}},
                            "volumes": {"data": {"name": self.binding.volume_name}},
                        }
                    ),
                ],
            ),
        ):
            self.assertFalse(
                _inventory_clear(self.root, self.binding, exact=True, references=references)
            )
        found = {(row["reason"], row.get("record_id")) for row in references}
        self.assertTrue(
            {
                ("provider_inventory_unavailable", "broken"),
                ("inventory_environment_malformed", "broken"),
                ("inventory_mounts_unavailable", "broken"),
                ("inventory_converted_document_malformed", "broken"),
                ("foreign_provider_reference", "consumer"),
            }
            <= found
        )
        self.assertNotIn("do-not-publish", json.dumps(references))

    def test_inventory_acquisition_causes_keep_denials_and_legacy_exception_behavior(self) -> None:
        environment = {
            **{kind: [] for kind in TARGET_COLLECTIONS},
            "env": "",
            "compose": [{"composeId": "target"}],
        }
        self.enterContext(
            patch.object(
                api,
                "fetch_dokploy_projects",
                return_value=({"env": "", "environments": [environment]},),
            )
        )
        for stage, function in (
            ("target", "api.fetch_dokploy_target_payload"),
            ("conversion", "compose.fetch_dokploy_converted_compose_file"),
        ):
            for error in (
                OSError("private location"),
                native_copy_stream.NativeSourceAuthorizationDenied("private denial"),
                api.DokployRequestFailed(
                    method="GET", path="/private", detail="private response", status_code=403
                ),
            ):
                denied = not isinstance(error, OSError)
                for diagnostic in (False, True):
                    references: list[dict[str, str]] | None = [] if diagnostic else None
                    with (
                        self.subTest(stage=stage, error=type(error), diagnostic=diagnostic),
                        patch.object(
                            api,
                            "fetch_dokploy_target_payload",
                            return_value={"env": "", "mounts": []},
                        ),
                        patch(
                            "control_plane.retained_volume_sweep_execution." + function,
                            side_effect=error,
                        ),
                    ):
                        if denied or not diagnostic:
                            with self.assertRaises(type(error)) as caught:
                                _inventory_clear(
                                    self.root, self.binding, exact=True, references=references
                                )
                            self.assertIs(caught.exception, error)
                        else:
                            self.assertFalse(
                                _inventory_clear(
                                    self.root, self.binding, exact=True, references=references
                                )
                            )
                            self.assertIn(
                                "inventory_" + stage + "_acquisition_failed",
                                {row["reason"] for row in references or []},
                            )
                        self.assertNotIn("private", json.dumps(references))

    def test_volume_resolution_failure_keeps_other_documents_and_references(self) -> None:
        environment = {
            **{kind: [] for kind in TARGET_COLLECTIONS},
            "env": "",
            "compose": [{"composeId": "target"}],
        }
        documents = (
            {"services": {"web": {}}, "volumes": {"bad": {"name": []}}},
            {"services": {"web": {}}, "volumes": {"good": {"name": self.binding.volume_name}}},
        )
        with (
            patch.object(
                api,
                "fetch_dokploy_projects",
                return_value=({"env": "", "environments": [environment]},),
            ),
            patch.object(
                api, "fetch_dokploy_target_payload", return_value={"env": "", "mounts": []}
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.compose.fetch_dokploy_converted_compose_file",
                return_value="fixture",
            ),
            patch(
                "control_plane.retained_volume_sweep_execution.read_volume_documents",
                return_value=documents,
            ),
        ):
            references: list[dict[str, str]] = []
            self.assertFalse(
                _inventory_clear(self.root, self.binding, exact=True, references=references)
            )
            self.assertTrue(
                {"inventory_volume_identity_unqualified", "foreign_provider_reference"}
                <= {row["reason"] for row in references}
            )
            self.assertFalse(_inventory_clear(self.root, self.binding, exact=True))

    def test_exact_inventory_reads_environment_variables_from_complete_identity_bound_detail(
        self,
    ) -> None:
        environment = {
            **{kind: [] for kind in TARGET_COLLECTIONS},
            "environmentId": "environment",
        }
        project = {"projectId": "project", "env": "", "environments": [environment]}
        detail = {
            "environmentId": environment["environmentId"],
            "projectId": project["projectId"],
            "env": "",
        }
        for response, clear, reason in (
            (detail, True, ""),
            (
                {**detail, "env": "STATE_VOLUME=" + self.binding.volume_name},
                False,
                "foreign_provider_reference",
            ),
            (
                {**detail, "environmentId": "foreign-environment"},
                False,
                "provider_inventory_unavailable",
            ),
            ({**detail, "projectId": "foreign-project"}, False, "provider_inventory_unavailable"),
            (
                {key: value for key, value in detail.items() if key != "env"},
                False,
                "provider_inventory_unavailable",
            ),
        ):
            with self.subTest(response=response):
                with (
                    patch(
                        "control_plane.retained_volume_sweep_execution.api.fetch_dokploy_projects",
                        return_value=(project,),
                    ),
                    patch(
                        "control_plane.retained_volume_sweep_execution.api.dokploy_request",
                        return_value=response,
                    ) as read,
                ):
                    references: list[dict[str, str]] = []
                    self.assertEqual(
                        _inventory_clear(
                            self.root, self.binding, exact=True, references=references
                        ),
                        clear,
                    )
                    self.assertEqual(
                        references,
                        [{"reason": reason, "kind": "environment", "record_id": "environment"}]
                        if reason
                        else [],
                    )
                self.assertTrue(
                    all(call.kwargs.get("method", "GET") == "GET" for call in read.call_args_list)
                )

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
