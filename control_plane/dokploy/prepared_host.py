import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.responses import PlainTextResponse
from starlette.types import Message, Receive, Scope, Send

from control_plane.contracts.prepared_public_site import PreparedPublicSite
from control_plane.prepared_public_site import prepared_public_app, verify_snapshot


ACTIVE_PAUSE_FILE = "active.json"


def bundle_name(pause_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", pause_id) is None:
        raise ValueError("prepared serving requires its original pause identity")
    return hashlib.sha256(pause_id.encode()).hexdigest() + ".json"


def prepared_bundle_text(site: PreparedPublicSite, pause_id: str) -> str:
    return json.dumps(
        {"pause_id": pause_id, "site": site.model_dump(mode="json", exclude_defaults=True)}
    )


def parse_prepared_bundle(text: str, pause_id: str) -> PreparedPublicSite:
    value = json.loads(text)
    if value["pause_id"] != pause_id:
        raise ValueError("prepared bundle belongs to another pause")
    site = PreparedPublicSite.model_validate(value["site"])
    verify_snapshot(site)
    return site


def parse_active_pause(text: str) -> str:
    value = json.loads(text)
    if not isinstance(value, str) or not value:
        raise ValueError("prepared publication has no original pause identity")
    return value


class PreparedSiteDirectory:
    def __init__(self, directory: Path) -> None:
        if (
            not directory.is_absolute()
            or directory.resolve() != directory
            or not directory.is_dir()
        ):
            raise ValueError("prepared serving requires an existing canonical private directory")
        self.directory = directory

    def path(self, pause_id: str) -> Path:
        return self.directory / bundle_name(pause_id)

    def load(self, pause_id: str) -> PreparedPublicSite:
        path = self.path(pause_id)
        if path.is_symlink():
            raise ValueError("prepared bundle cannot be a symlink")
        return parse_prepared_bundle(path.read_text(), pause_id)

    def active_pause(self) -> str:
        path = self.directory / ACTIVE_PAUSE_FILE
        if path.is_symlink():
            raise ValueError("prepared publication cannot be a symlink")
        return parse_active_pause(path.read_text())


class PreparedDirectoryServing:
    def __init__(self, directory: Path) -> None:
        self.directory = PreparedSiteDirectory(directory)
        self.signature: tuple[str, int, int, int] | None = None
        self.app: FastAPI | None = None
        self.host = ""
        self.pause_id = ""

    def refresh(self) -> None:
        try:
            pause_id = self.directory.active_pause()
            metadata = self.directory.path(pause_id).stat()
            signature = (pause_id, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size)
            if signature != self.signature:
                site = self.directory.load(pause_id)
                self.app = prepared_public_app(site)
                self.host = urlsplit(site.plan.origin).hostname or ""
                self.pause_id = pause_id
                self.signature = signature
        except (OSError, ValueError, KeyError, TypeError):
            self.signature, self.app = None, None
            self.host = ""
            self.pause_id = ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        self.refresh()
        if self.app is None:
            await PlainTextResponse("Prepared public copy is unavailable.", status_code=503)(
                scope, receive, send
            )
            return
        pause_id = self.pause_id

        async def bound_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        *message["headers"],
                        (b"x-launchplane-pause-id", pause_id.encode()),
                    ],
                }
            await send(message)

        await self.app(scope, receive, bound_response)


def prepared_directory_app(directory: Path) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/", PreparedDirectoryServing(directory))
    return app


class PreparedCatalogServing:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.sites: dict[Path, PreparedDirectoryServing] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        try:
            hosts = [
                value.decode("ascii") for key, value in scope.get("headers", []) if key == b"host"
            ]
            host = urlsplit("http://" + hosts[0]) if len(hosts) == 1 else None
        except (UnicodeError, ValueError):
            host = None
        candidates = []
        if (
            host is not None
            and host.hostname
            and not host.username
            and not host.password
            and not host.path
            and not host.query
            and not host.fragment
            and self.directory.is_dir()
            and self.directory.resolve() == self.directory
        ):
            for path in self.directory.iterdir():
                if not path.is_dir() or path.is_symlink():
                    continue
                site = self.sites.setdefault(path, PreparedDirectoryServing(path))
                site.refresh()
                if site.host == host.hostname:
                    candidates.append(site)
        if len(candidates) != 1:
            await PlainTextResponse("Prepared public copy is unavailable.", status_code=503)(
                scope, receive, send
            )
            return
        await candidates[0](scope, receive, send)


def prepared_service_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    root = Path(os.environ["LAUNCHPLANE_PREPARED_PUBLIC_ROOT"])
    if not root.is_absolute():
        raise ValueError("prepared serving requires its absolute public-volume mount")

    @app.get("/_launchplane_prepared_health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/", PreparedCatalogServing(root))
    return app
