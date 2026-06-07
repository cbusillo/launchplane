import unittest

import click

from control_plane.npmplus import NpmplusProxyHost, NpmplusProxyHostPayload
from control_plane.workflows.npmplus_ingress import (
    IngressIdentityAccessBinding,
    NpmplusIngressApplyRequest,
    NpmplusIngressRouteDesiredState,
    apply_npmplus_ingress_route,
    identity_access_from_npmplus_auth_request,
)


class _FakeNpmplusClient:
    def __init__(self, proxy_hosts: tuple[NpmplusProxyHost, ...] = ()) -> None:
        self.proxy_hosts = list(proxy_hosts)
        self.calls: list[str] = []
        self.next_id = 100

    def list_proxy_hosts(self) -> tuple[NpmplusProxyHost, ...]:
        self.calls.append("list")
        return tuple(self.proxy_hosts)

    def create_proxy_host(self, payload: NpmplusProxyHostPayload) -> NpmplusProxyHost:
        self.calls.append("create")
        created = NpmplusProxyHost.model_validate({"id": self.next_id, **payload.to_api_payload()})
        self.proxy_hosts.append(created)
        return created

    def update_proxy_host(
        self, *, host_id: int, payload: NpmplusProxyHostPayload
    ) -> NpmplusProxyHost:
        self.calls.append(f"update:{host_id}")
        updated = NpmplusProxyHost.model_validate({"id": host_id, **payload.to_api_payload()})
        self.proxy_hosts = [updated if host.id == host_id else host for host in self.proxy_hosts]
        return updated

    def disable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        self.calls.append(f"disable:{host_id}")
        return self._set_enabled(host_id=host_id, enabled=False)

    def enable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        self.calls.append(f"enable:{host_id}")
        return self._set_enabled(host_id=host_id, enabled=True)

    def _set_enabled(self, *, host_id: int, enabled: bool) -> NpmplusProxyHost:
        for index, host in enumerate(self.proxy_hosts):
            if host.id == host_id:
                updated = NpmplusProxyHost.model_validate(
                    {**host.model_dump(mode="json"), "enabled": enabled}
                )
                self.proxy_hosts[index] = updated
                return updated
        raise AssertionError(f"Unknown proxy host: {host_id}")


def _desired_route(**overrides: object) -> NpmplusIngressRouteDesiredState:
    return NpmplusIngressRouteDesiredState.model_validate(
        {
            "domain_names": ["Ingress-Canary.Example.Test"],
            "forward_scheme": "http",
            "forward_host": "192.0.2.10",
            "forward_port": 8123,
            "certificate_id": 47,
            **overrides,
        }
    )


def _request(
    *,
    mode: str = "dry-run",
    route: NpmplusIngressRouteDesiredState | None = None,
    **overrides: object,
) -> NpmplusIngressApplyRequest:
    return NpmplusIngressApplyRequest.model_validate(
        {
            "mode": mode,
            "route": (route or _desired_route()).model_dump(mode="json", exclude_unset=True),
            "reason": "test ingress workflow",
            **overrides,
        }
    )


def _proxy_host(*, id: int = 78, enabled: bool = True, **overrides: object) -> NpmplusProxyHost:
    payload = _desired_route(**overrides).to_proxy_host_payload().model_dump(mode="json")
    return NpmplusProxyHost.model_validate({"id": id, **payload, "enabled": enabled})


def _location(**overrides: object) -> dict[str, object]:
    return {
        "path": "/ws",
        "forward_scheme": "http",
        "forward_host": "192.0.2.11",
        "forward_port": 9000,
        **overrides,
    }


