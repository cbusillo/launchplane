import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import odoo_runtime_reads as reads
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretVersion
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceAuthorizationDenied
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH
from control_plane.odoo_addon_settings_override import (
    OdooAddonSettingsStore,
    OdooAddonSettingsApplyRequest,
    OdooShopifyAddonSettingsInput,
    build_odoo_addon_settings_plan,
    _validated_secret_bindings,
)
from control_plane.odoo_instance_overrides import SHOPIFY_STORE_SETTINGS, shopify_store_handle
from control_plane.odoo_product_driver_http import resolve_odoo_product_route
from control_plane.odoo_shopify_identity_probe import DOMAIN_PATTERN
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION, _decrypt_secret_value

ROUTE = "/v1/products/{product}/environments/{environment}/shopify-identity"


class ShopifyIdentityStore(OdooAddonSettingsStore, reads.OdooRuntimeReadStore, Protocol):
    def read_secret_version(self, version_id: str) -> SecretVersion: ...

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]: ...


class ShopifyIdentityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    separation: Literal["separate", "same", "unknown"] = "unknown"
    probe: Literal["verified", "token_rejected", "unavailable"] = "unavailable"
    testing_domain_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    testing_shop_id_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    production_domain_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    applied: Literal[False] = False
    values_redacted: Literal[True] = True

    @model_validator(mode="after")
    def require_separation_evidence(self) -> "ShopifyIdentityResult":
        if self.separation == "separate" and (
            self.probe != "verified"
            or not self.testing_domain_sha256
            or not self.testing_shop_id_sha256
            or not self.production_domain_sha256
            or self.testing_domain_sha256 == self.production_domain_sha256
        ):
            raise ValueError("Separation requires complete distinct store evidence")
        return self


@dataclass(frozen=True, repr=False)
class ShopifyAuthority:
    domain: str
    api_version: str
    version: SecretVersion
    snapshot: str


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def store_domain(value: str) -> str:
    domain = shopify_store_handle(value) + ".myshopify.com"
    if re.fullmatch(DOMAIN_PATTERN, domain) is None:
        raise ValueError("Store identity unavailable")
    return domain


def authority(store: ShopifyIdentityStore, product: str) -> ShopifyAuthority:
    profile = resolve_odoo_product_route(record_store=store, product=product, instance="testing")
    testing = next(lane for lane in profile.lanes if lane.instance == "testing")
    production = [lane for lane in profile.lanes if lane.instance == "prod"]
    if len(production) != 1:
        raise ValueError("Production identity unavailable")
    target = store.read_dokploy_target_record(
        context_name=testing.context, instance_name=testing.instance
    )
    hold = target.policies.staff_testing_hold
    if (
        hold is None
        or hold.purpose != "isolation_staging"
        or not any(
            item.integration == "shopify" and item.kind == "dev_store"
            for item in target.policies.integration_allowances
        )
    ):
        raise ValueError("Held testing identity unavailable")
    target_id = store.read_dokploy_target_id_record(
        context_name=testing.context, instance_name=testing.instance
    )
    if any(
        other.target_id == target_id.target_id
        and (other.context, other.instance) != (testing.context, testing.instance)
        for other in store.list_dokploy_target_id_records()
    ):
        raise ValueError("Testing target aliases another lane")
    override = store.read_odoo_instance_override_record(
        context_name=testing.context, instance_name=testing.instance
    )
    settings = {
        item.setting: item.value for item in override.addon_settings if item.addon == "shopify"
    }
    if settings["test_store"].source != "literal" or settings["test_store"].value is not True:
        raise ValueError("Testing settings unavailable")
    for name in ("shop_url_key", "api_version"):
        if settings[name].source != "literal" or not isinstance(settings[name].value, str):
            raise ValueError("Testing settings unavailable")
    request = OdooAddonSettingsApplyRequest(
        mode="dry-run",
        product=product,
        context=testing.context,
        instance=testing.instance,
        reason="Read-only held testing store identity",
        shopify=OdooShopifyAddonSettingsInput(
            shop_url_key=str(settings["shop_url_key"].value),
            api_version=str(settings["api_version"].value),
            test_store=True,
            api_token_secret_binding_id=settings["api_token"].secret_binding_id,
            webhook_key_secret_binding_id=settings["webhook_key"].secret_binding_id,
        ),
    )
    plan, _, _ = build_odoo_addon_settings_plan(record_store=store, request=request)
    binding = _validated_secret_bindings(record_store=store, request=request)["api_token"]
    record = store.read_secret_record(binding.secret_id)
    if (
        binding.declared_secret_class != "testing"
        or record.scope != "context_instance"
        or (record.context, record.instance) != (testing.context, testing.instance)
        or record.integration != RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
        or binding.integration != record.integration
        or any(
            item.secret_id == record.secret_id
            and item.status == "configured"
            and (item.context, item.instance) != (testing.context, testing.instance)
            for item in store.list_secret_bindings()
        )
    ):
        raise ValueError("Testing credential unavailable")
    version = store.read_secret_version(record.current_version_id)
    if version.secret_id != record.secret_id or version.version_id != record.current_version_id:
        raise ValueError("Testing credential unavailable")
    snapshot = json.dumps(
        [
            item.model_dump(mode="json")
            for item in (
                profile,
                override,
                target,
                target_id,
                binding,
                record,
                version,
            )
        ]
        + [plan.plan_sha256],
        sort_keys=True,
    )
    return ShopifyAuthority(
        store_domain(request.shopify.shop_url_key),
        request.shopify.api_version,
        version,
        snapshot,
    )


