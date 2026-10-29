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

from control_plane._retained_volume_sweep_program import RETAINED_VOLUME_SWEEP_HOST_PROGRAM


DOCKER_FIXTURE = r"""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
state_path=Path(os.environ['FAKE_DOCKER_STATE'])
state=json.loads(state_path.read_text())
args=sys.argv[1:]
if state.get('fail') and args[:2] not in (['volume','rm'],):
    print('access denied',file=sys.stderr);raise SystemExit(1)
root=state['root'];name=state['name']
def volume(value,point=None):
    return {'Name':value,'CreatedAt':'2026-01-01T00:00:00Z','Driver':'local','Labels':{},'Mountpoint':point or root+'/volumes/'+value+'/_data','Options':{}}
if args[:2]==['info','--format']:
    print(json.dumps({'ID':state.get('engine','engine-one'),'DockerRootDir':root,'Swarm':{'LocalNodeState':'active','ControlAvailable':True}}))
elif args[:3]==['ps','-aq','--no-trunc']:
    sys.stdout.write('\n'.join(state.get('containers',[])))
elif args[0]=='inspect' and len(args)==2:
    mounts=[]
    if state.get('container_ref'):
        mounts=[{'Type':'volume','Name':name,'Source':root+'/volumes/'+name+'/_data'}]
    print(json.dumps([{'Id':args[1],'Mounts':mounts}]))
elif args[:3]==['service','ls','-q']:
    sys.stdout.write('\n'.join(state.get('services',[])))
elif args[:2]==['service','inspect']:
    mounts=[{'Type':'volume','Source':name}] if state.get('service_ref') else []
    print(json.dumps([{'ID':args[2],'Spec':{'TaskTemplate':{'ContainerSpec':{'Mounts':mounts}}}}]))
elif args[:4]==['service','ps','-q','--no-trunc']:
    print('task-one' if state.get('task') else '')
elif args[:3]==['inspect','--type','task']:
    mounts=[{'Type':'volume','Source':name}] if state.get('task_ref') else []
    print(json.dumps([{'ID':args[3],'ServiceID':'service-one','Spec':{'ContainerSpec':{'Mounts':mounts}}}]))
elif args[:3]==['node','ls','-q']:
    print('node-one')
elif args[:3]==['volume','ls','-q']:
    values=[] if state.get('removed') else [name]
    if state.get('alias'):
        values.append('alias')
    sys.stdout.write('\n'.join(values))
elif args[:2]==['volume','inspect']:
    target=args[2]
    if target==name and state.get('removed'):
        print('not found',file=sys.stderr);raise SystemExit(1)
    point=root+'/volumes/'+name+'/_data' if target=='alias' and state.get('alias') else None
    print(json.dumps([volume(target,point)]))
elif args[:2]==['volume','rm']:
    state['rm_args']=args
    if args!=['volume','rm',name]:
        state_path.write_text(json.dumps(state));raise SystemExit(2)
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
        value = {
            "volume": self.volume(),
            "engine_id": self.state.get("engine", "engine-one"),
            "backing_device": metadata.st_dev,
            "backing_inode": metadata.st_ino,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def run_program(
        self, mode: str = "dry-run", identity: str | None = None, operation: str = "provider/key"
    ) -> dict[str, object]:
        spec = {
            "mode": mode,
            "volume": "candidate",
            "identity": identity or self.identity(),
            "operation": operation,
        }
        payload = base64.b64encode(json.dumps(spec).encode()).decode()
        environment = os.environ | {
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FAKE_DOCKER_STATE": str(self.state_path),
        }
        result = subprocess.run(
            [sys.executable, "-c", self.fixture_program(), payload, "FRAME:"],
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
            check=True,
        )
        self.assertTrue(result.stdout.startswith("FRAME:"), result)
        self.assertNotIn(str(self.data), result.stdout)
        return cast(dict[str, object], json.loads(result.stdout.removeprefix("FRAME:"))["result"])

    def fixture_program(self) -> str:
        return (
            "from pathlib import Path\nfrom unittest.mock import patch\n"
            "read_text=Path.read_text\n"
            "def fixture_read(path,*args,**kwargs):\n"
            "    if path==Path('/proc/self/mountinfo'):\n"
            "        return '1 0 8:1 / / rw - ext4 /dev/sda rw\\n'\n"
            "    return read_text(path,*args,**kwargs)\n"
            "with patch.object(Path,'read_text',fixture_read):\n"
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

    def test_nonempty_and_stopped_container_are_held(self) -> None:
        (self.data / "private-name").write_text("private-content")
        self.assertEqual(self.run_program()["status"], "held")
        (self.data / "private-name").unlink()
        self.state.update(containers=["stopped-one"], container_ref=True)
        self.write_state()
        self.assertEqual(self.run_program()["status"], "held")

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
        self.assertEqual(self.run_program(), {"reason_code": "qualified", "status": "eligible"})


if __name__ == "__main__":
    unittest.main()
