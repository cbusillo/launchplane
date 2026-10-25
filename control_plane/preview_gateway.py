import hashlib


PREVIEW_GATEWAY_SERVICE = "preview-router"
PREVIEW_IDLE_SECONDS = 15 * 60
PREVIEW_GATEWAY_SOCKET = "/tmp/launchplane-preview-control.sock"
PREVIEW_BACKEND_ALIAS_PREFIX = "lp-preview-"


def preview_backend_alias(identity: str) -> str:
    return PREVIEW_BACKEND_ALIAS_PREFIX + hashlib.sha256(identity.encode()).hexdigest()[:32]


PREVIEW_GATEWAY_PROGRAM = (
    f"PREVIEW_IDLE_SECONDS = {PREVIEW_IDLE_SECONDS!r}\n"
    f"PREVIEW_GATEWAY_SOCKET = {PREVIEW_GATEWAY_SOCKET!r}\n"
    f"PREVIEW_BACKEND_ALIAS_PREFIX = {PREVIEW_BACKEND_ALIAS_PREFIX!r}\n"
    + r"""
import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid


class PreviewGateway:
    def __init__(self, identity, state_path, *, idle_seconds, clock=time.monotonic):
        self.identity = hashlib.sha256(identity.encode()).hexdigest()
        self.state_path = Path(state_path)
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.boot_id = uuid.uuid4().hex
        self.sequence = 0
        self.last_visit = clock()
        self.active = 0
        self.requested = False
        self.mode = 'awake'
        self.sleep_record_id = ''
        self.wake_record_id = ''
        self.awake = asyncio.Event()
        if self.state_path.exists() or self.state_path.is_symlink():
            if self.state_path.is_symlink() or self.state_path.stat().st_size > 4096:
                raise ValueError('Preview gateway state custody differs')
            previous = json.loads(self.state_path.read_text())
            if previous.get('identity') != self.identity or previous.get('mode') not in {'awake', 'asleep'}:
                raise ValueError('Preview gateway state binding differs')
            self.mode = previous['mode']
            self.sleep_record_id = previous['sleep_record_id']
            self.wake_record_id = previous['wake_record_id']
        if self.mode == 'awake':
            self.awake.set()

    def status(self):
        return {'identity': self.identity, 'boot_id': self.boot_id, 'sequence': self.sequence,
            'idle_seconds': max(0, self.clock() - self.last_visit), 'active': self.active,
            'requested': self.requested, 'mode': self.mode,
            'sleep_record_id': self.sleep_record_id, 'wake_record_id': self.wake_record_id}

    def persist(self, mode, sleep_record_id, wake_record_id):
        payload = {'identity': self.identity, 'mode': mode,
            'sleep_record_id': sleep_record_id, 'wake_record_id': wake_record_id}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.state_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            descriptor = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.mode, self.sleep_record_id, self.wake_record_id = mode, sleep_record_id, wake_record_id

    def control(self, request):
        action = request.get('action')
        accepted = action == 'status'
        if action in {'sleep', 'waking', 'awake'}:
            record_id = request.get('record_id', '')
            if not isinstance(record_id, str) or not record_id.startswith('mutation-reservation-') or len(record_id) > 128:
                return {'accepted': False, **self.status()}
            if action == 'sleep':
                accepted = self.mode == 'asleep' and self.sleep_record_id == record_id
                if (self.mode == 'awake' and self.active == 0 and not self.requested
                    and self.clock() - self.last_visit >= self.idle_seconds
                    and request.get('boot_id') == self.boot_id and request.get('sequence') == self.sequence):
                    self.persist('asleep', record_id, '')
                    self.awake.clear()
                    accepted = True
            elif (request.get('sleep_record_id') == self.sleep_record_id and self.sleep_record_id
                and (action == 'waking' and self.mode == 'asleep' and self.wake_record_id in {'', record_id}
                     or action == 'awake' and self.wake_record_id == record_id)):
                if action == 'waking':
                    self.persist('asleep', self.sleep_record_id, record_id)
                    return {'accepted': True, **self.status()}
                self.persist('awake', self.sleep_record_id, record_id)
                self.last_visit = self.clock()
                self.requested = False
                self.awake.set()
                accepted = True
        return {'accepted': accepted, **self.status()}

    async def control_connection(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if len(line) > 4096:
                return
            result = self.control(json.loads(line))
            writer.write(json.dumps(result).encode() + b'\n')
            await writer.drain()
        except (OSError, ValueError, TypeError, AttributeError, asyncio.TimeoutError):
            return
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    def visit(self):
        self.last_visit = self.clock()
        self.sequence += 1

    async def unavailable(self, writer, *, refresh):
        body = (('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            + ('<meta http-equiv="refresh" content="5">' if refresh else '')
            + '<title>Opening preview</title><style>body{font:18px system-ui;max-width:34rem;'
              'margin:15vh auto;padding:2rem;color:#203047;background:#f6f8fc}h1{font-size:1.6rem}'
              '</style></head><body><h1>Opening your preview</h1><p>The preview will open when it '
              'has room to start. This page checks again automatically.</p></body></html>')
            if refresh else 'The preview is starting. Please try this request again shortly.').encode()
        writer.write(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n'
            b'Cache-Control: no-store\r\nRetry-After: 5\r\nContent-Type: text/html; charset=utf-8\r\n'
            + ('Content-Length: ' + str(len(body)) + '\r\n\r\n').encode() + body)
        await writer.drain()

    async def connection(self, reader, writer, backend_host, backend_port):
        upstream_writer = None
        self.active += 1
        self.visit()
        try:
            first = await asyncio.wait_for(reader.readline(), timeout=10)
            if not first:
                return
            refresh = first.startswith(b'GET ')
            if self.mode == 'asleep':
                self.requested = True
                if refresh:
                    await self.unavailable(writer, refresh=True)
                    return
                try:
                    await asyncio.wait_for(self.awake.wait(), timeout=90)
                except asyncio.TimeoutError:
                    await self.unavailable(writer, refresh=False)
                    return
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(backend_host, backend_port), timeout=2)
            except (OSError, asyncio.TimeoutError):
                self.requested = self.mode == 'asleep'
                await self.unavailable(writer, refresh=refresh)
                return
            self.requested = False
            upstream_writer.write(first)
            await upstream_writer.drain()

            async def forward(source, destination):
                while chunk := await source.read(65536):
                    self.visit()
                    destination.write(chunk)
                    await destination.drain()
                if destination.can_write_eof():
                    destination.write_eof()

            async with asyncio.TaskGroup() as group:
                group.create_task(forward(reader, upstream_writer))
                group.create_task(forward(upstream_reader, writer))
        except (OSError, ValueError, asyncio.TimeoutError, ExceptionGroup):
            return
        finally:
            self.active -= 1
            for connection in (upstream_writer, writer):
                if connection is not None:
                    connection.close()
                    with suppress(OSError):
                        await connection.wait_closed()


async def run_gateway():
    port = int(sys.argv[1])
    if not 0 < port < 65536:
        raise ValueError('Invalid preview port')
    identity = os.environ['LAUNCHPLANE_RUNTIME_IDENTITY_JSON']
    gateway = PreviewGateway(identity, '/tmp/launchplane-preview-state.json', idle_seconds=PREVIEW_IDLE_SECONDS)
    socket_path = Path(PREVIEW_GATEWAY_SOCKET)
    if socket_path.is_symlink():
        raise ValueError('Preview control socket is aliased')
    socket_path.unlink(missing_ok=True)
    os.umask(0o077)
    control = await asyncio.start_unix_server(gateway.control_connection, path=socket_path, limit=4096)
    server = await asyncio.start_server(
        lambda reader, writer: gateway.connection(reader, writer, PREVIEW_BACKEND_ALIAS_PREFIX + gateway.identity[:32], port), '0.0.0.0', port)
    async with control, server:
        await asyncio.gather(control.serve_forever(), server.serve_forever())


if __name__ == '__main__':
    asyncio.run(run_gateway())
"""
)

