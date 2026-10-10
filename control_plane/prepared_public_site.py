"""Prepare and serve an immutable public copy without an Odoo/DB dependency."""

import base64
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from fastapi import FastAPI, Request, Response

from control_plane.contracts.prepared_public_site import (
    CapturedPublicResponse,
    PreparedPublicSite,
    PreparedResource,
    PublicSitePlan,
    validate_public_path,
)
from control_plane.prepared_public_html import PassivePublicHTML, css_references, local_reference

CSP = (
    "default-src 'none'; script-src 'none'; connect-src 'none'; form-action 'none'; "
    "frame-src 'none'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; "
    "img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; media-src 'self'"
)
PASSIVE_TYPES = frozenset(
    {
        "text/css",
        "text/javascript",
        "application/javascript",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "font/woff",
        "font/woff2",
        "application/font-woff",
        "font/ttf",
        "font/otf",
        "video/mp4",
        "audio/mpeg",
        "text/vtt",
    }
)


class PublicCapture(Protocol):
    def capture(self, path: str) -> CapturedPublicResponse:
        """Capture without Cookie/Authorization, from the plan's verified serving runtime."""
        ...


def snapshot_digest(site: PreparedPublicSite) -> str:
    payload = site.model_dump_json(exclude={"content_digest"}).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def verify_snapshot(site: PreparedPublicSite) -> None:
    if site.content_digest != snapshot_digest(site):
        raise ValueError("prepared copy content or binding changed")
    paths = {resource.path for resource in site.resources}
    if len(paths) != len(site.resources) or not set(site.plan.public_routes) <= paths:
        raise ValueError("prepared public route coverage is incomplete")
    if not set(site.plan.retained_assets) <= paths:
        raise ValueError("prepared retained asset coverage is incomplete")
    for resource in site.resources:
        validate_public_path(resource.path)
        if site.plan.excluded(resource.path):
            raise ValueError("prepared copy contains an excluded path")
        base64.b64decode(resource.body_base64, validate=True)


def prepare_public_site(
    plan: PublicSitePlan,
    capture: PublicCapture,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PreparedPublicSite:
    """Fail the whole preparation if any declared/discovered reference is missing."""
    pending = set(plan.public_routes) | set(plan.retained_assets)
    resources: dict[str, PreparedResource] = {}
    total = 0
    while pending:
        if len(resources) + len(pending) > plan.max_resources:
            raise ValueError("public copy exceeds resource budget")
        path = min(pending)
        pending.remove(path)
        if plan.excluded(path):
            raise ValueError("asset/page reference reaches an excluded path")
        response = capture.capture(path)
        if response.binding != plan.binding:
            raise ValueError("capture runtime does not match artifact/database/release binding")
        headers = {key.lower(): value for key, value in response.headers.items()}
        if "private" in headers.get("cache-control", "").lower() or any(
            item.strip().lower() in {"cookie", "authorization", "*"}
            for item in headers.get("vary", "").split(",")
        ):
            raise ValueError("personalized response cannot enter public copy")
        body = response.body
        if len(body) > plan.max_resource_bytes:
            raise ValueError("public resource exceeds byte budget")
        content_type = response.content_type.split(";")[0].strip().lower()
        kept_headers: list[tuple[str, str]] = []
        references: set[str] = set()
        if response.status in {301, 302, 307, 308}:
            location = local_reference(plan, path, headers.get("location", ""))
            if not location or location not in plan.public_routes:
                raise ValueError("redirect must target a declared supported public route")
            kept_headers.append(("location", location))
            references.add(location)
            body = b""
        elif response.status != 200:
            raise ValueError(f"public coverage failed at {path}: status {response.status}")
        elif path in plan.public_routes:
            if content_type != "text/html":
                raise ValueError("public page must be HTML or a supported redirect")
            parser = PassivePublicHTML(plan, path)
            body = parser.render(body.decode("utf-8")).encode()
            if not parser.links <= set(plan.public_routes):
                raise ValueError(
                    f"unconfigured linked public routes: {sorted(parser.links - set(plan.public_routes))}"
                )
            references.update(parser.assets)
        elif content_type not in PASSIVE_TYPES:
            raise ValueError(f"unsupported public asset content type: {content_type}")
        elif content_type == "text/css":
            references.update(css_references(plan, path, body.decode("utf-8")))
        if "content-language" in headers:
            kept_headers.append(("content-language", headers["content-language"]))
        # Never copy Set-Cookie, CSRF/session headers, CSP, tracking or upstream cache headers.
        resources[path] = PreparedResource(
            path=path,
            status=response.status,
            content_type=content_type,
            body_base64=base64.b64encode(body).decode(),
            headers=tuple(kept_headers),
        )
        total += len(body)
        if total > plan.max_total_bytes:
            raise ValueError("public copy exceeds total byte budget")
        pending.update(references - resources.keys())
    # A redirect cycle can return only 3xx forever; it is not browsing availability.
    for path in plan.public_routes:
        visited = set()
        cursor = path
        while resources[cursor].status != 200:
            if cursor in visited:
                raise ValueError("public redirect cycle")
            visited.add(cursor)
            cursor = dict(resources[cursor].headers)["location"]
    site = PreparedPublicSite(
        plan=plan,
        prepared_at=clock(),
        resources=tuple(resources[p] for p in sorted(resources)),
        content_digest="",
    )
    site = site.model_copy(update={"content_digest": snapshot_digest(site)})
    verify_snapshot(site)
    return site


def prepared_public_app(site: PreparedPublicSite) -> FastAPI:
    """A standalone serving surface: no proxy, capture adapter, cookie or DB access."""
    verify_snapshot(site)
    resources = {item.path: item for item in site.resources}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def public_copy(request: Request, call_next: object) -> Response:
        del call_next
        headers = {
            "Content-Security-Policy": CSP,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Launchplane-Served-Mode": "prepared-public",
            "X-Launchplane-Reduced-Service": "writers-paused",
            "X-Launchplane-Public-Copy": site.content_digest,
        }
        if request.method not in {"GET", "HEAD"}:
            return Response("Submissions temporarily paused.", status_code=423, headers=headers)
        # Never serve authenticated/personalized variants from public content, even anonymously.
        if "authorization" in request.headers or "cookie" in request.headers:
            return Response(
                "Signed-in activity temporarily paused.", status_code=423, headers=headers
            )
        path = request.url.path + ("?" + request.url.query if request.url.query else "")
        if site.plan.excluded(path):
            return Response(
                "Signed-in/editor activity temporarily paused.", status_code=423, headers=headers
            )
        resource = resources.get(path)
        if resource is None:
            return Response(
                "Route unavailable in prepared public copy.", status_code=404, headers=headers
            )
        headers.update(dict(resource.headers))
        return Response(
            b"" if request.method == "HEAD" else base64.b64decode(resource.body_base64),
            status_code=resource.status,
            media_type=resource.content_type,
            headers=headers,
        )

    return app
