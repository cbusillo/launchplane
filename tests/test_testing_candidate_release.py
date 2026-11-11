import asyncio
import base64
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from httpx2 import Response
from pydantic import BaseModel

from control_plane import testing_candidate_release as release
from control_plane.contracts.testing_candidate_release import TestingCandidateReleasePlan
from control_plane.http_routes import testing_candidate_release as routes
from control_plane.http_routes.support import ApiRouteRegistrar
from control_plane.provider_operations import (
    ProviderOperationLease,
)
from control_plane.service_auth import LocalOperatorIdentity
from tests.support.http import request
from control_plane.testing_candidate_release import (
    TESTING_CANDIDATE_RELEASE_PROGRAM,
    TestingCandidateReleaseRefused,
)

NAMESPACE = "lp-testing-" + "a" * 32

DOCKER_FIXTURE = r"""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
state_path=Path(os.environ['FAKE_DOCKER_STATE'])
state=json.loads(state_path.read_text())
if sys.argv[1:3]!=['--host','unix:///var/run/docker.sock']:
    raise SystemExit(97)
args=sys.argv[3:]
state.setdefault('calls',[]).append(args)
def save():
    state_path.write_text(json.dumps(state))
volumes=state['volumes'];containers=state['containers']
if args==['volume','ls','--format','{{.Name}}']:
    print('\n'.join(volumes))
elif args[:2]==['volume','inspect']:
    item=volumes[args[2]]
    print(json.dumps([{'Name':args[2],'Driver':'local','Labels':item['labels'],'Mountpoint':item['path'],'Options':item.get('options')}]))
elif args==['ps','-aq','--no-trunc']:
    print('\n'.join(containers))
elif args[:3]==['inspect','--type','container']:
    print(json.dumps([{'Id':key,**containers[key]} for key in args[3:]]))
elif args[:3]==['container','inspect','--size']:
    print(json.dumps([{'Id':key,'SizeRw':containers[key].get('SizeRw',0)} for key in args[3:]]))
elif args[0]=='stop':
    pass
elif args[0]=='rm':
    for key in args[1:]:
        containers.pop(key)
elif args[:2]==['volume','rm']:
    if any(mount.get('Name')==args[2] for row in containers.values() for mount in row['Mounts']):
        save();raise SystemExit(1)
    volumes.pop(args[2])
else:
    save();raise SystemExit(2)
save()
"""


def resources(**changes: str) -> dict[str, str]:
    return {
        "product": "fixture",
        "context": "fixture-testing",
        "instance": "testing",
        "operation_id": "operation-1",
        "target_id": "compose-1",
        "namespace": NAMESPACE,
        **changes,
    }


