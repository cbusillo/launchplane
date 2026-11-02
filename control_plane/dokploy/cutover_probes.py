import base64
from collections.abc import Callable
import hashlib
import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from playwright.sync_api import Route, sync_playwright

from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import runtime_identity_from_health_payload
from control_plane.dokploy.slot_maintenance import ContainerCommand
from control_plane.testing_browser import launch_testing_browser


HTTP_OBSERVATION_PROGRAM = r"""
import base64,json,socket,sys
from http.client import HTTPException
from urllib.error import HTTPError,URLError
from urllib.request import HTTPRedirectHandler,ProxyHandler,Request,build_opener
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None
spec=json.load(sys.stdin)
request=Request(spec['url'],headers={'Host':spec['host'],'X-Odoo-Database':spec['database']})
try:
    try: response=build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=20)
    except HTTPError as error: response=error
    with response:
        body=response.read(20_000_001)
        if len(body)>20_000_000:
            print(json.dumps({'failure':'resource_exceeds_bound'}))
        else:
            headers={key.lower():value for key,value in response.headers.items() if key.lower() not in {'set-cookie','authorization','www-authenticate'}}
            print(json.dumps({'status':response.status,'headers':headers,'body':base64.b64encode(body).decode()},sort_keys=True))
except HTTPException:
    print(json.dumps({'failure':'protocol_error'}))
except OSError as error:
    reason=error.reason if isinstance(error,URLError) else error
    kind='timeout' if isinstance(reason,TimeoutError) else 'connection_refused' if isinstance(reason,ConnectionRefusedError) else 'dns' if isinstance(reason,socket.gaierror) else 'connection_unavailable'
    print(json.dumps({'failure':kind}))
"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class PrivateHealthFailed(ValueError):
    def __init__(self, kind: str, *, status: int | None = None) -> None:
        self.kind, self.status = kind, status
        super().__init__(f"Private health failed: {kind}; status={status}.")


def readiness_fingerprint(contract: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class CutoverProbes:
    def __init__(
        self, plan: OdooCutoverPlan, execute: ContainerCommand, runner: Callable[[OdooSlot], str]
    ) -> None:
        self.plan, self.execute, self.runner = plan, execute, runner

    def private_resource(self, origin: str, path: str) -> dict[str, Any]:
        output = self.execute(
            (
                "docker",
                "exec",
                "-i",
                self.runner(self.plan.old),
                "/venv/bin/python",
                "-c",
                HTTP_OBSERVATION_PROGRAM,
            ),
            json.dumps(
                {
                    "url": origin.rstrip("/") + path,
                    "host": self.plan.host,
                    "database": self.plan.old.binding.database,
                }
            ),
        )
        value: dict[str, Any] = json.loads(output)
        if value.get("failure") in {
            "timeout",
            "connection_refused",
            "dns",
            "connection_unavailable",
            "protocol_error",
            "resource_exceeds_bound",
        }:
            raise PrivateHealthFailed(value["failure"])
        if (
            type(value.get("status")) is not int
            or not isinstance(value.get("headers"), dict)
            or not isinstance(value.get("body"), str)
        ):
            raise ValueError("private resource observation is incomplete")
        base64.b64decode(value["body"], validate=True)
        return value

    def private_health(self, slot: OdooSlot) -> HealthcheckEvidence:
        path = "/launchplane/health"
        resource = self.private_resource(slot.private_url, path)
        if resource["status"] != 200:
            raise PrivateHealthFailed("http_status", status=resource["status"])
        try:
            observed = runtime_identity_from_health_payload(
                json.loads(base64.b64decode(resource["body"]))
            )
        except (ValueError, TypeError, KeyError):
            raise PrivateHealthFailed("invalid_health") from None
        if observed is None:
            raise PrivateHealthFailed("invalid_health")
        if observed != slot.runtime_identity:
            raise PrivateHealthFailed("identity_mismatch")
        return HealthcheckEvidence(
            status="pass",
            verified=True,
            urls=(slot.private_url.rstrip("/") + path,),
            timeout_seconds=20,
            runtime_identity_status="match",
            observed_runtime_identity=observed,
        )

    def public_resource(self, origin: str, path: str) -> dict[str, Any]:
        request = Request(origin.rstrip("/") + path, headers={"Host": self.plan.host})
        try:
            response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            body = response.read(20_000_001)
            if len(body) > 20_000_000:
                raise ValueError("public observation exceeds the resource bound")
            return {
                "status": response.status,
                "headers": {key.lower(): value for key, value in response.headers.items()},
                "body": base64.b64encode(body).decode(),
            }

    def public_health(self, origin: str, slot: OdooSlot) -> HealthcheckEvidence:
        try:
            resource = self.public_resource(origin, "/launchplane/health")
        except OSError:
            return HealthcheckEvidence(status="fail")
        if resource["status"] != 200:
            return HealthcheckEvidence(status="fail")
        observed = runtime_identity_from_health_payload(
            json.loads(base64.b64decode(resource["body"]))
        )
        if observed != slot.runtime_identity:
            raise ValueError("public endpoint differs from the applied immutable slot")
        return HealthcheckEvidence(
            status="pass",
            verified=True,
            urls=(origin.rstrip("/") + "/launchplane/health",),
            timeout_seconds=20,
            runtime_identity_status="match",
            observed_runtime_identity=observed,
        )

    def browser(
        self,
        origin: str,
        routes: tuple[tuple[str, str], ...],
        *,
        contract: dict[str, Any] | None = None,
        notice: str = "",
    ) -> dict[str, Any]:
        errors: list[str] = []
        with sync_playwright() as playwright:
            browser = launch_testing_browser(playwright.chromium)
            try:
                context = browser.new_context(service_workers="block", accept_downloads=False)
                try:

                    def serve(route: Route) -> None:
                        parts = urlsplit(route.request.url)
                        if parts.hostname != self.plan.host or route.request.method not in {
                            "GET",
                            "HEAD",
                        }:
                            errors.append("unbound_browser_request")
                            route.abort()
                            return
                        path = parts.path + ("?" + parts.query if parts.query else "")
                        resource = self.private_resource(origin, path)
                        headers = resource["headers"]
                        route.fulfill(
                            status=resource["status"],
                            headers={
                                key: value
                                for key, value in headers.items()
                                if key
                                not in {
                                    "content-length",
                                    "content-encoding",
                                    "transfer-encoding",
                                    "connection",
                                }
                            },
                            body=base64.b64decode(resource["body"]),
                        )

                    context.route("**/*", serve)
                    context.route_web_socket("**/*", lambda socket: socket.close())
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append("browser_script_failed"))
                    page.on(
                        "requestfailed", lambda request: errors.append("browser_resource_failed")
                    )
                    page.on(
                        "response",
                        lambda response: (
                            errors.append("browser_http_failed") if response.status >= 400 else None
                        ),
                    )
                    page.set_default_timeout(20_000)
                    for path, expected in routes:
                        response = page.goto(
                            "https://" + self.plan.host + path, wait_until="networkidle"
                        )
                        if (
                            response is None
                            or response.status != 200
                            or expected not in page.content()
                        ):
                            raise ValueError(
                                "private browser render differs from its declared content"
                            )
                        page.locator("img").evaluate_all(
                            "images => images.forEach(image => { image.loading = 'eager'; })"
                        )
                        page.wait_for_function(
                            "Array.from(document.images).filter(image => image.getAttribute('src') && image.offsetWidth > 0 && image.offsetHeight > 0).every(image => image.naturalWidth > 0)"
                        )
                        if notice and (
                            notice not in page.content()
                            or page.locator(
                                "input:not([disabled]),button:not([disabled]),form"
                            ).count()
                        ):
                            raise ValueError("prepared browser has no visible writer pause")
                    if errors:
                        raise ValueError("private browser has failed resources or scripts")
                finally:
                    context.close()
            finally:
                browser.close()
        return {
            "status": "pass",
            "paths": [path for path, _expected in routes],
            "errors": [],
            "checked_at": time.time(),
            "contract_sha256": readiness_fingerprint(contract) if contract is not None else "",
        }
