from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from email.message import Message
import unittest
from urllib.error import HTTPError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from control_plane.contracts.merge_train_policy import (
    ProviderCodeScanningToolExpectationV1,
    ProviderDeliveryProtectionExpectationV1,
    ProviderPullRequestExpectationV1,
    ProviderRequiredStatusCheckExpectationV1,
)
from control_plane.contracts.provider_delivery_inspection import (
    ProviderDeliveryInspectionResultV1,
)
from control_plane.contracts.provider_delivery_readiness import (
    PROVIDER_INSPECTION_MAX_RETRY_AFTER_SECONDS,
)
from control_plane.github_app_identity import GitHubAppIdentity
from control_plane.provider_delivery_inspection_github import (
    ProviderDeliveryInspectionCapabilityError,
    inspect_provider_delivery_protection,
)
from control_plane.provider_delivery_inspection_profile import (
    PROVIDER_DELIVERY_INSPECTION_PERMISSIONS,
    ResolvedProviderDeliveryInspectionProfile,
)


class _GitHubFixture:
    def __init__(self, test: unittest.TestCase, *, private_key: str) -> None:
        self.test = test
        self.private_key = private_key
        self.calls: list[dict[str, object]] = []
        self.rulesets: dict[int, dict[str, object]] = {
            10: {
                "id": 10,
                "source_type": "Repository",
                "source": "example/repo",
                "target": "branch",
                "enforcement": "active",
                "bypass_actors": [
                    {
                        "actor_id": 42,
                        "actor_type": "Integration",
                        "bypass_mode": "pull_request",
                    }
                ],
                "rules": [{"type": "update"}],
            },
            20: {
                "id": 20,
                "source_type": "Repository",
                "source": "example/repo",
                "target": "branch",
                "enforcement": "active",
                "bypass_actors": [],
                "rules": [
                    {"type": "deletion"},
                    {"type": "non_fast_forward"},
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "do_not_enforce_on_create": False,
                            "strict_required_status_checks_policy": True,
                            "required_status_checks": [
                                {"context": "ci-gate", "integration_id": 9001}
                            ],
                        },
                    },
                ],
            },
        }
        self.evaluated: list[dict[str, object]] = [
            {"type": "update", "ruleset_id": 10},
            {"type": "required_status_checks", "ruleset_id": 20},
        ]
        self.classic: object = HTTPError(
            url="https://api.github.com/repos/example/repo/branches/main/protection",
            code=404,
            msg="Not Found",
            hdrs=Message(),
            fp=None,
        )
        self.classic_status: object = self._not_found("required_status_checks")
        self.classic_review: object = self._not_found("required_pull_request_reviews")
        self.classic_signatures: object = self._not_found("required_signatures")
        self.classic_restrictions: object = self._not_found("restrictions")
        self.classic_apps: object = []
        self.fail_cleanup = False

    @staticmethod
    def _not_found(suffix: str) -> HTTPError:
        return HTTPError(
            url=("https://api.github.com/repos/example/repo/branches/main/protection/" + suffix),
            code=404,
            msg="Not Found",
            hdrs=Message(),
            fp=None,
        )

    def request(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        timeout = kwargs["timeout_seconds"]
        self.test.assertIsInstance(timeout, (int, float))
        assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
        self.test.assertGreater(timeout, 0)
        self.test.assertLessEqual(timeout, 15)
        path = kwargs["path"]
        if path == "/app":
            return {"id": 700}
        if path == "/repos/example/repo/installation":
            return {
                "id": 701,
                "app_id": 700,
                "account": {"id": 456, "login": "example"},
                "permissions": {
                    "administration": "write",
                    "contents": "read",
                    "metadata": "read",
                },
            }
        if path == "/app/installations/701/access_tokens":
            self.test.assertEqual(kwargs["method"], "POST")
            self.test.assertEqual(
                kwargs["body"],
                {
                    "repository_ids": [123],
                    "permissions": {"administration": "write", "contents": "read"},
                },
            )
            return {
                "token": "provider-inspection-token",
                "expires_at": "2026-09-11T21:00:00Z",
                "permissions": {
                    "administration": "write",
                    "contents": "read",
                    "metadata": "read",
                },
                "repositories": [{"id": 123, "full_name": "example/repo"}],
            }
        if path == "/repos/example/repo":
            return {
                "id": 123,
                "full_name": "example/repo",
                "owner": {"id": 456, "login": "example", "type": "User"},
                "allow_merge_commit": True,
                "allow_squash_merge": False,
                "allow_rebase_merge": False,
            }
        if path == "/repos/example/repo/branches/main":
            return {"name": "main", "protected": True}
        if path == "/repos/example/repo/rules/branches/main?per_page=100":
            return self.evaluated
        if isinstance(path, str) and path.startswith("/repos/example/repo/rulesets/"):
            ruleset_id = int(path.split("/")[-1].split("?", 1)[0])
            return self.rulesets[ruleset_id]
        if path == "/repos/example/repo/branches/main/protection":
            if isinstance(self.classic, BaseException):
                raise self.classic
            return self.classic
        classic_paths = {
            "/repos/example/repo/branches/main/protection/required_status_checks": (
                self.classic_status
            ),
            "/repos/example/repo/branches/main/protection/required_pull_request_reviews": (
                self.classic_review
            ),
            "/repos/example/repo/branches/main/protection/required_signatures": (
                self.classic_signatures
            ),
            "/repos/example/repo/branches/main/protection/restrictions": (
                self.classic_restrictions
            ),
            "/repos/example/repo/branches/main/protection/restrictions/apps?per_page=100": (
                self.classic_apps
            ),
        }
        if path in classic_paths:
            response = classic_paths[path]
            if isinstance(response, BaseException):
                raise response
            return response
        if path == "/installation/token":
            self.test.assertEqual(kwargs["method"], "DELETE")
            if self.fail_cleanup:
                raise TimeoutError("revoke timed out")
            return None
        raise AssertionError(path)


class ProviderDeliveryInspectionGitHubTests(unittest.TestCase):
    private_key: str

    @classmethod
    def setUpClass(cls) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_key = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def _profile(self) -> ResolvedProviderDeliveryInspectionProfile:
        return ResolvedProviderDeliveryInspectionProfile(
            identity=GitHubAppIdentity(app_id=700, private_key=self.private_key),
            profile_id="provider-delivery-inspection-v1",
            profile_sha256="a" * 64,
            app_id=700,
            secret_id="secret",
            secret_binding_id="binding",
            secret_version_id="version",
            permissions=PROVIDER_DELIVERY_INSPECTION_PERMISSIONS,
        )

    def _expectation(self) -> ProviderDeliveryProtectionExpectationV1:
        return ProviderDeliveryProtectionExpectationV1(
            required_status_checks=(
                ProviderRequiredStatusCheckExpectationV1(
                    context="ci-gate",
                    app_id=9001,
                ),
            ),
            strict_required_status_checks_policy=True,
            code_scanning_tools=(),
            pull_request=None,
            allowed_merge_methods=("merge",),
        )

    def _inspect(
        self,
        fixture: _GitHubFixture,
        *,
        expectation: ProviderDeliveryProtectionExpectationV1 | None = None,
        provider_seconds: float = 45,
        cleanup_seconds: float = 10,
        api_request: Callable[..., object] | None = None,
    ) -> tuple[ProviderDeliveryInspectionResultV1, list[tuple[str, object]]]:
        events: list[tuple[str, object]] = []
        result = inspect_provider_delivery_protection(
            profile=self._profile(),
            repository="example/repo",
            repository_id=123,
            repository_owner_id=456,
            base_branch="main",
            ordinary_delivery_app_id=42,
            expectation=expectation or self._expectation(),
            remaining_provider_seconds=lambda: provider_seconds,
            remaining_cleanup_seconds=lambda: cleanup_seconds,
            before_token_mint=lambda app_id, installation_id: events.append(
                ("before_mint", (app_id, installation_id))
            ),
            token_issued=lambda token: events.append(
                ("issued", (token.app_id, token.installation_id, token.repository_id))
            ),
            token_cleanup=lambda outcome: events.append(("cleanup", outcome)),
            api_request=api_request or fixture.request,
            utc_now=lambda: datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc),
        )
        return result, events

    def test_exact_ready_shape_uses_only_fixed_reads_and_cleans_up_token(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)

        result, events = self._inspect(fixture)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.reason_codes, ("provider_protection_ready",))
        self.assertIsNotNone(result.facts)
        assert result.facts is not None
        self.assertEqual(result.facts.effective_protection, self._expectation())
        self.assertEqual(result.facts.applicable_ruleset_ids, (10, 20))
        self.assertEqual(result.facts.update_ruleset_id, 10)
        self.assertEqual(result.facts.repository_owner_id, 456)
        self.assertEqual(result.provider_request_count, len(fixture.calls))
        self.assertEqual(
            events,
            [
                ("before_mint", (700, 701)),
                ("issued", (700, 701, 123)),
                ("cleanup", "confirmed_revoked"),
            ],
        )
        business_calls = fixture.calls[3:-1]
        self.assertTrue(all(call.get("method", "GET") == "GET" for call in business_calls))
        self.assertNotIn("provider-inspection-token", str(result.model_dump()))

    def test_complete_expectation_mismatch_is_negative_not_inconclusive(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        different = self._expectation().model_copy(
            update={"strict_required_status_checks_policy": False}
        )

        result, _ = self._inspect(fixture, expectation=different)

        self.assertEqual(result.status, "protection_not_ready")
        self.assertEqual(result.reason_codes, ("strict_status_checks_mismatch",))
        self.assertIsNotNone(result.facts)

    def test_complete_absence_of_required_status_checks_is_negative(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        gate_rules = fixture.rulesets[20]["rules"]
        assert isinstance(gate_rules, list)
        fixture.rulesets[20]["rules"] = gate_rules[:2]
        fixture.evaluated[1] = {"type": "deletion", "ruleset_id": 20}

        result, _ = self._inspect(fixture)

        self.assertEqual(result.status, "protection_not_ready")
        self.assertEqual(result.reason_codes, ("required_status_checks_mismatch",))

    def test_normalizes_exact_scanning_and_nullable_review_semantics(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        gate_rules = fixture.rulesets[20]["rules"]
        assert isinstance(gate_rules, list)
        gate_rules.extend(
            [
                {
                    "type": "code_scanning",
                    "parameters": {
                        "code_scanning_tools": [
                            {
                                "tool": "CodeQL",
                                "alerts_threshold": "errors",
                                "security_alerts_threshold": "high_or_higher",
                            }
                        ]
                    },
                },
                {
                    "type": "pull_request",
                    "parameters": {
                        "dismiss_stale_reviews_on_push": False,
                        "require_code_owner_review": False,
                        "require_last_push_approval": False,
                        "required_approving_review_count": 0,
                        "required_review_thread_resolution": True,
                        "allowed_merge_methods": ["merge"],
                    },
                },
            ]
        )
        expectation = self._expectation().model_copy(
            update={
                "code_scanning_tools": (
                    ProviderCodeScanningToolExpectationV1(
                        tool="CodeQL",
                        alerts_threshold="errors",
                        security_alerts_threshold="high_or_higher",
                    ),
                ),
                "pull_request": ProviderPullRequestExpectationV1(
                    dismiss_stale_reviews_on_push=False,
                    require_code_owner_review=False,
                    require_last_push_approval=False,
                    required_approving_review_count=0,
                    required_review_thread_resolution=True,
                ),
            }
        )

        result, _ = self._inspect(fixture, expectation=expectation)

        self.assertEqual(result.status, "ready")
        self.assertIsNotNone(result.facts)
        assert result.facts is not None
        self.assertEqual(result.facts.effective_protection.pull_request, expectation.pull_request)
        self.assertEqual(
            result.facts.effective_protection.code_scanning_tools,
            expectation.code_scanning_tools,
        )

    def test_unknown_semantic_parameter_fails_closed(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        gate_rules = fixture.rulesets[20]["rules"]
        assert isinstance(gate_rules, list)
        status_rule = gate_rules[2]
        assert isinstance(status_rule, dict)
        parameters = status_rule["parameters"]
        assert isinstance(parameters, dict)
        parameters["future_semantic_switch"] = True

        result, _ = self._inspect(fixture)

        self.assertEqual(result.status, "semantic_inconclusive")
        self.assertEqual(result.reason_codes, ("unsupported_protection_shape",))

    def test_classic_missing_visibility_and_linear_history_fail_closed(self) -> None:
        official_optional_shape: dict[str, object] = {
            "enforce_admins": {"enabled": True},
            "required_linear_history": {"enabled": False},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_conversation_resolution": {"enabled": False},
            "lock_branch": {"enabled": False},
        }
        fixture = _GitHubFixture(self, private_key=self.private_key)
        fixture.classic = {
            key: value
            for key, value in official_optional_shape.items()
            if key != "required_linear_history"
        }
        result, _ = self._inspect(fixture)
        self.assertEqual(
            result.reason_codes,
            ("classic_response_incomplete",),
        )

        fixture = _GitHubFixture(self, private_key=self.private_key)
        fixture.classic = {
            **official_optional_shape,
            "required_linear_history": {"enabled": True},
        }
        result, _ = self._inspect(fixture)
        self.assertEqual(result.status, "semantic_inconclusive")
        self.assertEqual(result.reason_codes, ("unsupported_protection_shape",))

    def test_complete_classic_status_check_contributes_gate_but_not_exclusivity(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        gate_rules = fixture.rulesets[20]["rules"]
        assert isinstance(gate_rules, list)
        fixture.rulesets[20]["rules"] = gate_rules[:2]
        fixture.classic = {
            "enforce_admins": {"enabled": True},
            "required_linear_history": {"enabled": False},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_conversation_resolution": {"enabled": False},
            "lock_branch": {"enabled": False},
        }
        fixture.classic_status = {
            "strict": True,
            "contexts": ["ci-gate"],
            "checks": [{"context": "ci-gate", "app_id": 9001}],
        }

        result, _ = self._inspect(fixture)

        self.assertEqual(result.status, "ready")
        self.assertIsNotNone(result.facts)
        assert result.facts is not None
        self.assertTrue(result.facts.classic_protection_present)
        self.assertEqual(result.facts.update_ruleset_id, 10)

    def test_twenty_rulesets_and_complete_classic_reads_fit_request_bound(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        for ruleset_id in range(21, 39):
            fixture.evaluated.append({"type": "deletion", "ruleset_id": ruleset_id})
            fixture.rulesets[ruleset_id] = {
                "id": ruleset_id,
                "source_type": "Repository",
                "source": "example/repo",
                "target": "branch",
                "enforcement": "active",
                "bypass_actors": [],
                "rules": [{"type": "deletion"}],
            }
        fixture.classic = {
            "enforce_admins": {"enabled": True},
            "required_linear_history": {"enabled": False},
            "required_conversation_resolution": {"enabled": False},
            "lock_branch": {"enabled": False},
            "restrictions": {"users": [], "teams": [], "apps": []},
        }

        def organization_owner(**kwargs: object) -> object:
            payload = fixture.request(**kwargs)
            if kwargs["path"] == "/repos/example/repo":
                assert isinstance(payload, dict)
                payload = {
                    **payload,
                    "owner": {"id": 456, "login": "example", "type": "Organization"},
                }
            return payload

        result, _ = self._inspect(fixture, api_request=organization_owner)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.provider_request_count, 32)
        self.assertEqual(result.provider_request_count, len(fixture.calls))

    def test_repository_owner_must_match_exact_inventory_identity(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        original_request = fixture.request

        def changed_owner(**kwargs: object) -> object:
            payload = original_request(**kwargs)
            if kwargs["path"] == "/repos/example/repo":
                assert isinstance(payload, dict)
                payload = {**payload, "owner": {"id": 999, "login": "example", "type": "User"}}
            return payload

        result, _ = self._inspect(fixture, api_request=changed_owner)

        self.assertEqual(result.status, "semantic_inconclusive")
        self.assertEqual(result.reason_codes, ("repository_identity_mismatch",))

    def test_update_rule_cannot_share_or_waive_a_gate(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        gate_rules = fixture.rulesets[20]["rules"]
        assert isinstance(gate_rules, list)
        fixture.rulesets[10]["rules"] = [
            {"type": "update"},
            gate_rules[2],
        ]

        result, _ = self._inspect(fixture)

        self.assertEqual(result.status, "protection_not_ready")
        self.assertEqual(result.reason_codes, ("update_ruleset_not_isolated",))

        fixture = _GitHubFixture(self, private_key=self.private_key)
        fixture.rulesets[20]["bypass_actors"] = [
            {
                "actor_id": 42,
                "actor_type": "Integration",
                "bypass_mode": "pull_request",
            }
        ]
        result, _ = self._inspect(fixture)
        self.assertEqual(result.reason_codes, ("gate_ruleset_bypass_present",))

    def test_foreign_app_is_not_confused_with_delivery_installation_id(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        fixture.rulesets[10]["bypass_actors"] = [
            {
                "actor_id": 701,
                "actor_type": "Integration",
                "bypass_mode": "pull_request",
            }
        ]

        result, _ = self._inspect(fixture)

        self.assertEqual(result.status, "protection_not_ready")
        self.assertEqual(result.reason_codes, ("update_bypass_not_exclusive",))

    def test_full_page_and_unknown_rule_fail_semantically_closed(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        fixture.evaluated = [{"ruleset_id": index + 1} for index in range(100)]
        result, _ = self._inspect(fixture)
        self.assertEqual(
            (result.status, result.reason_codes),
            (
                "semantic_inconclusive",
                ("ruleset_page_full",),
            ),
        )

        fixture = _GitHubFixture(self, private_key=self.private_key)
        fixture.rulesets[20]["rules"] = [{"type": "merge_queue"}]
        result, _ = self._inspect(fixture)
        self.assertEqual(result.reason_codes, ("unknown_rule_type",))

    def test_unreadable_applicable_ruleset_is_semantic_visibility_loss(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        original_request = fixture.request

        def hidden_ruleset(**kwargs: object) -> object:
            if kwargs["path"] == "/repos/example/repo/rulesets/20?includes_parents=true":
                raise HTTPError(
                    url="https://api.github.com/repos/example/repo/rulesets/20",
                    code=404,
                    msg="Not Found",
                    hdrs=Message(),
                    fp=None,
                )
            return original_request(**kwargs)

        result, _ = self._inspect(fixture, api_request=hidden_ruleset)

        self.assertEqual(result.status, "semantic_inconclusive")
        self.assertEqual(
            result.reason_codes,
            ("inherited_ruleset_visibility_unavailable",),
        )

    def test_provider_deadline_denies_before_mint_and_permission_is_capability(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        with self.assertRaises(ProviderDeliveryInspectionCapabilityError) as deadline:
            self._inspect(fixture, provider_seconds=0)
        self.assertEqual(deadline.exception.reason_code, "provider_attempt_deadline")
        self.assertEqual(fixture.calls, [])

        fixture = _GitHubFixture(self, private_key=self.private_key)

        def denied(**kwargs: object) -> object:
            if kwargs["path"] == "/app":
                headers = Message()
                raise HTTPError(
                    url="https://api.github.com/app",
                    code=403,
                    msg="Forbidden",
                    hdrs=headers,
                    fp=None,
                )
            return fixture.request(**kwargs)

        with self.assertRaises(ProviderDeliveryInspectionCapabilityError) as permission:
            self._inspect(fixture, api_request=denied)
        self.assertEqual(permission.exception.reason_code, "provider_permission_denied")

    def test_deadline_immediately_before_post_does_not_mark_mint_unknown(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        remaining = iter((45.0, 44.0, 0.0))
        events: list[tuple[str, object]] = []

        with self.assertRaises(ProviderDeliveryInspectionCapabilityError) as caught:
            inspect_provider_delivery_protection(
                profile=self._profile(),
                repository="example/repo",
                repository_id=123,
                repository_owner_id=456,
                base_branch="main",
                ordinary_delivery_app_id=42,
                expectation=self._expectation(),
                remaining_provider_seconds=lambda: next(remaining),
                remaining_cleanup_seconds=lambda: 10,
                before_token_mint=lambda app_id, installation_id: events.append(
                    ("before_mint", (app_id, installation_id))
                ),
                token_issued=lambda token: events.append(("issued", token.installation_id)),
                token_cleanup=lambda outcome: events.append(("cleanup", outcome)),
                api_request=fixture.request,
                utc_now=lambda: datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(caught.exception.reason_code, "provider_attempt_deadline")
        self.assertEqual(events, [])
        self.assertEqual(
            [call["path"] for call in fixture.calls],
            ["/app", "/repos/example/repo/installation"],
        )

    def test_invalid_returned_token_is_revoked_and_cleanup_is_reported(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        events: list[tuple[str, object]] = []

        def invalid_token(**kwargs: object) -> object:
            payload = fixture.request(**kwargs)
            if kwargs["path"] == "/app/installations/701/access_tokens":
                assert isinstance(payload, dict)
                payload = {**payload, "repositories": [{"id": 999, "full_name": "other/repo"}]}
            return payload

        with self.assertRaises(ProviderDeliveryInspectionCapabilityError) as caught:
            inspect_provider_delivery_protection(
                profile=self._profile(),
                repository="example/repo",
                repository_id=123,
                repository_owner_id=456,
                base_branch="main",
                ordinary_delivery_app_id=42,
                expectation=self._expectation(),
                remaining_provider_seconds=lambda: 45,
                remaining_cleanup_seconds=lambda: 10,
                before_token_mint=lambda app_id, installation_id: events.append(
                    ("before_mint", (app_id, installation_id))
                ),
                token_issued=lambda token: events.append(("issued", token.installation_id)),
                token_cleanup=lambda outcome: events.append(("cleanup", outcome)),
                api_request=invalid_token,
                utc_now=lambda: datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(caught.exception.reason_code, "provider_transport")
        self.assertEqual(
            events,
            [("before_mint", (700, 701)), ("cleanup", "confirmed_revoked")],
        )

    def test_provider_retry_after_survives_successful_cleanup(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        original_request = fixture.request

        def rate_limited(**kwargs: object) -> object:
            if kwargs["path"] == "/repos/example/repo":
                headers = Message()
                headers["Retry-After"] = "30"
                raise HTTPError(
                    url="https://api.github.com/repos/example/repo",
                    code=429,
                    msg="Too Many Requests",
                    hdrs=headers,
                    fp=None,
                )
            return original_request(**kwargs)

        with self.assertRaises(ProviderDeliveryInspectionCapabilityError) as caught:
            self._inspect(fixture, api_request=rate_limited)

        self.assertEqual(caught.exception.reason_code, "provider_wait")
        self.assertEqual(caught.exception.retry_not_before, 1_789_156_830)

    def test_cleanup_failure_is_typed_and_reported_exactly_once(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        fixture.fail_cleanup = True
        events: list[str] = []

        with self.assertRaises(ProviderDeliveryInspectionCapabilityError) as caught:
            inspect_provider_delivery_protection(
                profile=self._profile(),
                repository="example/repo",
                repository_id=123,
                repository_owner_id=456,
                base_branch="main",
                ordinary_delivery_app_id=42,
                expectation=self._expectation(),
                remaining_provider_seconds=lambda: 45,
                remaining_cleanup_seconds=lambda: 10,
                before_token_mint=lambda app_id, installation_id: None,
                token_issued=lambda token: None,
                token_cleanup=events.append,
                api_request=fixture.request,
                utc_now=lambda: datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(caught.exception.reason_code, "cleanup_unknown")
        self.assertEqual(events, ["cleanup_unknown"])

    def test_cleanup_failure_keeps_strongest_bounded_provider_retry(self) -> None:
        fixture = _GitHubFixture(self, private_key=self.private_key)
        original_request = fixture.request

        def rate_limited_read_and_cleanup(**kwargs: object) -> object:
            if kwargs["path"] == "/repos/example/repo":
                headers = Message()
                headers["Retry-After"] = "30"
                raise HTTPError(
                    url="https://api.github.com/repos/example/repo",
                    code=429,
                    msg="Too Many Requests",
                    hdrs=headers,
                    fp=None,
                )
            if kwargs["path"] == "/installation/token":
                headers = Message()
                headers["Retry-After"] = "999999"
                raise HTTPError(
                    url="https://api.github.com/installation/token",
                    code=429,
                    msg="Too Many Requests",
                    hdrs=headers,
                    fp=None,
                )
            return original_request(**kwargs)

        with self.assertRaises(ProviderDeliveryInspectionCapabilityError) as caught:
            self._inspect(fixture, api_request=rate_limited_read_and_cleanup)

        self.assertEqual(caught.exception.reason_code, "cleanup_unknown")
        self.assertEqual(
            caught.exception.retry_not_before,
            int(datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc).timestamp())
            + PROVIDER_INSPECTION_MAX_RETRY_AFTER_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