def ownership_label(spec: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CandidateReleaseProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        for name, content in (("docker", DOCKER_FIXTURE),):
            (bin_directory / name).write_text(content)
            (bin_directory / name).chmod(0o755)
        self.path = str(bin_directory) + os.pathsep + os.environ["PATH"]
        self.state_path = self.root / "state.json"
        label = ownership_label(resources())
        volumes = {}
        for suffix in ("-data", "-db", "-logs"):
            data = self.root / "volumes" / (NAMESPACE + suffix) / "_data"
            data.mkdir(parents=True)
            volumes[NAMESPACE + suffix] = {
                "labels": {"io.launchplane.testing-resources": label},
                "path": str(data),
            }
        self.state: dict[str, Any] = {
            "volumes": volumes,
            "containers": {
                "candidate-web": {
                    "Config": {"Labels": {"com.docker.compose.project": NAMESPACE + "-abc123"}},
                    "Mounts": [{"Type": "volume", "Name": NAMESPACE + "-data"}],
                    "SizeRw": 500,
                }
            },
        }
        self.save()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def run_program(self, mode: str, spec: dict[str, str] | None = None) -> dict[str, Any]:
        payload = base64.b64encode(
            json.dumps({"mode": mode, "resources": spec or resources()}).encode()
        ).decode()
        result = subprocess.run(
            [sys.executable, "-c", TESTING_CANDIDATE_RELEASE_PROGRAM, payload, "FRAME:"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env=os.environ | {"PATH": self.path, "FAKE_DOCKER_STATE": str(self.state_path)},
        )
        self.state = json.loads(self.state_path.read_text())
        frame: dict[str, dict[str, Any]] = json.loads(result.stdout.removeprefix("FRAME:"))
        return frame["result"]

    def removals(self) -> list[list[str]]:
        return [
            call
            for call in self.state.get("calls", [])
            if call[0] == "rm" or call[:2] == ["volume", "rm"]
        ]

    def test_legacy_observation_reports_remaining_resources_and_removes_nothing(self) -> None:
        self.assertEqual(
            self.run_program("dry-run"), {"status": "eligible", "reclaimable_bytes": None}
        )
        self.assertEqual(self.removals(), [])

    def test_legacy_receipt_observation_cannot_dispatch_an_effect(self) -> None:
        self.assertEqual(self.run_program("apply")["reason_code"], "invalid_request")
        self.assertEqual(self.removals(), [])

    def test_production_or_foreign_scope_is_refused_before_any_removal(self) -> None:
        for spec in (
            resources(instance="prod"),
            resources(namespace="fixture-prod"),
            resources(namespace="lp-rehearsal-" + "a" * 32),
        ):
            with self.subTest(spec=spec):
                self.assertEqual(
                    self.run_program("apply", spec),
                    {"status": "held", "reason_code": "invalid_request"},
                )
        self.assertEqual(self.removals(), [])
        self.assertEqual(len(self.state["volumes"]), 3)

    def test_volume_without_this_candidates_ownership_label_is_kept(self) -> None:
        self.state["volumes"][NAMESPACE + "-db"]["labels"] = {
            "io.launchplane.testing-resources": ownership_label(resources(operation_id="other"))
        }
        self.save()
        self.assertEqual(
            self.run_program("dry-run"),
            {"status": "held", "reason_code": "volume_ownership_unverified"},
        )
        self.assertEqual(self.removals(), [])

    def test_bind_backed_volume_is_kept(self) -> None:
        self.state["volumes"][NAMESPACE + "-data"]["options"] = {"o": "bind"}
        self.save()
        self.assertEqual(
            self.run_program("dry-run"),
            {"status": "held", "reason_code": "volume_ownership_unverified"},
        )
        self.assertEqual(self.removals(), [])

    def test_volume_mounted_by_another_container_is_kept(self) -> None:
        self.state["containers"]["borrower"] = {
            "Config": {"Labels": {}},
            "Mounts": [{"Type": "volume", "Name": NAMESPACE + "-db", "Source": "/x"}],
        }
        self.save()
        self.assertEqual(
            self.run_program("dry-run"),
            {"status": "held", "reason_code": "volume_has_other_consumer"},
        )
        self.assertEqual(self.removals(), [])


def failed_operation(**changes: object) -> Mock:
    values = {
        "product": "fixture",
        "context": "fixture-testing",
        "operation_id": "operation-1",
        "is_native_testing_rebuild": True,
        "status": "fail",
        "error_code": "candidate_preparation_failed",
        **changes,
    }
    return Mock(**values)


class CandidateReleaseGuardTests(unittest.TestCase):
    def refusal(self, call: Callable[[], object]) -> str:
        with self.assertRaises(TestingCandidateReleaseRefused) as raised:
            call()
        return raised.exception.code

    def test_only_a_failed_testing_rebuild_is_releasable(self) -> None:
        for operation, code in (
            (failed_operation(is_native_testing_rebuild=False), "candidate_release_not_testing"),
            (failed_operation(product="other"), "candidate_release_not_testing"),
            (failed_operation(status="running"), "candidate_release_outcome_unrecorded"),
            (failed_operation(status="cancelled"), "candidate_release_outcome_unrecorded"),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(
                    self.refusal(
                        lambda: release.require_failed_testing_operation(operation, "fixture")
                    ),
                    code,
                )
        release.require_failed_testing_operation(failed_operation(), "fixture")

    def test_production_operation_never_reaches_the_host(self) -> None:
        store = Mock()
        store.read_odoo_stable_target_replacement_operation_record.return_value = failed_operation(
            is_native_testing_rebuild=False
        )
        with patch.object(release, "host_release") as host:
            self.assertEqual(
                self.refusal(
                    lambda: release.build_candidate_release(
                        Path("/unused"), store, "fixture", "operation-1"
                    )
                ),
                "candidate_release_not_testing",
            )
        host.assert_not_called()
        store.read_testing_candidate.assert_not_called()

    def test_selected_or_routed_candidate_is_kept(self) -> None:
        allocation = Mock(product="fixture", context="fixture-testing", operation_id="operation-1")
        cases = (
            ({"custody": ValueError("lane target names it")}, "candidate_release_selected"),
            ({"publication": Mock(state="applied")}, "candidate_release_selected"),
            ({"routed": True}, "candidate_release_selected"),
        )
        for case, code in cases:
            with (
                self.subTest(case=case),
                patch.object(
                    release, "require_candidate_target_custody", side_effect=case.get("custody")
                ),
                patch.object(release, "read_publication", return_value=case.get("publication")),
                patch.object(release, "candidate_routed", return_value=case.get("routed", False)),
            ):
                self.assertEqual(
                    self.refusal(
                        lambda: release.require_candidate_unselected(
                            Path("/unused"), Mock(), allocation
                        )
                    ),
                    code,
                )
        for state in ("planned", "reversed"):
            with (
                patch.object(release, "require_candidate_target_custody"),
                patch.object(release, "read_publication", return_value=Mock(state=state)),
                patch.object(release, "candidate_routed", return_value=False),
            ):
                release.require_candidate_unselected(Path("/unused"), Mock(), allocation)

    def lane_refusal(
        self,
        *,
        only_copy: bool = False,
        shared: bool = False,
        protected: bool = False,
        reason: str = "usage_protected",
    ) -> str | None:
        store = Mock()
        store.read_disposable_lane_safety.return_value = None
        with (
            patch.object(
                release,
                "recorded_lane_protection",
                return_value=Mock(only_copy=only_copy, shared=shared),
            ),
            patch.object(release, "read_odoo_testing_usage", return_value=Mock(usage="unknown")),
            patch.object(
                release,
                "evaluate_lane_usage",
                return_value=Mock(protected=protected, reason=reason),
            ) as usage,
        ):
            try:
                release.require_lane_releasable(Path("/unused"), store, failed_operation())
            except TestingCandidateReleaseRefused as error:
                return error.code
        self.assertFalse(usage.call_args.kwargs["require_unused"])
        return None

    def test_needed_copy_shared_resources_or_lane_use_keep_the_candidate(self) -> None:
        self.assertEqual(self.lane_refusal(only_copy=True), "testing_needed_copy")
        self.assertEqual(self.lane_refusal(shared=True), "testing_production_resources_shared")
        self.assertEqual(self.lane_refusal(protected=True), "testing_in_use")
        self.assertEqual(
            self.lane_refusal(protected=True, reason="staff_testing_hold"), "testing_staff_hold"
        )
        self.assertIsNone(self.lane_refusal())


class CandidateReleaseFixture(unittest.TestCase):
    def setUp(self) -> None:
        from contextlib import nullcontext
        from unittest.mock import PropertyMock
        from tests import test_native_testing_candidate_retention as fixtures

        self.fixture = fixtures.CandidateRetentionPlanTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.store = self.fixture.root, self.fixture.store
        self.enterContext(
            patch.object(
                type(self.store),
                "database_dialect_name",
                new_callable=PropertyMock,
                return_value="postgresql",
            )
        )
        self.executor = self.enterContext(
            patch.object(
                self.store,
                "testing_candidate_retention_guard",
                side_effect=lambda _: nullcontext(Mock()),
            )
        )
        self.routed = self.enterContext(
            patch.object(release, "candidate_routed", return_value=False)
        )
        self.usage = self.enterContext(patch.object(release, "require_lane_releasable"))
        self.applied: list[str] = []
        self.interrupt = False
        self.uncertain = True

        def host(
            _root: Path,
            _binding: Any,
            mode: str,
            key: str,
            lease: ProviderOperationLease | None = None,
            **kwargs: Any,
        ) -> tuple[str, str]:
            if mode == "apply":
                assert lease is not None
                lease.checkpoint_effect("release-fixture-volume")
                self.applied.append(key)
                return ("unknown", "effect_unknown") if self.interrupt else ("removed", "removed")
            if mode == "readback":
                return (
                    ("unknown", "effect_unknown")
                    if self.interrupt and self.uncertain
                    else ("absent", "removed")
                )
            return "eligible", "qualified"

        self.fixture.host.side_effect = host

    def post(
        self, director: bool = True, key: str = "", operation: str | None = None, **body: object
    ) -> Response:
        app = FastAPI()

        def error(**kwargs: object) -> HTTPException:
            return HTTPException(status_code=cast(int, kwargs["status_code"]), detail=kwargs)

        routes.register_testing_candidate_release_route(
            cast(ApiRouteRegistrar, cast(object, app)),
            dependencies=routes.TestingCandidateReleaseDependencies(
                read_identity=lambda: LocalOperatorIdentity(
                    "test-agent", "test", director_agent=director
                ),
                get_record_store=lambda: self.store,
                next_trace_id=lambda: "release-test",
                http_error=error,
                control_plane_root=self.root,
                error_response_model=BaseModel,
            ),
        )
        operation = operation or self.fixture.candidate.operation_id
        return asyncio.run(
            request(
                app,
                "POST",
                f"/v1/products/{self.fixture.candidate.product}/testing-rebuild/{operation}/candidate-release",
                payload=body,
                headers={"Idempotency-Key": key},
            )
        )


class CandidateReleaseRouteTests(CandidateReleaseFixture):
    def test_dry_run_apply_readback_and_wrong_identity_release_once(self) -> None:
        self.assertEqual(self.post(director=False).status_code, 403)
        dry = self.post()
        self.assertEqual(dry.status_code, 200, dry.text)
        self.assertEqual(dry.json()["status"], "eligible")
        self.assertEqual(dry.json()["candidates"][0]["namespace"], self.fixture.candidate.namespace)
        reviewed = dry.json()["plan_sha256"]
        stale = self.post(key="release", mode="apply", reviewed_plan_sha256="c" * 64)
        self.assertEqual(stale.status_code, 409, stale.text)
        applied = self.post(key="release", mode="apply", reviewed_plan_sha256=reviewed)
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["status"], "released")
        readback = self.post(key="release", mode="readback", reviewed_plan_sha256=reviewed)
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(
            (
                readback.json()["status"],
                readback.json()["replayed"],
                readback.json()["plan_sha256"],
            ),
            ("released", True, reviewed),
        )
        self.assertEqual(len(self.applied), 1)

    def test_current_use_refuses_before_effect_and_releases_reservation(self) -> None:
        reviewed = self.post().json()["plan_sha256"]
        self.usage.side_effect = TestingCandidateReleaseRefused(
            "testing_in_use", "A person is using it."
        )
        refused = self.post(key="release", mode="apply", reviewed_plan_sha256=reviewed)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        self.usage.side_effect = None
        applied = self.post(key="release", mode="apply", reviewed_plan_sha256=reviewed)
        self.assertEqual(applied.json()["status"], "released", applied.text)

    def test_unknown_original_only_reconciles_without_repeating_removal(self) -> None:
        reviewed = self.post().json()["plan_sha256"]
        self.interrupt = True
        for mode in ("apply", "readback", "apply"):
            response = self.post(key="release", mode=mode, reviewed_plan_sha256=reviewed)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], "mutation_reconciliation_required")
        self.assertEqual(len(self.applied), 1)
        self.usage.side_effect = TestingCandidateReleaseRefused(
            "testing_in_use", "A person is using it."
        )
        self.uncertain = False
        observed = self.post(key="release", mode="readback", reviewed_plan_sha256=reviewed)
        self.assertEqual(observed.json()["status"], "released", observed.text)
        self.assertEqual(observed.json()["plan_sha256"], reviewed)
        self.assertEqual(len(self.applied), 1)

    def test_missing_receipt_and_foreign_receiver_do_not_dispatch(self) -> None:
        missing = self.post(key="missing", mode="readback", reviewed_plan_sha256="a" * 64)
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(missing.json()["detail"]["code"], "release_receipt_unavailable")
        missing_receiver = self.post(receiver_operation_id="missing-original")
        self.assertEqual(missing_receiver.status_code, 409, missing_receiver.text)
        self.assertEqual(self.applied, [])

    def test_legacy_completed_receipt_replays_with_original_fingerprint(self) -> None:
        from control_plane.provider_operations import (
            ProviderMutationOutcome,
            ProviderObservation,
            run_durable_provider_operation,
        )
        from control_plane.service_auth import launchplane_identity_actor
        from control_plane.testing_candidate_allocation import candidate_resources
        from control_plane.contracts.testing_candidate_release import (
            TESTING_CANDIDATE_RELEASE_ROUTE,
        )

        actor = launchplane_identity_actor(
            LocalOperatorIdentity("test-agent", "test", director_agent=True)
        )
        candidate = self.fixture.candidate
        plan = TestingCandidateReleasePlan(
            product=candidate.product,
            operation_id=candidate.operation_id,
            error_code="candidate_preparation_failed",
            namespace=candidate.namespace,
            resources_binding_sha256="b" * 64,
        )
        old_request = json.dumps(
            {"mode": "apply", "reviewed_plan_sha256": plan.digest()}, separators=(",", ":")
        )
        fingerprint = hashlib.sha256(
            "\n".join((actor, candidate.product, candidate.operation_id, old_request)).encode()
        ).hexdigest()
        receipt = json.dumps(
            {
                "plan": plan.model_dump(mode="json", exclude={"retention_plan_sha256"}),
                "resources": candidate_resources(candidate).model_dump(mode="json"),
                "server_id": "host",
                "reclaimable_bytes": 123,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        reconstructed = release.release_from_receipt(receipt)

        class LegacyAdapter:
            def target_key(self) -> str:
                return "testing-candidate-release:" + candidate.namespace

            def reconciliation_key(self) -> str:
                return receipt

            def observe(
                self,
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> ProviderObservation:
                return ProviderObservation("unknown")

            def apply(
                self, provider_operation_key: str, lease: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                lease.checkpoint_effect("legacy-fabricated-completion")
                return ProviderMutationOutcome(
                    202,
                    release.release_response(reconstructed, "old-trace", "released").model_dump(
                        mode="json"
                    ),
                )

        adapter = LegacyAdapter()
        run_durable_provider_operation(
            store=self.store,
            scope=f"testing-candidate-release:{candidate.product}:{candidate.operation_id}",
            route_path=TESTING_CANDIDATE_RELEASE_ROUTE,
            idempotency_key="legacy",
            request_fingerprint=fingerprint,
            lease_owner="legacy",
            response_trace_id="legacy",
            adapter=adapter,
        )
        response = self.post(key="legacy", mode="readback", reviewed_plan_sha256=plan.digest())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            (
                response.json()["status"],
                response.json()["replayed"],
                response.json()["plan_sha256"],
            ),
            ("released", True, plan.digest()),
        )
        self.assertEqual(self.applied, [])


class AutomaticCandidateReleaseTests(CandidateReleaseFixture):
    def release_failed(self, operation_id: str = "") -> None:
        release.release_failed_candidate(
            self.store, self.root, operation_id or self.fixture.candidate.operation_id
        )

    def view(self, operation_id: str = "") -> Any:
        return release.read_automatic_release(
            self.store,
            self.fixture.candidate.product,
            operation_id or self.fixture.candidate.operation_id,
        )

    def original(self) -> Any:
        return self.store.read_odoo_stable_target_replacement_operation_record(
            self.fixture.candidate.operation_id
        )

    def forget_automatic_receipt(self) -> None:
        from sqlalchemy import delete
        from control_plane.storage.postgres import LaunchplaneIdempotencyRow

        with self.store._session_factory() as session:
            session.execute(
                delete(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.idempotency_key == release.AUTOMATIC_RELEASE_KEY
                )
            )
            session.commit()

    def test_failed_rebuild_releases_its_eligible_candidate_once(self) -> None:
        before = self.original()
        self.release_failed()
        view = self.view()
        self.assertEqual((view.status, view.reasons), ("released", ()))
        self.release_failed()
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.original(), before)
        readback = self.post(
            key=release.AUTOMATIC_RELEASE_KEY,
            mode="readback",
            reviewed_plan_sha256=view.plan_sha256,
        )
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(
            (readback.json()["status"], readback.json()["replayed"]), ("released", True)
        )
        self.assertNotIn("reason_code", readback.json())

    def test_each_refusal_keeps_the_candidate_and_names_it(self) -> None:
        lane_refusals = (
            "testing_needed_copy",
            "testing_production_resources_shared",
            "testing_staff_hold",
            "testing_in_use",
        )
        for code in (*lane_refusals, "candidate_release_selected", "candidate_release_unavailable"):
            with self.subTest(code=code):
                self.usage.side_effect, self.routed.side_effect = None, None
                self.routed.return_value = code == "candidate_release_selected"
                if code in lane_refusals:
                    self.usage.side_effect = TestingCandidateReleaseRefused(code, "Kept.")
                if code == "candidate_release_unavailable":
                    self.routed.side_effect = OSError("Fabricated provider read failure")
                self.forget_automatic_receipt()
                self.release_failed()
                view = self.view()
                self.assertEqual(view.status, "held")
                self.assertIn(code, view.reasons)
                self.assertEqual(self.applied, [])

    def test_change_after_planning_settles_held_without_effect(self) -> None:
        planned: list[bool] = []

        def changed_use(*_args: object) -> None:
            if planned:
                raise TestingCandidateReleaseRefused("testing_in_use", "A person arrived.")
            planned.append(True)

        self.usage.side_effect = changed_use
        self.release_failed()
        view = self.view()
        self.assertEqual(view.status, "held")
        self.assertEqual(view.reasons[:2], ("candidate_release_held", "testing_in_use"))
        self.assertEqual(self.applied, [])

    def test_concurrent_executor_settles_held_without_effect(self) -> None:
        self.executor.side_effect = ValueError("The original still has an executor.")
        self.release_failed()
        self.assertEqual(self.view().status, "held")
        self.assertEqual(self.applied, [])

    def test_manual_apply_cannot_dispatch_through_the_automatic_key(self) -> None:
        reviewed = self.post().json()["plan_sha256"]
        manual = self.post(
            key=release.AUTOMATIC_RELEASE_KEY, mode="apply", reviewed_plan_sha256=reviewed
        )
        self.assertEqual(manual.status_code, 409, manual.text)
        self.assertEqual(manual.json()["detail"]["code"], "idempotency_key_reused")
        self.assertIsNone(self.view())
        self.assertEqual(self.applied, [])

    def test_uncertain_automatic_effect_is_only_observed(self) -> None:
        self.interrupt = True
        self.release_failed()
        view = self.view()
        self.assertEqual(
            (view.status, view.reasons), ("unknown", ("mutation_reconciliation_required",))
        )
        self.release_failed()
        manual = self.post(
            key=release.AUTOMATIC_RELEASE_KEY, mode="apply", reviewed_plan_sha256=view.plan_sha256
        )
        self.assertEqual(manual.status_code, 409, manual.text)
        self.assertEqual(len(self.applied), 1)
        self.uncertain = False
        observed = self.post(
            key=release.AUTOMATIC_RELEASE_KEY,
            mode="readback",
            reviewed_plan_sha256=view.plan_sha256,
        )
        self.assertEqual(observed.json()["status"], "released", observed.text)
        self.assertEqual(self.view().status, "released")
        self.assertEqual(len(self.applied), 1)

    def test_only_a_failed_native_testing_rebuild_with_a_cause_is_released(self) -> None:
        failed = self.original()
        for changes in (
            {"status": "pass", "phase": "completed", "error_code": ""},
            {"status": "cancelled", "phase": "cancelled"},
            {"status": "reconciliation_required", "phase": "running", "finished_at": ""},
            {"error_code": ""},
            {"instance": "prod"},
        ):
            with (
                self.subTest(changes=changes),
                patch.object(
                    self.store,
                    "read_odoo_stable_target_replacement_operation_record",
                    return_value=failed.model_copy(update=changes),
                ),
            ):
                self.release_failed()
                self.assertIsNone(self.view())
                self.fixture.host.assert_not_called()


if __name__ == "__main__":
    unittest.main()
