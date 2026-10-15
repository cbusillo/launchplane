"""Bounded artifact reads through Dokploy's existing remote terminal transport.

The only programs accepted by the public helpers are fixed source inspection /
export programs. Provider credentials stay on the connection to Dokploy; its
existing SSH identity stays inside Dokploy. Raw terminal output is never logged.
"""

import base64
from collections.abc import Callable, Iterator
import json
import logging
import re
import shlex
import time
from typing import Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit
import uuid
import click
from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import connect


class NativeSourceAuthorizationDenied(DurableOperationAuthorizationDeniedError):
    """The existing source transport explicitly refused this identity."""

    def __init__(self, message: str, *, provider_effect_terminal: bool = False):
        self.provider_effect_terminal = provider_effect_terminal
        super().__init__(code="authorization_denied", message=message)


class NativeSourceUnavailable(click.ClickException, ValueError):
    """The bounded source read did not produce complete evidence."""


class NativeUploadRejected(NativeSourceUnavailable):
    """A provider validation refusal or explicit remote exit, never a timeout."""

    provider_effect_terminal = True

    def __init__(self, *, retryable: bool = False):
        self.retryable = retryable
        super().__init__("The provider rejected the native upload with terminal evidence.")


class ReadConnection(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...


def require_registered_remote_server(server_id: object) -> str:
    if (
        not isinstance(server_id, str)
        or server_id == "local"
        or not re.fullmatch(r"[a-zA-Z0-9_-]+", server_id)
    ):
        raise NativeSourceUnavailable("Native testing copy requires a registered remote server.")
    return server_id


def _terminal_uri(host: str, server_id: str) -> str:
    require_registered_remote_server(server_id)
    parsed = urlsplit(host)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or server_id == "local"
        or not re.fullmatch(r"[a-zA-Z0-9_-]+", server_id)
    ):
        raise NativeSourceUnavailable("A registered remote provider source is required.")
    return urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            "/terminal",
            urlencode({"serverId": server_id}),
            "",
        )
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NativeSourceUnavailable("The source read exceeded its bounded deadline.")
    return remaining


def _lines(connection: ReadConnection, deadline: float) -> Iterator[str]:
    pending = ""
    while True:
        message = connection.recv(timeout=_remaining(deadline))
        pending += (
            message.decode("utf-8", errors="strict") if isinstance(message, bytes) else message
        )
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            if len(line) > 128 * 1024:
                raise NativeSourceUnavailable("The source frame exceeded its bound.")
            yield line.rstrip("\r")
        if len(pending) > 128 * 1024:
            raise NativeSourceUnavailable("The source frame exceeded its bound.")


def _read_program(
    connection: ReadConnection,
    *,
    program: str,
    payload: dict[str, object],
    timeout_seconds: float,
    receive: Callable[[dict[str, object]], None],
) -> None:
    deadline = time.monotonic() + timeout_seconds
    nonce = uuid.uuid4().hex
    ready = "LP_SOURCE_READY_" + nonce
    prefix = "LP_SOURCE_FRAME_" + nonce + ":"
    # The remote endpoint first emits connection progress, then a terminal clear
    # on SSH readiness. Wait for that state before a harmless readiness handshake.
    initial = ""
    while "\x1bc" not in initial:
        message = connection.recv(timeout=_remaining(deadline))
        initial += message.decode() if isinstance(message, bytes) else message
        if "\x1bc" not in initial and any(
            line.startswith("Authentication failed:") for line in initial.splitlines()
        ):
            raise NativeSourceAuthorizationDenied("The registered source refused authentication.")
        if len(initial) > 16384 or "SSH connection error" in initial:
            raise NativeSourceUnavailable("The registered source terminal is unavailable.")
    # Dokploy emits the clear before opening the shell and installing its input
    # listener. Actual shell output proves that listener can receive the handshake.
    prompt_text = initial.split("\x1bc", 1)[1]
    if not prompt_text:
        prompt = connection.recv(timeout=_remaining(deadline))
        prompt_text = prompt.decode() if isinstance(prompt, bytes) else prompt
    loader = (
        "import base64,json,sys,termios; a=termios.tcgetattr(0);"
        "a[3]&=~(termios.ECHO|termios.ICANON|termios.IEXTEN);"
        "a[0]&=~(termios.ICRNL|termios.IXON);a[6][termios.VMIN]=1;a[6][termios.VTIME]=0;"
        "termios.tcsetattr(0,termios.TCSANOW,a);print('\\n'+sys.argv[1]+'\\n',flush=True);"
        "line=sys.stdin.readline(262145);"
        "assert line.endswith('\\n') and len(line)<=262144;v=json.loads(line);"
        "sys.argv=[sys.argv[0],v['payload'],sys.argv[2]];"
        "exec(base64.b64decode(v['program'],validate=True))"
    )
    # Replace the interactive shell before sending any private payload. The
    # loader itself disables canonical input/echo and then proves readiness;
    # shell readline can otherwise reset tty settings and truncate long input.
    connection.send(
        "HISTFILE=/dev/null; export HISTFILE; unset PROMPT_COMMAND; "
        "set +o history; exec " + shlex.join(["python3", "-u", "-c", loader, ready, prefix]) + "\n"
    )
    lines = _lines(connection, deadline)
    for line in lines:
        if line == ready:
            break
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    encoded_program = base64.b64encode(program.encode()).decode()
    if len(encoded) > 120 * 1024 or len(encoded_program) > 120 * 1024:
        raise NativeSourceUnavailable("The fixed remote read exceeds the transport argument bound.")
    connection.send(json.dumps({"program": encoded_program, "payload": encoded}) + "\n")
    complete = False
    for line in lines:
        if not line.startswith(prefix):
            continue
        value = json.loads(line[len(prefix) :])
        if not isinstance(value, dict):
            raise NativeSourceUnavailable("The source returned an invalid frame.")
        if value.get("type") == "denied":
            raise NativeSourceAuthorizationDenied("The registered source refused the read.")
        if value.get("type") == "failed":
            raise NativeSourceUnavailable("The registered source read did not pass.")
        receive(value)
        if value.get("type") == "complete":
            complete = True
            break
    if not complete:
        raise NativeSourceUnavailable("The source read has no completion witness.")