PREVIEW_GATEWAY_INSPECTION_LIBRARY = (
    f"PREVIEW_BACKEND_ALIAS_PREFIX = {PREVIEW_BACKEND_ALIAS_PREFIX!r}\n"
    + r"""
import hashlib

def inspect_preview_gateway(container, web):
    config, host = container['Config'], container['HostConfig']
    if (container.get('Mounts') or host.get('Privileged') or host.get('Binds')
        or host.get('Devices') or host.get('CapAdd') or host.get('ExtraHosts')
        or host.get('NetworkMode') == 'host' or host.get('PidMode') not in (None, '', 'private')
        or any(host.get('PortBindings', {}).values()) or host.get('CapDrop') != ['ALL']
        or 'no-new-privileges:true' not in host.get('SecurityOpt', [])
        or config.get('Image') != web['Config'].get('Image')
        or config.get('Entrypoint') != ['/venv/bin/python3', '-I', '-S', '-c']):
        raise RuntimeError('Preview gateway ownership differs')
    args = config.get('Cmd')
    if not isinstance(args, list) or len(args) != 2 or not isinstance(args[0], str) or not 0 < len(args[0]) <= 65536 or not args[1].isdigit() or not 0 < int(args[1]) < 65536:
        raise RuntimeError('Preview gateway program differs')
    web_env = dict(item.split('=', 1) for item in web['Config'].get('Env', []) if '=' in item)
    identity = web_env.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON')
    base = json.loads(docker('image', 'inspect', container['Image']))[0]['Config'].get('Env', [])
    expected = dict(item.split('=', 1) for item in base if '=' in item)
    expected['LAUNCHPLANE_RUNTIME_IDENTITY_JSON'] = identity
    environment = dict(item.split('=', 1) for item in config.get('Env', []) if '=' in item)
    if not identity or environment != expected:
        raise RuntimeError('Preview gateway environment differs')
    if set(container['NetworkSettings']['Networks']) != set(web['NetworkSettings']['Networks']):
        raise RuntimeError('Preview gateway networks differ')
    private = set(web['NetworkSettings']['Networks']) - {'dokploy-network'}
    alias = PREVIEW_BACKEND_ALIAS_PREFIX + hashlib.sha256(identity.encode()).hexdigest()[:32]
    if len(private) != 1 or alias not in (web['NetworkSettings']['Networks'][next(iter(private))].get('Aliases') or []):
        raise RuntimeError('Preview gateway backend differs')
"""
)
