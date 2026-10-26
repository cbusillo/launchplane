import asyncio
import json
import os
from pathlib import Path
import pty
import subprocess
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect


app = FastAPI()
root = Path(os.environ["CUTOVER_FIXTURE_ROOT"])


def targets() -> dict[str, Any]:
    value: dict[str, Any] = json.loads((root / "targets.json").read_text())
    return value


def write_targets(value: dict[str, Any]) -> None:
    (root / "targets.json").write_text(json.dumps(value))
    for item in value.values():
        directory = Path(item["directory"])
        directory.mkdir(exist_ok=True)
        (directory / ".env").write_text(item["env"])
        (directory / "compose.yml").write_text(item["composeFile"])


def compose(item: dict[str, Any], *arguments: str) -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            item["appName"],
            "--project-directory",
            item["directory"],
            "--file",
            item["directory"] + "/compose.yml",
            *arguments,
        ],
        text=True,
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:])


@app.get("/api/compose.one")
def one(composeId: str) -> dict[str, Any]:
    return dict(targets()[composeId])


@app.post("/api/compose.update")
async def update(request: Request) -> dict[str, str]:
    value = await request.json()
    current = targets()
    current[value["composeId"]].update(
        {key: item for key, item in value.items() if key != "composeId"}
    )
    write_targets(current)
    return {"status": "ok"}


@app.post("/api/compose.deploy")
async def deploy(request: Request) -> dict[str, str]:
    value = await request.json()
    current = targets()
    item = current[value["composeId"]]
    await asyncio.to_thread(compose, item, "up", "-d", "--pull", "never", "--no-build")
    item["deployments"].append(
        {
            "deploymentId": str(len(item["deployments"]) + 1),
            "title": value["title"],
            "status": "done",
        }
    )
    write_targets(current)
    return {"status": "ok"}


@app.get("/fixture/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/terminal")
async def terminal(socket: WebSocket) -> None:
    if (
        socket.query_params.get("serverId") != "isolated"
        or socket.headers.get("x-api-key") != "inert-fixture-token"
    ):
        await socket.close(code=1008)
        return
    await socket.accept()
    master, slave = pty.openpty()
    child = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={**os.environ, "PS1": "fixture> ", "HISTFILE": "/dev/null"},
    )
    os.close(slave)
    os.set_blocking(master, False)
    await socket.send_text("\x1bcfixture> ")

    async def output() -> None:
        while child.poll() is None:
            try:
                value = os.read(master, 65536)
            except BlockingIOError:
                await asyncio.sleep(0.005)
                continue
            except OSError:
                return
            if value:
                await socket.send_bytes(value)
        while True:
            try:
                value = os.read(master, 65536)
            except (BlockingIOError, OSError):
                return
            if not value:
                return
            await socket.send_bytes(value)

    async def input_stream() -> None:
        while True:
            value = (await socket.receive_text()).encode()
            while value:
                try:
                    written = os.write(master, value)
                    value = value[written:]
                except BlockingIOError:
                    await asyncio.sleep(0.005)

    sending = asyncio.create_task(output())
    receiving = asyncio.create_task(input_stream())
    try:
        done, pending = await asyncio.wait(
            (sending, receiving), return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            try:
                await task
            except (WebSocketDisconnect, RuntimeError):
                pass
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        os.close(master)
        if child.poll() is None:
            child.terminate()
            await asyncio.to_thread(child.wait, 5)