def read_native_source(
    *,
    host: str,
    token: str,
    server_id: str,
    source: dict[str, object],
    mode: str,
    receive: Callable[[dict[str, object]], None],
    timeout_seconds: float = 420,
) -> None:
    if mode not in {"probe", "capture"}:
        raise NativeSourceUnavailable("Unsupported source read mode.")
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=NATIVE_ODOO_SOURCE_READ_PROGRAM,
        payload={"source": source, "mode": mode},
        receive=receive,
        timeout_seconds=timeout_seconds,
    )


def inspect_native_testing_receiver(
    *,
    host: str,
    token: str,
    server_id: str,
    spec: dict[str, object],
    receive: Callable[[dict[str, object]], None],
) -> None:
    from control_plane.odoo_testing_restore import FRESH_TESTING_RESTORE_HOST_PROGRAM

    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=FRESH_TESTING_RESTORE_HOST_PROGRAM,
        payload={**spec, "inspection_only": True},
        receive=receive,
        timeout_seconds=120,
    )


def _read_remote(
    *,
    host: str,
    token: str,
    server_id: str,
    program: str,
    payload: dict[str, object],
    receive: Callable[[dict[str, object]], None],
    timeout_seconds: float,
) -> None:
    logger = logging.Logger("launchplane-private-source", level=logging.CRITICAL + 1)
    logger.disabled = True
    logger.propagate = False
    try:
        # The sync client performs one handshake. It does not reconnect or follow
        # redirects; no proxy, cookie or alternate credential is selected.
        with connect(
            _terminal_uri(host, server_id),
            additional_headers={"x-api-key": token},
            proxy=None,
            compression=None,
            logger=logger,
            max_size=1024 * 1024,
            open_timeout=20,
            close_timeout=5,
            legacy=True,
        ) as connection:
            _read_program(
                connection,
                program=program,
                payload=payload,
                timeout_seconds=timeout_seconds,
                receive=receive,
            )
    except InvalidStatus as error:
        if error.response.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied("The provider refused the source read.") from None
        raise NativeSourceUnavailable("The provider source handshake did not pass.") from None
    except (
        ConnectionClosed,
        InvalidHandshake,
        OSError,
        TimeoutError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        raise NativeSourceUnavailable("The provider source stream is incomplete.") from None


NATIVE_ODOO_SOURCE_READ_PROGRAM = r"""
import base64, hashlib, json, os, re, subprocess, sys, threading

spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
source=spec['source']
prefix=sys.argv[2]

def emit(value):
    print(prefix+json.dumps(value,sort_keys=True),flush=True)

class ReadDenied(Exception):
    pass

def denied(error):
    text=error.lower()
    return any(word in text for word in (b'password authentication failed',b'no password supplied'))

def checked(arguments):
    result=subprocess.run(arguments,capture_output=True,check=False)
    if result.returncode:
        if denied(result.stderr):
            raise ReadDenied()
        raise RuntimeError('Source read did not pass')
    return result.stdout

def inspect(identifier):
    if not re.fullmatch(r'[a-f0-9]{64}',identifier):
        raise RuntimeError('Invalid source container')
    values=json.loads(checked(['docker','inspect',identifier]))
    if len(values)!=1:
        raise RuntimeError('Ambiguous source container')
    return values[0]

def validate():
    for role in ('database','web'):
        row=inspect(source[role+'_container'])
        labels=row['Config'].get('Labels') or {}
        if (not row['State']['Running'] or row['Image']!=source[role+'_image']
            or labels.get('com.docker.compose.project')!=source['compose_project']
            or labels.get('com.docker.compose.service')!=role):
            raise RuntimeError('Source identity changed')
        if role=='web':
            environment=dict(v.split('=',1) for v in row['Config'].get('Env',[]) if '=' in v)
            if environment.get('ODOO_DB_NAME')!=source['database_name'] or environment.get('PLATFORM_INSTANCE')!='prod' or environment.get('ODOO_DB_HOST')!='database':
                raise RuntimeError('Source no longer names production')
            identity=json.loads(environment.get('__RUNTIME_IDENTITY_ENV_KEY__','null'))
            if (not isinstance(identity,dict)
                or any(identity.get(key)!=source[key] for key in ('product','context','instance'))
                or identity.get('environment_kind')!='stable'):
                raise RuntimeError('Source runtime does not belong to the registered production lane')
    if (source['instance']!='prod'
        or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}',source['database_name'])
        or not re.fullmatch(r'/[a-zA-Z0-9_./-]+',source['filestore_path'])
        or '..' in source['filestore_path'].split('/')):
        raise RuntimeError('Invalid source coordinates')

def stream(part, arguments):
    digest=hashlib.sha256()
    size=0
    # Drain stderr without logging or returning it, retaining only a bounded
    # private prefix for an explicit authorization-refusal classification.
    with subprocess.Popen(arguments,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as process:
        errors=bytearray()
        def drain():
            while True:
                chunk=process.stderr.read(4096)
                if not chunk:
                    return
                if len(errors)<16384:
                    errors.extend(chunk[:16384-len(errors)])
        reader=threading.Thread(target=drain,daemon=True)
        reader.start()
        try:
            while True:
                chunk=process.stdout.read(48*1024)
                if not chunk:
                    break
                size+=len(chunk)
                if size>128*1024*1024:
                    raise RuntimeError('Source artifact exceeds the bounded transport')
                digest.update(chunk)
                emit({'type':'data','part':part,'data':base64.b64encode(chunk).decode()})
            status=process.wait()
            reader.join()
            if status and denied(bytes(errors)):
                raise ReadDenied()
            if status!=0 or not size:
                raise RuntimeError('Source export failed')
        except BaseException:
            process.kill()
            process.wait()
            raise
    return {'sha256':digest.hexdigest(),'size':size}

try:
    validate()
    # Both commands stay inside existing source containers. They create no dump,
    # archive, pin, schedule, credential or other persistent production file.
    database=['docker','exec',source['database_container'],'sh','-c',
        'exec timeout --signal=TERM --kill-after=5s 180s pg_dump --host=/var/run/postgresql --username="$POSTGRES_USER" --lock-wait-timeout=10s --format=custom --no-owner -- "$1"',
        '_',source['database_name']]
    filestore=['docker','exec',source['web_container'],'timeout','--signal=TERM','--kill-after=5s','180s','tar','--create','--gzip','--file=-',
        '--directory',source['filestore_path'],'.']
    if spec['mode']=='probe':
        checks=[['docker','exec',source['database_container'],'sh','-c',
            'command -v pg_dump >/dev/null && exec timeout 30s psql -X -qAt --host=/var/run/postgresql --username="$POSTGRES_USER" --dbname="$1" --command="SELECT 1"',
            '_',source['database_name']],
            ['docker','exec',source['web_container'],'timeout','30s','test','-r',source['filestore_path']]]
        for arguments in checks:
            checked(arguments)
        emit({'type':'complete','probe':True})
    elif spec['mode']=='capture':
        database_receipt=stream('database',database)
        filestore_receipt=stream('filestore',filestore)
        validate()
        emit({'type':'complete','database':database_receipt,'filestore':filestore_receipt})
    else:
        raise RuntimeError('Invalid source read mode')
except ReadDenied:
    emit({'type':'denied'})
    raise SystemExit(1)
except BaseException:
    emit({'type':'failed'})
    raise SystemExit(1)
""".replace("__RUNTIME_IDENTITY_ENV_KEY__", RUNTIME_IDENTITY_ENV_KEY)
