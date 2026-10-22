import hashlib
import json
import re
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DOMAIN_PATTERN = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com"
QUERY = "query { shop { id myshopifyDomain } }"


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def read_identity(spec: dict[str, object]) -> dict[str, str]:
    unknown = {"status": "unavailable"}
    domain, version, token = (spec.get(key) for key in ("domain", "api_version", "token"))
    if (
        not isinstance(domain, str)
        or re.fullmatch(DOMAIN_PATTERN, domain) is None
        or not isinstance(version, str)
        or re.fullmatch(r"[0-9]{4}-(01|04|07|10)", version) is None
        or not isinstance(token, str)
        or not token
        or len(token) > 4096
        or any(character.isspace() for character in token)
    ):
        return unknown
    request = Request(
        f"https://{domain}/admin/api/{version}/graphql.json",
        data=json.dumps({"query": QUERY}).encode(),
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        method="POST",
    )
    try:
        with build_opener(NoRedirects(), ProxyHandler({})).open(request, timeout=10) as response:
            raw = response.read(8193)
        if len(raw) > 8192:
            return unknown
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("errors"):
            return unknown
        shop = payload.get("data", {}).get("shop")
        if not isinstance(shop, dict):
            return unknown
        shop_id, observed = shop.get("id"), shop.get("myshopifyDomain")
        if (
            not isinstance(shop_id, str)
            or re.fullmatch(r"gid://shopify/Shop/[1-9][0-9]*", shop_id) is None
            or observed != domain
        ):
            return unknown
        return {
            "status": "verified",
            "shop_id_sha256": hashlib.sha256(shop_id.encode()).hexdigest(),
            "domain_sha256": hashlib.sha256(domain.encode()).hexdigest(),
        }
    except HTTPError as error:
        return {"status": "token_rejected"} if error.code in {401, 403} else unknown
    except (OSError, ValueError, TypeError, AttributeError):
        return unknown


def main() -> None:
    try:
        spec = json.loads(sys.stdin.read(16385))
        result = read_identity(spec)
    except (OSError, ValueError, TypeError, AttributeError):
        result = {"status": "unavailable"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
