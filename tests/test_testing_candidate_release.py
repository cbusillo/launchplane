import asyncio
import base64
from dataclasses import replace
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
from control_plane.provider_operations import ProviderOperationLease
from control_plane.service_auth import LocalOperatorIdentity
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import request
from control_plane.odoo_testing_resources import OdooTestingResources
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
    print(json.dumps([{'Name':args[2],'Driver':'local','Labels':item['labels'],'Mountpoint':item['path']}]))
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

DU_FIXTURE = '#!/bin/sh\necho 1000 "$3"\n'


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
        for name, content in (("docker", DOCKER_FIXTURE), ("du", DU_FIXTURE)):
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

    def test_dry_run_reports_reclaimable_bytes_and_removes_nothing(self) -> None:
        self.assertEqual(
            self.run_program("dry-run"), {"status": "eligible", "reclaimable_bytes": 3500}
        )
        self.assertEqual(self.removals(), [])

    def test_apply_releases_only_the_candidates_containers_and_volumes(self) -> None:
        self.state["containers"]["other-service"] = {
            "Config": {"Labels": {"com.docker.compose.project": "fixture-testing"}},
            "Mounts": [{"Type": "volume", "Name": "fixture-testing-data", "Source": "/srv/other"}],
        }
        self.state["volumes"]["fixture-testing-data"] = {"labels": {}, "path": "/srv/other"}
        self.save()
        self.assertEqual(self.run_program("apply")["status"], "released")
        self.assertEqual(set(self.state["containers"]), {"other-service"})
        self.assertEqual(set(self.state["volumes"]), {"fixture-testing-data"})
        self.assertEqual(self.run_program("dry-run"), {"status": "absent", "reclaimable_bytes": 0})

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
            self.run_program("apply"),
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
            self.run_program("apply"),
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

    def test_only_a_failed_testing_rebuild_with_a_recorded_cause_is_releasable(self) -> None:
        for operation, code in (
            (failed_operation(is_native_testing_rebuild=False), "candidate_release_not_testing"),
            (failed_operation(product="other"), "candidate_release_not_testing"),
            (failed_operation(status="running"), "candidate_release_outcome_unrecorded"),
            (failed_operation(status="cancelled"), "candidate_release_outcome_unrecorded"),
            (failed_operation(error_code=""), "candidate_release_outcome_unrecorded"),
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
            ({"publication": Mock(state="planned")}, "candidate_release_selected"),
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
        with (
            patch.object(release, "require_candidate_target_custody"),
            patch.object(release, "read_publication", return_value=Mock(state="reversed")),
            patch.object(release, "candidate_routed", return_value=False),
        ):
            release.require_candidate_unselected(Path("/unused"), Mock(), allocation)

    def lane_refusal(
        self, *, only_copy: bool = False, shared: bool = False, protected: bool = False
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
                release, "evaluate_lane_usage", return_value=Mock(protected=protected)
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
        self.assertIsNone(self.lane_refusal())

    def test_plan_is_stable_after_release_so_readback_matches_its_review(self) -> None:
        store = Mock()
        store.read_odoo_stable_target_replacement_operation_record.return_value = failed_operation()
        allocation = Mock(
            product="fixture",
            context="fixture-testing",
            operation_id="operation-1",
            namespace=NAMESPACE,
            compose_id="compose-1",
            server_id="server-1",
        )
        store.read_testing_candidate.return_value = allocation
        candidate = OdooTestingResources.model_validate(resources())
        plans = []
        for status, size in (("eligible", 3500), ("absent", 0)):
            with (
                patch.object(release, "require_candidate_unselected"),
                patch.object(release, "require_lane_releasable"),
                patch.object(release, "candidate_resources", return_value=candidate),
                patch.object(
                    release,
                    "host_release",
                    return_value=release.HostRelease(status, size, ""),
                ),
            ):
                built = release.build_candidate_release(
                    Path("/unused"), store, "fixture", "operation-1"
                )
            self.assertEqual((built.status, built.reclaimable_bytes), (status, size))
            plans.append(built.plan.digest())
        self.assertEqual(plans[0], plans[1])

    def test_held_host_evidence_refuses_the_plan(self) -> None:
        store = Mock()
        store.read_odoo_stable_target_replacement_operation_record.return_value = failed_operation()
        store.read_testing_candidate.return_value = Mock(compose_id="compose-1")
        with (
            patch.object(release, "require_candidate_unselected"),
            patch.object(release, "require_lane_releasable"),
            patch.object(release, "candidate_resources"),
            patch.object(
                release,
                "host_release",
                return_value=release.HostRelease("held", None, "volume_has_other_consumer"),
            ),
        ):
            self.assertEqual(
                self.refusal(
                    lambda: release.build_candidate_release(
                        Path("/unused"), store, "fixture", "operation-1"
                    )
                ),
                "candidate_release_held",
            )


class CandidateReleaseRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'records.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.host_status = "eligible"
        self.refusal: TestingCandidateReleaseRefused | None = None
        self.applied: list[str] = []
        candidate = release.CandidateRelease(
            TestingCandidateReleasePlan(
                product="fixture",
                operation_id="operation-1",
                error_code="candidate_preparation_failed",
                namespace=NAMESPACE,
                resources_binding_sha256="b" * 64,
            ),
            OdooTestingResources.model_validate(resources()),
            "server-1",
            "eligible",
            3500,
        )

        def plan(*_: object) -> release.CandidateRelease:
            return replace(candidate, status=self.host_status)

        def replan(*_: object) -> release.CandidateRelease:
            if self.refusal is not None:
                raise self.refusal
            return plan()

        def host(
            _root: Path,
            _server: str,
            _resources: OdooTestingResources,
            mode: str,
            lease: ProviderOperationLease | None = None,
        ) -> release.HostRelease:
            if mode == "apply":
                assert lease is not None
                lease.checkpoint_effect("release-candidate")
                self.applied.append(mode)
                self.host_status = "absent"
                return release.HostRelease("released", 3500, "")
            return release.HostRelease(self.host_status, 0, "")

        self.enterContext(patch.object(routes, "build_candidate_release", side_effect=plan))
        self.enterContext(patch.object(release, "build_candidate_release", side_effect=replan))
        self.enterContext(patch.object(release, "host_release", side_effect=host))

    def post(self, director: bool = True, key: str = "", **body: object) -> Response:
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
        return asyncio.run(
            request(
                app,
                "POST",
                "/v1/products/fixture/testing-rebuild/operation-1/candidate-release",
                payload=body,
                headers={"Idempotency-Key": key},
            )
        )

    def test_dry_run_apply_and_readback_release_once(self) -> None:
        self.assertEqual(self.post(director=False).status_code, 403)
        dry = self.post()
        self.assertEqual(dry.status_code, 200, dry.text)
        self.assertEqual(
            (dry.json()["status"], dry.json()["reclaimable_bytes"]), ("eligible", 3500)
        )
        reviewed = dry.json()["plan_sha256"]
        stale = self.post(key="release", mode="apply", reviewed_plan_sha256="c" * 64)
        self.assertEqual(stale.status_code, 409, stale.text)
        applied = self.post(key="release", mode="apply", reviewed_plan_sha256=reviewed)
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["status"], "released")
        readback = self.post(key="release", mode="readback", reviewed_plan_sha256=reviewed)
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(
            (readback.json()["status"], readback.json()["replayed"]), ("released", True)
        )
        self.assertEqual(self.applied, ["apply"])

    def test_guard_refusing_at_apply_releases_nothing_and_leaves_the_key_usable(self) -> None:
        reviewed = self.post().json()["plan_sha256"]
        self.refusal = TestingCandidateReleaseRefused("testing_in_use", "A person is using it.")
        refused = self.post(key="release", mode="apply", reviewed_plan_sha256=reviewed)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "testing_in_use")
        self.assertEqual(self.applied, [])
        self.refusal = None
        applied = self.post(key="release", mode="apply", reviewed_plan_sha256=reviewed)
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(self.applied, ["apply"])


if __name__ == "__main__":
    unittest.main()