HOST_PROGRAM = r"""
import base64,json,re,subprocess,sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]
result={'status':'unavailable'}
def inspect():
    raw=subprocess.run(['docker','inspect','--type','container',spec['container']],capture_output=True,check=True,timeout=5)
    values=json.loads(raw.stdout)
    if len(values)!=1:
        raise ValueError()
    value=values[0]
    config=value['Config']
    env=dict(item.split('=',1) for item in config['Env'] if '=' in item)
    if (not re.fullmatch(r'[0-9a-f]{12,64}',spec['container'])
        or not re.fullmatch(r'[0-9a-f]{64}',value['Id'])
        or not value['Id'].startswith(spec['container'])
        or value['State'].get('Running') is not True
        or config['Image']!=spec['identity']['image_reference']
        or config['Labels'].get('com.docker.compose.project')!=spec['project']
        or config['Labels'].get('com.docker.compose.service')!='web'
        or env.get('PLATFORM_INSTANCE')!=spec['identity']['instance']
        or spec['identity']['instance'] not in ('testing','prod')
        or json.loads(env[__IDENTITY_KEY__])!=spec['identity']):
        raise ValueError()
    return {key:value.get(key) for key in ('Id','Image','Config','HostConfig','Mounts','NetworkSettings','RestartCount')} | {'State':{key:value['State'].get(key) for key in ('Running','StartedAt','Pid')}}
try:
    before=inspect()
    child=subprocess.run(
        ['docker','exec','-i',before['Id'],'timeout','--kill-after=2s','15s','python3','-I','-c',spec['program']],
        input=json.dumps(spec['request']),text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=20,check=False)
    after=inspect()
    if before!=after or child.returncode!=0 or len(child.stdout)>512:
        raise ValueError()
    result=json.loads(child.stdout)
except Exception:
    result={'status':'unavailable'}
print(prefix+json.dumps({'type':'complete','result':result}),flush=True)
""".replace("__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY))


def runtime_identity(
    connection: reads.OdooRuntimeConnection,
    selected: ShopifyAuthority | None,
    environment: dict[str, str],
    token: str,
) -> dict[str, str]:
    observed = json.loads(environment[RUNTIME_IDENTITY_ENV_KEY])
    frames: list[dict[str, object]] = []
    production = selected is None
    program = "odoo_shopify_production_probe.py" if production else "odoo_shopify_identity_probe.py"
    request: dict[str, object] = (
        {
            "config_path": ODOO_WEB_CONFIG_PATH,
            "store_keys": ["shopify." + key for key in SHOPIFY_STORE_SETTINGS],
        }
        if selected is None
        else {"domain": selected.domain, "api_version": selected.api_version, "token": token}
    )
    _read_remote(
        host=connection.host,
        token=connection.token,
        server_id=connection.server_id,
        program=HOST_PROGRAM,
        payload={
            "container": connection.evidence.container_id,
            "project": connection.app_name,
            "identity": observed,
            "program": Path(__file__).with_name(program).read_text(),
            "request": request,
        },
        receive=frames.append,
        timeout_seconds=40,
    )
    if len(frames) != 1 or set(frames[0]) != {"type", "result"} or frames[0]["type"] != "complete":
        raise ValueError("Identity unavailable")
    result = frames[0]["result"]
    if not isinstance(result, dict) or result.get("status") not in {
        "verified",
        "token_rejected",
        "unavailable",
    }:
        raise ValueError("Identity unavailable")
    keys = (
        (
            {"status", "domain_sha256"}
            if production
            else {"status", "shop_id_sha256", "domain_sha256"}
        )
        if result["status"] == "verified"
        else {"status"}
    )
    if set(result) != keys or any(not isinstance(value, str) for value in result.values()):
        raise ValueError("Identity unavailable")
    return result


def compare_identity(
    *,
    store: ShopifyIdentityStore,
    profile: LaunchplaneProductProfileRecord,
    control_plane_root: Path,
    database_url: str | None,
) -> ShopifyIdentityResult:
    try:
        product = profile.product
        selected = authority(store, product)
        if store.read_product_profile_record(product) != profile:
            return ShopifyIdentityResult()
        production_selection = reads.select_stable_runtime(store, profile, "prod")
        production_connection = reads.connect_runtime(
            store=store,
            selection=production_selection,
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        production_environment = reads.inspect_runtime_container(
            production_connection.host,
            production_connection.token,
            production_connection.server_id,
            production_connection.evidence.container_id,
            production_connection.app_name,
            production_selection.identity,
        )
        production = runtime_identity(production_connection, None, production_environment, "")
        if (
            production.get("status") != "verified"
            or re.fullmatch(r"[a-f0-9]{64}", production.get("domain_sha256", "")) is None
        ):
            return ShopifyIdentityResult()
        production_domain = production["domain_sha256"]
        if digest(selected.domain) == production_domain:
            return ShopifyIdentityResult(
                separation="same", production_domain_sha256=production_domain
            )
        selection = reads.select_stable_runtime(store, profile, "testing")
        connection = reads.connect_runtime(
            store=store,
            selection=selection,
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        environment = reads.inspect_runtime_container(
            connection.host,
            connection.token,
            connection.server_id,
            connection.evidence.container_id,
            connection.app_name,
            selection.identity,
        )
        token = _decrypt_secret_value(selected.version.ciphertext, selected.version.key_id)
        observed = runtime_identity(connection, selected, environment, token)
        reads.assert_selection_current(store, selection)
        if (
            authority(store, product).snapshot != selected.snapshot
            or reads.inspect_runtime_container(
                connection.host,
                connection.token,
                connection.server_id,
                connection.evidence.container_id,
                connection.app_name,
                selection.identity,
            )
            != environment
        ):
            return ShopifyIdentityResult()
        reads.assert_selection_current(store, production_selection)
        if runtime_identity(production_connection, None, production_environment, "") != production:
            return ShopifyIdentityResult()
        if authority(store, product).snapshot != selected.snapshot:
            return ShopifyIdentityResult()
        reads.assert_selection_current(store, production_selection)
        reads.assert_selection_current(store, selection)
        result = ShopifyIdentityResult(
            probe=cast(Literal["verified", "token_rejected", "unavailable"], observed["status"]),
            production_domain_sha256=production_domain,
        )
        if result.probe != "verified":
            return result
        testing_domain = observed["domain_sha256"]
        if testing_domain == production_domain:
            return result.model_copy(update={"separation": "same"})
        if testing_domain != digest(selected.domain):
            return ShopifyIdentityResult()
        return ShopifyIdentityResult(
            separation="separate",
            probe="verified",
            testing_domain_sha256=testing_domain,
            testing_shop_id_sha256=observed["shop_id_sha256"],
            production_domain_sha256=production_domain,
        )
    except NativeSourceAuthorizationDenied:
        raise reads.OdooRuntimeReadError(
            "authorization_denied", "The provider refused the Shopify identity read.", 403
        ) from None
    except Exception:
        return ShopifyIdentityResult()
