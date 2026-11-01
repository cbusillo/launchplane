import base64
from collections.abc import Callable
import json
import zlib

from control_plane.dokploy.native_copy_stream import (
    NativeSourceUnavailable,
    _read_remote,
    require_registered_remote_server,
)


CUTOVER_COMMAND_BYTES = 64 * 1024 * 1024

CUTOVER_COMMAND_PROGRAM = r"""
import base64,json,subprocess,sys,zlib
prefix=sys.argv[2]
def emit(value):
    print(prefix+json.dumps(value),flush=True)
try:
    envelope=json.loads(base64.b64decode(sys.argv[1],validate=True))
    decoder=zlib.decompressobj()
    raw=decoder.decompress(base64.b64decode(envelope['command'],validate=True),__OUTPUT_BYTES__+1)
    if len(raw)>__OUTPUT_BYTES__ or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise ValueError('invalid bounded command envelope')
    spec=json.loads(raw)
    argv=spec['argv']
    if not isinstance(argv,list) or not argv or argv[0] not in {'docker','python3','bash'}:
        raise ValueError('unbound host command')
    result=subprocess.run(argv,input=spec['stdin'],text=True,capture_output=True,timeout=spec['timeout'])
    if result.returncode or len(result.stdout.encode())>__OUTPUT_BYTES__:
        raise ValueError('host command did not complete')
    raw=result.stdout.encode()
    for offset in range(0,len(raw),24*1024):
        emit({'type':'stdout','offset':offset,'data':base64.b64encode(raw[offset:offset+24*1024]).decode()})
    emit({'type':'complete','size':len(raw)})
except BaseException:
    emit({'type':'failed'})
""".replace("__OUTPUT_BYTES__", str(CUTOVER_COMMAND_BYTES))


class CutoverHostTransport:
    def __init__(
        self,
        *,
        host: str,
        token: str,
        server_id: str,
        guard: Callable[[], None],
        timeout_seconds: int = 600,
    ) -> None:
        self.server_id = require_registered_remote_server(server_id)
        if not 1 <= timeout_seconds <= 7200:
            raise ValueError("cutover host requires a bounded execution deadline")
        self.host, self.token = host, token
        self.guard, self.timeout_seconds = guard, timeout_seconds

    def read(self, argv: tuple[str, ...], stdin: str | None) -> str:
        return self._command(argv, stdin, guarded=False)

    def __call__(self, argv: tuple[str, ...], stdin: str | None) -> str:
        try:
            return self._command(argv, stdin, guarded=True)
        except NativeSourceUnavailable as error:
            raise RuntimeError(
                "Original cutover command requires reconciliation; its effect is uncertain."
            ) from error

    def _command(self, argv: tuple[str, ...], stdin: str | None, *, guarded: bool) -> str:
        command: dict[str, object] = {
            "argv": argv,
            "stdin": stdin,
            "timeout": self.timeout_seconds,
        }
        raw = json.dumps(command, sort_keys=True).encode()
        if len(raw) > CUTOVER_COMMAND_BYTES:
            raise NativeSourceUnavailable("Cutover command exceeds its buffer bound.")
        payload: dict[str, object] = {"command": base64.b64encode(zlib.compress(raw)).decode()}
        if len(base64.b64encode(json.dumps(payload, sort_keys=True).encode())) > 120 * 1024:
            raise NativeSourceUnavailable("Cutover command exceeds the admitted transport bound.")
        output = bytearray()
        completed = False

        def receive(frame: dict[str, object]) -> None:
            nonlocal completed
            if completed:
                raise NativeSourceUnavailable("Cutover command returned duplicate completion.")
            if frame.get("type") == "stdout":
                if frame.get("offset") != len(output) or not isinstance(frame.get("data"), str):
                    raise NativeSourceUnavailable("Cutover command output is incomplete.")
                output.extend(base64.b64decode(str(frame["data"]), validate=True))
                if len(output) > CUTOVER_COMMAND_BYTES:
                    raise NativeSourceUnavailable("Cutover command output exceeds its bound.")
            elif frame == {"type": "complete", "size": len(output)}:
                completed = True
            else:
                raise NativeSourceUnavailable("Cutover command has no exact completion witness.")

        def dispatch(send: Callable[[], None]) -> None:
            self.guard()
            send()

        _read_remote(
            host=self.host,
            token=self.token,
            server_id=self.server_id,
            program=CUTOVER_COMMAND_PROGRAM,
            payload=payload,
            receive=receive,
            timeout_seconds=self.timeout_seconds + 30,
            dispatch_guard=dispatch if guarded else None,
        )
        if not completed:
            raise NativeSourceUnavailable("Cutover command completion is unavailable.")
        return output.decode()