class NpmplusIngressWorkflowTests(unittest.TestCase):
    def test_dry_run_plans_create_without_mutating(self) -> None:
        client = _FakeNpmplusClient()

        result = apply_npmplus_ingress_route(client=client, request=_request())

        self.assertEqual(result.status, "planned")
        self.assertTrue(result.dry_run)
        self.assertEqual(result.operations[0].action, "create")
        self.assertEqual(
            result.operations[0].change_categories,
            ("route", "upstream", "certificate", "tls", "provider_options"),
        )
        self.assertEqual(client.calls, ["list"])
        self.assertEqual(client.proxy_hosts, [])

    def test_apply_creates_missing_proxy_host(self) -> None:
        client = _FakeNpmplusClient()

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(mode="apply"),
        )

        self.assertEqual(result.status, "applied")
        self.assertEqual(result.proxy_host.id if result.proxy_host else None, 100)
        self.assertEqual(client.calls, ["list", "create"])

    def test_apply_updates_existing_proxy_host(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(forward_port=8080),))

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(mode="apply"),
        )

        self.assertEqual(tuple(operation.action for operation in result.operations), ("update",))
        self.assertEqual(result.operations[0].change_categories, ("upstream",))
        self.assertEqual(client.calls, ["list", "update:78"])
        self.assertEqual(result.proxy_host.forward_port if result.proxy_host else None, 8123)

    def test_apply_disables_existing_proxy_host(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(),))

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(mode="apply", route=_desired_route(enabled=False)),
        )

        self.assertEqual(tuple(operation.action for operation in result.operations), ("disable",))
        self.assertEqual(result.operations[0].change_categories, ("enabled",))
        self.assertFalse(result.proxy_host.enabled if result.proxy_host else True)
        self.assertEqual(client.calls, ["list", "disable:78"])

    def test_apply_update_skips_redundant_lifecycle_call(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(enabled=True, forward_port=8080),))

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(mode="apply", route=_desired_route(enabled=False)),
        )

        self.assertEqual(
            tuple(operation.action for operation in result.operations),
            ("update", "disable"),
        )
        self.assertFalse(result.proxy_host.enabled if result.proxy_host else True)
        self.assertEqual(client.calls, ["list", "update:78"])

    def test_noop_when_existing_route_matches_desired_state(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(),))

        result = apply_npmplus_ingress_route(client=client, request=_request(mode="apply"))

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(tuple(operation.action for operation in result.operations), ("no-op",))
        self.assertEqual(result.operations[0].change_categories, ())
        self.assertEqual(client.calls, ["list"])

    def test_omitted_advanced_config_preserves_existing_proxy_host_value(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(advanced_config="# odoo config"),))

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(mode="apply"),
        )

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(tuple(operation.action for operation in result.operations), ("no-op",))
        self.assertEqual(
            result.proxy_host.advanced_config if result.proxy_host else "",
            "# odoo config",
        )
        self.assertEqual(client.calls, ["list"])

    def test_omitted_advanced_config_preserves_existing_value_during_update(self) -> None:
        client = _FakeNpmplusClient(
            (_proxy_host(advanced_config="# odoo config", forward_port=8080),)
        )

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(mode="apply"),
        )

        self.assertEqual(tuple(operation.action for operation in result.operations), ("update",))
        self.assertEqual(result.operations[0].change_categories, ("upstream",))
        self.assertEqual(
            result.proxy_host.advanced_config if result.proxy_host else "",
            "# odoo config",
        )
        self.assertEqual(client.calls, ["list", "update:78"])

    def test_explicit_empty_advanced_config_clears_existing_proxy_host_value(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(advanced_config="# odoo config"),))

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(mode="apply", route=_desired_route(advanced_config="")),
        )

        self.assertEqual(tuple(operation.action for operation in result.operations), ("update",))
        self.assertEqual(result.operations[0].change_categories, ("provider_options",))
        self.assertEqual(result.proxy_host.advanced_config if result.proxy_host else None, "")
        self.assertEqual(client.calls, ["list", "update:78"])

    def test_explicit_advanced_config_replaces_existing_proxy_host_value(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(advanced_config="# odoo config"),))

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(
                mode="apply",
                route=_desired_route(advanced_config="# replacement config"),
            ),
        )

        self.assertEqual(tuple(operation.action for operation in result.operations), ("update",))
        self.assertEqual(result.operations[0].change_categories, ("provider_options",))
        self.assertEqual(
            result.proxy_host.advanced_config if result.proxy_host else "",
            "# replacement config",
        )
        self.assertEqual(client.calls, ["list", "update:78"])

    def test_noop_ignores_api_assigned_location_ids(self) -> None:
        route = _desired_route(locations=[_location()])
        existing = _proxy_host(locations=[_location(id=42)])
        client = _FakeNpmplusClient((existing,))

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(mode="apply", route=route),
        )

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(tuple(operation.action for operation in result.operations), ("no-op",))
        self.assertEqual(client.calls, ["list"])

    def test_update_identifies_identity_access_change(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(npmplus_auth_request="none"),))

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(
                route=_desired_route(
                    identity_access={
                        "mode": "forward-auth",
                        "provider": "authentik",
                    }
                )
            ),
        )

        self.assertEqual(tuple(operation.action for operation in result.operations), ("update",))
        self.assertEqual(result.operations[0].change_categories, ("identity_access",))

    def test_update_identifies_location_change_as_provider_options(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(locations=[_location(forward_port=9001)]),))

        result = apply_npmplus_ingress_route(
            client=client,
            request=_request(route=_desired_route(locations=[_location()])),
        )

        self.assertEqual(tuple(operation.action for operation in result.operations), ("update",))
        self.assertEqual(result.operations[0].change_categories, ("provider_options",))

    def test_identity_access_binding_maps_authentik_forward_auth(self) -> None:
        route = _desired_route(
            identity_access={
                "mode": "forward-auth",
                "provider": "authentik",
            }
        )

        payload = route.to_proxy_host_payload()

        self.assertEqual(payload.npmplus_auth_request, "authentik")
        self.assertEqual(
            route.identity_access.provider if route.identity_access else "", "authentik"
        )

    def test_edge_endpoint_key_rejects_raw_forward_host_conflict(self) -> None:
        with self.assertRaises(ValueError):
            _desired_route(edge_endpoint_key="cm-prod-dokploy")

    def test_identity_access_binding_maps_authentik_basic_auth_forwarding(self) -> None:
        route = _desired_route(
            identity_access={
                "mode": "forward-auth",
                "provider": "authentik",
                "send_basic_auth": True,
            }
        )

        self.assertEqual(
            route.to_proxy_host_payload().npmplus_auth_request,
            "authentik-send-basic-auth",
        )

    def test_identity_access_binding_rejects_provider_without_forward_auth(self) -> None:
        with self.assertRaises(ValueError):
            IngressIdentityAccessBinding.model_validate({"mode": "none", "provider": "authentik"})

    def test_identity_access_binding_rejects_npmplus_conflict(self) -> None:
        with self.assertRaises(ValueError):
            _desired_route(
                npmplus_auth_request="authelia",
                identity_access={"mode": "forward-auth", "provider": "authentik"},
            )

    def test_identity_access_binding_treats_legacy_none_as_unset(self) -> None:
        route = _desired_route(
            npmplus_auth_request="none",
            identity_access={"mode": "forward-auth", "provider": "authentik"},
        )

        self.assertEqual(route.to_proxy_host_payload().npmplus_auth_request, "authentik")

    def test_identity_access_can_be_derived_from_npmplus_authentik(self) -> None:
        binding = identity_access_from_npmplus_auth_request("authentik-send-basic-auth")

        self.assertEqual(binding.mode, "forward-auth")
        self.assertEqual(binding.provider, "authentik")
        self.assertTrue(binding.send_basic_auth)

    def test_identity_access_round_trips_existing_npmplus_auth_request_modes(self) -> None:
        for auth_request in ("anubis", "tinyauth", "authelia", "authentik"):
            with self.subTest(auth_request=auth_request):
                binding = identity_access_from_npmplus_auth_request(auth_request)

                self.assertEqual(binding.mode, "forward-auth")
                self.assertEqual(binding.provider, auth_request)
                self.assertFalse(binding.send_basic_auth)
                self.assertEqual(binding.to_npmplus_auth_request(), auth_request)

    def test_rejects_ambiguous_domain_matches(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(id=78), _proxy_host(id=79)))

        with self.assertRaises(click.ClickException):
            apply_npmplus_ingress_route(client=client, request=_request())

    def test_rejects_missing_expected_host_id(self) -> None:
        client = _FakeNpmplusClient((_proxy_host(id=78),))

        with self.assertRaises(click.ClickException):
            apply_npmplus_ingress_route(
                client=client,
                request=_request(expected_host_id=79),
            )

    def test_rejects_expected_host_id_when_expected_host_domain_does_not_match(
        self,
    ) -> None:
        client = _FakeNpmplusClient((_proxy_host(id=79, domain_names=["other.example.test"]),))

        with self.assertRaises(click.ClickException):
            apply_npmplus_ingress_route(
                client=client,
                request=_request(expected_host_id=79),
            )

    def test_rejects_expected_host_id_when_domain_matches_another_host(self) -> None:
        client = _FakeNpmplusClient(
            (
                _proxy_host(id=78),
                _proxy_host(id=79, domain_names=["other.example.test"]),
            )
        )

        with self.assertRaises(click.ClickException):
            apply_npmplus_ingress_route(
                client=client,
                request=_request(expected_host_id=79),
            )

    def test_rejects_expected_host_id_when_exact_domains_are_required(self) -> None:
        client = _FakeNpmplusClient(
            (
                _proxy_host(
                    id=78, domain_names=["ingress-canary.example.test", "other.example.test"]
                ),
            )
        )

        with self.assertRaises(click.ClickException):
            apply_npmplus_ingress_route(
                client=client,
                request=_request(
                    expected_host_id=78,
                    require_exact_expected_host_domains=True,
                ),
            )

        self.assertEqual(client.calls, ["list"])

    def test_requires_expected_host_id_for_exact_domain_guard(self) -> None:
        with self.assertRaises(ValueError):
            _request(require_exact_expected_host_domains=True)

    def test_rejects_create_when_disallowed(self) -> None:
        client = _FakeNpmplusClient()

        with self.assertRaises(click.ClickException):
            apply_npmplus_ingress_route(
                client=client,
                request=_request(allow_create=False),
            )

        self.assertEqual(client.calls, ["list"])


if __name__ == "__main__":
    unittest.main()
