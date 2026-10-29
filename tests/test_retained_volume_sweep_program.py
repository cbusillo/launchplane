import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import cast

from control_plane.retained_volume_sweep_program import RETAINED_VOLUME_SWEEP_HOST_PROGRAM
from control_plane.retained_volume_capture import (
    retained_volume_engine_identity,
    retained_volume_identity,
)


DOCKER_FIXTURE = r"""#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
state_path=Path(os.environ['FAKE_DOCKER_STATE'])
state=json.loads(state_path.read_text())
args=sys.argv[1:]
state.setdefault('calls',[]).append(args);state_path.write_text(json.dumps(state))
if state.get('fail') and args[:2] not in (['volume','rm'],):
    print('access denied',file=sys.stderr);raise SystemExit(1)
root=state['root'];name=state['name']
def volume(value,point=None):
    return {'Name':value,'CreatedAt':'2026-01-01T00:00:00Z','Driver':'local','Labels':{},'Mountpoint':point or root+'/volumes/'+value+'/_data','Options':{}}
if args[:2]==['info','--format']:
    print(json.dumps({'ID':state.get('engine','engine-one'),'DockerRootDir':root,'Swarm':{'LocalNodeState':'active','ControlAvailable':True}}))
elif args[:3]==['ps','-aq','--no-trunc']:
    sys.stdout.write('\n'.join(state.get('containers',[])))
elif args[:3]==['inspect','--type','container']:
    rows=[]
    for identifier in args[3:]:
        mounts=[]
        if state.get('container_ref'):
            mounts=[{'Type':'volume','Name':name,'Source':root+'/volumes/'+name+'/_data'}]
        if state.get('bind_ref'):
            mounts=[{'Type':'bind','Source':root+'/volumes/'+name}]
        rows.append({'Id':identifier,'Mounts':mounts})
    if state.get('malformed_containers'):
        rows=rows[:-1]
    if state.get('duplicate_containers') and rows:
        rows.append(rows[0])
    print(json.dumps(rows))
elif args[:3]==['service','ls','-q']:
    sys.stdout.write('\n'.join(state.get('services',[])))
elif args[:3]==['inspect','--type','service']:
    mounts=[{'Type':'volume','Source':name}] if state.get('service_ref') else []
    print(json.dumps([{'ID':identifier,'Spec':{'TaskTemplate':{'ContainerSpec':{'Mounts':mounts}}}} for identifier in args[3:]]))
elif args[:4]==['service','ps','-q','--no-trunc']:
    if state.get('task_per_service'):
        print('\n'.join('task-'+service for service in args[4:]))
    else:
        print('task-one' if state.get('task') else '')
elif args[:3]==['inspect','--type','task']:
    mounts=[{'Type':'volume','Source':name}] if state.get('task_ref') else []
    print(json.dumps([{'ID':identifier,'ServiceID':identifier.removeprefix('task-') if state.get('task_per_service') else 'service-one','NodeID':'','Spec':{'ContainerSpec':{'Mounts':mounts}}} for identifier in args[3:]]))
elif args[:3]==['node','ls','-q']:
    print('node-one')
elif args[:3]==['volume','ls','-q']:
    values=[] if state.get('removed') else [name]
    values.extend(state.get('extra_volumes',[]))
    if state.get('alias'):
        values.append('alias')
    sys.stdout.write('\n'.join(values))
elif args[:3]==['inspect','--type','volume']:
    targets=args[3:]
    if name in targets and state.get('removed'):
        print('not found',file=sys.stderr);raise SystemExit(1)
    print(json.dumps([volume(target,root+'/volumes/'+name+'/_data' if target=='alias' and state.get('alias') else None) for target in targets]))
elif args[:2]==['volume','rm']:
    state['rm_args']=args
    if args!=['volume','rm',name]:
        state_path.write_text(json.dumps(state));raise SystemExit(2)
    if state.get('rm_timeout'):
        time.sleep(11)
    if 'clock_after_rm' in state:
        state['clock']=state['clock_after_rm']
    if state.get('rm_fail'):
        state_path.write_text(json.dumps(state));raise SystemExit(1)
    state['removed']=True;state_path.write_text(json.dumps(state));print(name)
else:
    print('unsupported '+repr(args),file=sys.stderr);raise SystemExit(2)
"""


class RetainedVolumeSweepProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.docker_root = self.root / "docker"
        self.data = self.docker_root / "volumes" / "candidate" / "_data"
        self.data.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(DOCKER_FIXTURE)
        docker.chmod(0o755)
        self.state_path = self.root / "state.json"
        self.state: dict[str, object] = {
            "root": str(self.docker_root),
            "name": "candidate",
        }
        self.write_state()

    def write_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def volume(self) -> dict[str, object]:
        return {
            "Name": "candidate",
            "CreatedAt": "2026-01-01T00:00:00Z",
            "Driver": "local",
            "Labels": {},
            "Mountpoint": str(self.data),
            "Options": {},
        }

    def identity(self) -> str:
        metadata = self.data.stat()
        engine_id = self.state.get("engine", "engine-one")
        assert isinstance(engine_id, str)
        return retained_volume_identity(
            self.volume(),
            engine_id,
            metadata.st_dev,
            metadata.st_ino,
        )

    def run_program(
        self,
        mode: str = "dry-run",
        identity: str | None = None,
        operation: str = "provider/key",
        exhaust_budget: bool = False,
        engine_identity: str | None = None,
    ) -> dict[str, object]:
        engine_id = self.state.get("engine", "engine-one")
        assert isinstance(engine_id, str)
        spec = {
            "mode": mode,
            "volume": "candidate",
            "identity": identity or self.identity(),
            "operation": operation,
            "engine_identity": (
                retained_volume_engine_identity(engine_id)
                if engine_identity is None
                else engine_identity
            ),
        }
        payload = base64.b64encode(json.dumps(spec).encode()).decode()
        environment = os.environ | {
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FAKE_DOCKER_STATE": str(self.state_path),
        }
        result = subprocess.run(
            [sys.executable, "-c", self.fixture_program(exhaust_budget), payload, "FRAME:"],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            check=True,
        )
        self.assertTrue(result.stdout.startswith("FRAME:"), result)
        self.assertNotIn(str(self.data), result.stdout)
        return cast(dict[str, object], json.loads(result.stdout.removeprefix("FRAME:"))["result"])

    @staticmethod
    def fixture_program(exhaust_budget: bool = False) -> str:
        return (
            "from pathlib import Path\nfrom unittest.mock import patch\nimport json,os\n"
            "read_text=Path.read_text\nmount_reads=[0]\n"
            "def fixture_read(path,*args,**kwargs):\n"
            "    if path==Path('/proc/self/mountinfo'):\n"
            "        mount_reads[0]+=1\n"
            "        return '1 0 8:1 / / rw - ext4 /dev/sda rw\\n'\n"
            "    return read_text(path,*args,**kwargs)\n"
            "def fixture_clock():\n"
            f"    if {exhaust_budget!r} and mount_reads[0]>=2: return 1e9\n"
            "    state=json.loads(read_text(Path(os.environ['FAKE_DOCKER_STATE'])))\n"
            "    return float(state.get('clock',len(state.get('calls',[]))))\n"
            "with patch.object(Path,'read_text',fixture_read), patch('time.monotonic',fixture_clock):\n"
            f"    exec({RETAINED_VOLUME_SWEEP_HOST_PROGRAM!r})\n"
        )

    def test_started_receipt_with_a_present_volume_remains_unknown(self) -> None:
        directory = self.home / ".local" / "state" / "launchplane-retained-volume-sweep"
        directory.mkdir(parents=True)
        operation = "provider/key"
        digest = self.identity()
        key = hashlib.sha256((operation + "\0" + digest).encode()).hexdigest()
        (directory / (key + ".json")).write_text(
            json.dumps(
                {
                    "operation": operation,
                    "identity": digest,
                    "volume": "candidate",
                    "started": True,
                    "engine_id": "engine-one",
                }
            )
        )
        self.assertEqual(self.run_program("readback")["status"], "unknown")
        self.assertEqual(self.run_program("apply")["status"], "unknown")
        self.assertNotIn("rm_args", json.loads(self.state_path.read_text()))

    def test_empty_unattached_volume_is_removed_and_original_key_reads_back(self) -> None:
        self.assertEqual(self.run_program("apply")["status"], "removed")
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["rm_args"], ["volume", "rm", "candidate"])
        self.assertEqual(self.run_program("readback")["status"], "absent")
        self.assertEqual(self.run_program("apply")["status"], "absent")

    def test_fresh_dry_run_accepts_absent_but_original_missing_receipt_stays_unknown(self) -> None:
        self.state["removed"] = True
        self.write_state()

        self.assertEqual(
            self.run_program(operation="provider/fresh"),
            {"status": "absent", "reason_code": "already_absent"},
        )
        self.assertEqual(
            self.run_program("readback", operation="provider/original"),
            {"status": "unknown", "reason_code": "inventory_unavailable"},
        )
        self.assertNotIn("rm_args", json.loads(self.state_path.read_text()))

    def test_fresh_absence_requires_matching_captured_engine_identity(self) -> None:
        self.state["removed"] = True
        self.write_state()

        self.assertEqual(self.run_program()["status"], "absent")
        self.assertEqual(self.run_program(engine_identity="0" * 64)["status"], "unknown")
        self.assertEqual(self.run_program(engine_identity="")["status"], "unknown")

    def test_confirmed_no_effect_and_never_started_readbacks_settle_without_removal(self) -> None:
        self.assertEqual(self.run_program("readback")["reason_code"], "never_started")
        self.state["rm_fail"] = True
        self.write_state()
        self.assertEqual(self.run_program("apply")["reason_code"], "no_effect")
        self.assertEqual(self.run_program("readback")["reason_code"], "no_effect")
        self.assertFalse(json.loads(self.state_path.read_text()).get("removed", False))

    def test_late_failed_removal_gets_a_fresh_no_effect_classification_budget(self) -> None:
        self.state.update(rm_fail=True, clock_after_rm=80)
        self.write_state()

        result = self.run_program("apply")

        self.assertEqual(result, {"status": "held", "reason_code": "no_effect"})
        self.assertEqual(self.run_program("readback")["reason_code"], "no_effect")
        self.assertFalse(json.loads(self.state_path.read_text()).get("removed", False))

    def test_late_successful_removal_gets_a_fresh_absence_budget(self) -> None:
        self.state["clock_after_rm"] = 80
        self.write_state()

        result = self.run_program("apply")

        self.assertEqual(result, {"status": "removed", "reason_code": "removed"})
        self.assertTrue(json.loads(self.state_path.read_text())["removed"])

    def test_started_removal_timeout_remains_unknown(self) -> None:
        self.state["rm_timeout"] = True
        self.write_state()

        result = self.run_program("apply")

        self.assertEqual(result["status"], "unknown")
        receipts = list(
            (self.home / ".local" / "state" / "launchplane-retained-volume-sweep").glob("*.json")
        )
        self.assertEqual(len(receipts), 1)
        self.assertNotIn("outcome", json.loads(receipts[0].read_text()))

    def test_nonempty_and_stopped_container_are_held(self) -> None:
        (self.data / "private-name").write_text("private-content")
        self.assertEqual(self.run_program()["status"], "held")
        (self.data / "private-name").unlink()
        self.state.update(containers=["stopped-one"], container_ref=True)
        self.write_state()
        self.assertEqual(self.run_program()["status"], "held")

    def test_large_full_inventory_is_batched_and_complete_before_removal(self) -> None:
        containers = [f"stopped-{index}" for index in range(300)]
        services = [f"service-{index}" for index in range(200)]
        extra_volumes = [f"other-{index}" for index in range(200)]
        for name in extra_volumes:
            (self.docker_root / "volumes" / name / "_data").mkdir(parents=True)
        self.state.update(
            containers=containers,
            services=services,
            task_per_service=True,
            extra_volumes=extra_volumes,
        )
        self.write_state()
        self.assertEqual(self.run_program("apply")["status"], "removed")
        calls = json.loads(self.state_path.read_text())["calls"]
        expected = {
            "container": containers,
            "service": services,
            "task": ["task-" + service for service in services],
            "volume": ["candidate", *extra_volumes],
        }
        for kind, identifiers in expected.items():
            inspections = [call for call in calls if call[:3] == ["inspect", "--type", kind]]
            inspected = [identifier for call in inspections for identifier in call[3:]]
            self.assertEqual(set(inspected), set(identifiers))
            self.assertLess(len(inspections), len(identifiers))
        task_lists = [call for call in calls if call[:4] == ["service", "ps", "-q", "--no-trunc"]]
        self.assertLess(len(task_lists), len(services))

    def test_bind_ancestor_and_malformed_or_duplicate_batches_fail_closed(self) -> None:
        self.state.update(containers=["stopped-one"], bind_ref=True)
        self.write_state()
        self.assertEqual(self.run_program()["status"], "held")
        for field in ("malformed_containers", "duplicate_containers"):
            self.state.pop("bind_ref", None)
            self.state[field] = True
            self.write_state()
            self.assertEqual(self.run_program()["status"], "unknown")
            self.state.pop(field)

    def test_pre_dispatch_budget_exhaustion_has_no_receipt_or_rm(self) -> None:
        self.assertEqual(self.run_program("apply", exhaust_budget=True)["status"], "held")
        state = json.loads(self.state_path.read_text())
        self.assertNotIn("rm_args", state)
        receipts = list(
            (self.home / ".local" / "state" / "launchplane-retained-volume-sweep").glob("*.json")
        )
        self.assertEqual(receipts, [])

    def test_service_and_task_references_are_held(self) -> None:
        for field in ("service_ref", "task_ref"):
            self.state.update(
                services=["service-one"], task=True, service_ref=False, task_ref=False
            )
            self.state[field] = True
            self.write_state()
            self.assertEqual(self.run_program()["status"], "held")

    def test_aliased_backing_and_changed_identity_are_held(self) -> None:
        self.state["alias"] = True
        self.write_state()
        self.assertEqual(self.run_program()["status"], "held")
        self.state.pop("alias")
        self.write_state()
        self.assertEqual(self.run_program(identity="0" * 64)["status"], "held")

    def test_unknown_inventory_and_readback_error_fail_closed(self) -> None:
        self.state["fail"] = True
        self.write_state()
        self.assertEqual(self.run_program()["status"], "unknown")
        self.assertEqual(self.run_program("readback")["status"], "unknown")

    def test_root_filesystem_mount_does_not_disqualify_candidate(self) -> None:
        self.assertEqual(self.run_program()["status"], "eligible")


if __name__ == "__main__":
    unittest.main()
