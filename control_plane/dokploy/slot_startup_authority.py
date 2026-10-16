"""Durable, value-free Compose coordinates captured before writer recreation."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.dokploy.slot_maintenance import ContainerCommand


class ComposeServiceAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    writer_id: str
    service: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class ComposeProjectAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    slot_id: str
    project: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    directory: str
    files: tuple[str, ...] = Field(min_length=1)
    services: tuple[ComposeServiceAuthority, ...] = Field(min_length=1)

    @field_validator("directory")
    @classmethod
    def absolute_directory(cls, value: str) -> str:
        return absolute_path(value)

    @field_validator("files")
    @classmethod
    def absolute_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(absolute_path(value) for value in values)

    @property
    def argv(self) -> tuple[str, ...]:
        result: tuple[str, ...] = (
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            self.directory,
        )
        for path in self.files:
            result += ("--file", path)
        return result

    @property
    def env_file(self) -> str:
        return str(PurePosixPath(self.directory) / ".env")


def absolute_path(value: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValueError("startup authority requires absolute canonical paths")
    return value


def plan_digest(plan: OdooCutoverPlan) -> str:
    return hashlib.sha256(plan.model_dump_json().encode()).hexdigest()


# The host supplies a private, persistent directory, shared by its API/worker
# transports. Publish once before effects; neither secrets nor Docker IDs live
# here. No existing authority is overwritten, including after a lost reply.
AUTHORITY_PROGRAM = """
import json,os,stat,sys,tempfile
from pathlib import Path
request=json.load(sys.stdin)
root=Path(request['directory'])
if not root.is_absolute() or root.resolve()!=root or not root.is_dir():
    raise ValueError('startup authority directory is unavailable or noncanonical')
path=root/(request['digest']+'.json')
if path.is_symlink(): raise ValueError('startup authority cannot be a symlink')
def read():
    if not path.exists(): return None
    if not stat.S_ISREG(path.stat().st_mode): raise ValueError('invalid startup authority')
    return json.loads(path.read_text())
previous=read()
if 'authority' in request:
    desired=request['authority']
    if previous is not None and previous!=desired:
        raise ValueError('durable Compose authority changed')
    if previous is None:
        descriptor,temporary=tempfile.mkstemp(dir=root,prefix='.startup-')
        try:
            with os.fdopen(descriptor,'w') as handle:
                json.dump(desired,handle,sort_keys=True);handle.flush();os.fsync(handle.fileno())
            try: os.link(temporary,path)
            except FileExistsError: pass
            directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally: os.unlink(temporary)
    if read()!=desired: raise ValueError('durable Compose authority readback differs')
print(json.dumps(read()))
"""


class StartupComposeAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan_sha256: str
    projects: tuple[ComposeProjectAuthority, ComposeProjectAuthority]


def read_authority(
    plan: OdooCutoverPlan,
    directory: str,
    execute: ContainerCommand,
    authority: StartupComposeAuthority | None = None,
) -> StartupComposeAuthority | None:
    request: dict[str, object] = {
        "directory": absolute_path(directory),
        "digest": plan_digest(plan),
    }
    if authority is not None:
        request["authority"] = authority.model_dump(mode="json")
    raw = json.loads(execute(("python3", "-c", AUTHORITY_PROGRAM), json.dumps(request)))
    if raw is None:
        return None
    result = StartupComposeAuthority.model_validate(raw)
    if result.plan_sha256 != plan_digest(plan):
        raise ValueError("durable Compose authority belongs to another release")
    return result
